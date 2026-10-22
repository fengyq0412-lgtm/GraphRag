import hashlib
import json
import re
import sqlite3
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path


def norm(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def chunks(text, limit=700):
    """Split at paragraph boundaries, then hard split long paragraphs."""
    result, current = [], ""
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and len(current) + len(paragraph) + 2 > limit:
            result.append(current)
            current = ""
        while len(paragraph) > limit:
            if current:
                result.append(current)
                current = ""
            result.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = (current + "\n\n" + paragraph).strip() if paragraph else current
    if current:
        result.append(current)
    return result


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, normalized TEXT NOT NULL,
  UNIQUE(normalized, type)
);
CREATE TABLE IF NOT EXISTS aliases (
  normalized TEXT NOT NULL, entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  PRIMARY KEY(normalized, entity_id)
);
CREATE TABLE IF NOT EXISTS assertions (
  id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES entities(id),
  predicate TEXT NOT NULL, object_id TEXT NOT NULL REFERENCES entities(id),
  chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  quote TEXT NOT NULL, confidence REAL NOT NULL, status TEXT NOT NULL DEFAULT 'accepted',
  UNIQUE(subject_id, predicate, object_id, chunk_id, quote)
);
CREATE INDEX IF NOT EXISTS assertion_subject ON assertions(subject_id);
CREATE INDEX IF NOT EXISTS assertion_object ON assertions(object_id);
CREATE TABLE IF NOT EXISTS resolution_queue (
  id TEXT PRIMARY KEY, surface TEXT NOT NULL, type TEXT NOT NULL, candidate_id TEXT NOT NULL,
  similarity REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(body, content='chunks', content_rowid='rowid')")
            self.db.executescript("""
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
              INSERT INTO chunk_fts(rowid,body) VALUES(new.rowid,new.body);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
              INSERT INTO chunk_fts(chunk_fts,rowid,body) VALUES('delete',old.rowid,old.body);
            END;
            """)
            self.db.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('rebuild')")
            self.db.commit()
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False

    def close(self):
        self.db.close()

    def entity(self, name, kind):
        name, kind = name.strip(), kind.strip() or "concept"
        normalized = norm(name)
        if not normalized:
            raise ValueError("Entity name is empty")
        row = self.db.execute("""SELECT e.* FROM entities e JOIN aliases a ON a.entity_id=e.id
            WHERE a.normalized=? AND e.type=?""", (normalized, kind)).fetchone()
        if row:
            return row["id"]
        candidates = self.db.execute("SELECT * FROM entities WHERE type=?", (kind,)).fetchall()
        for item in candidates:
            similarity = SequenceMatcher(None, normalized, item["normalized"]).ratio()
            if 0.76 <= similarity < 1:
                queue_id = digest(name + kind + item["id"])
                self.db.execute("""INSERT OR IGNORE INTO resolution_queue
                    (id,surface,type,candidate_id,similarity) VALUES(?,?,?,?,?)""",
                    (queue_id, name, kind, item["id"], similarity))
        entity_id = digest(kind + ":" + normalized)
        self.db.execute("INSERT OR IGNORE INTO entities(id,name,type,normalized) VALUES(?,?,?,?)",
                        (entity_id, name, kind, normalized))
        self.db.execute("INSERT OR IGNORE INTO aliases(normalized,entity_id) VALUES(?,?)", (normalized, entity_id))
        return entity_id

    def ingest(self, title, body, facts):
        if not title.strip() or not body.strip():
            raise ValueError("title and text are required")
        parts = chunks(body)
        if not parts:
            raise ValueError("No text to ingest")
        document_id = digest(title + "\n" + body)
        accepted, rejected = 0, []
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO documents(id,title,body) VALUES(?,?,?)", (document_id, title, body))
            for ordinal, part in enumerate(parts):
                chunk_id = f"{document_id}:{ordinal}"
                self.db.execute("INSERT OR IGNORE INTO chunks(id,document_id,ordinal,body) VALUES(?,?,?,?)",
                                (chunk_id, document_id, ordinal, part))
            for fact in facts:
                try:
                    subject = fact["subject"].strip()
                    predicate = fact["predicate"].strip()
                    obj = fact["object"].strip()
                    quote = fact["quote"].strip()
                    if not all((subject, predicate, obj, quote)):
                        raise ValueError("empty fact field")
                    match = next((i for i, part in enumerate(parts) if quote in part), None)
                    if match is None:
                        raise ValueError("quote is not an exact substring of a chunk")
                    confidence = float(fact.get("confidence", 1))
                    if not 0 <= confidence <= 1:
                        raise ValueError("confidence outside [0,1]")
                    sid = self.entity(subject, fact.get("subject_type", "concept"))
                    oid = self.entity(obj, fact.get("object_type", "concept"))
                    chunk_id = f"{document_id}:{match}"
                    aid = digest("|".join((sid, predicate, oid, chunk_id, quote)))
                    cursor = self.db.execute("""INSERT OR IGNORE INTO assertions
                        (id,subject_id,predicate,object_id,chunk_id,quote,confidence) VALUES(?,?,?,?,?,?,?)""",
                        (aid, sid, predicate, oid, chunk_id, quote, confidence))
                    accepted += cursor.rowcount
                except (KeyError, ValueError, TypeError) as exc:
                    rejected.append({"fact": fact, "reason": str(exc)})
        return {"document_id": document_id, "chunks": len(parts), "assertions": accepted, "rejected": rejected}

    def resolve(self, queue_id, action):
        row = self.db.execute("SELECT * FROM resolution_queue WHERE id=? AND status='pending'", (queue_id,)).fetchone()
        if not row:
            raise ValueError("Pending resolution not found")
        if action not in ("merge", "distinct"):
            raise ValueError("action must be merge or distinct")
        with self.db:
            if action == "merge":
                source = self.db.execute("SELECT id FROM entities WHERE normalized=? AND type=?",
                                         (norm(row["surface"]), row["type"])).fetchone()
                if source and source["id"] != row["candidate_id"]:
                    self.db.execute("UPDATE OR IGNORE assertions SET subject_id=? WHERE subject_id=?",
                                    (row["candidate_id"], source["id"]))
                    self.db.execute("UPDATE OR IGNORE assertions SET object_id=? WHERE object_id=?",
                                    (row["candidate_id"], source["id"]))
                    self.db.execute("DELETE FROM assertions WHERE subject_id=? OR object_id=?", (source["id"], source["id"]))
                    self.db.execute("DELETE FROM entities WHERE id=?", (source["id"],))
                self.db.execute("INSERT OR IGNORE INTO aliases(normalized,entity_id) VALUES(?,?)",
                                (norm(row["surface"]), row["candidate_id"]))
            self.db.execute("UPDATE resolution_queue SET status=? WHERE id=?", (action, queue_id))

    def pending(self):
        return [dict(row) for row in self.db.execute("""SELECT q.*, e.name AS candidate_name
            FROM resolution_queue q JOIN entities e ON e.id=q.candidate_id WHERE q.status='pending'""")]

    def stats(self):
        return {name: self.db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in ("documents", "chunks", "entities", "assertions")}
