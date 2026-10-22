import hashlib
import json
import re
import sqlite3
import unicodedata
from difflib import SequenceMatcher, unified_diff
from pathlib import Path


def norm(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


class VersionConflict(ValueError):
    def __init__(self, current_version):
        self.current_version = current_version
        super().__init__(f"Version conflict: current version is {current_version}")


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
CREATE TABLE IF NOT EXISTS document_series (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, current_version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS document_versions (
  series_id TEXT NOT NULL REFERENCES document_series(id), version INTEGER NOT NULL,
  document_id TEXT NOT NULL REFERENCES documents(id), base_version INTEGER NOT NULL,
  status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(series_id,version), UNIQUE(document_id)
);
CREATE INDEX IF NOT EXISTS version_document ON document_versions(document_id);
CREATE TABLE IF NOT EXISTS version_conflicts (
  id TEXT PRIMARY KEY, series_id TEXT NOT NULL, version INTEGER NOT NULL,
  old_assertion_id TEXT NOT NULL REFERENCES assertions(id),
  new_assertion_id TEXT NOT NULL REFERENCES assertions(id),
  resolution TEXT NOT NULL DEFAULT 'pending',
  FOREIGN KEY(series_id,version) REFERENCES document_versions(series_id,version)
);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("""INSERT OR IGNORE INTO document_series(id,title,current_version,created_at)
            SELECT d.id,d.title,1,d.created_at FROM documents d
            WHERE NOT EXISTS(SELECT 1 FROM document_versions v WHERE v.document_id=d.id)""")
        self.db.execute("""INSERT OR IGNORE INTO document_versions
            (series_id,version,document_id,base_version,status,created_at)
            SELECT d.id,1,d.id,0,'active',d.created_at FROM documents d
            WHERE NOT EXISTS(SELECT 1 FROM document_versions v WHERE v.document_id=d.id)""")
        self.db.commit()
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

    def _insert_snapshot(self, document_id, title, body, facts, status="pending"):
        if not title.strip() or not body.strip():
            raise ValueError("title and text are required")
        parts = chunks(body)
        if not parts:
            raise ValueError("No text to ingest")
        accepted, rejected = 0, []
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
                    (id,subject_id,predicate,object_id,chunk_id,quote,confidence,status) VALUES(?,?,?,?,?,?,?,?)""",
                    (aid, sid, predicate, oid, chunk_id, quote, confidence, status))
                accepted += cursor.rowcount
            except (KeyError, ValueError, TypeError) as exc:
                rejected.append({"fact": fact, "reason": str(exc)})
        return {"document_id": document_id, "chunks": len(parts), "assertions": accepted, "rejected": rejected}

    def _conflicting_assertions(self, series_id, document_id):
        return [dict(row) for row in self.db.execute("""SELECT old.id AS old_id,new.id AS new_id,
            s.name AS subject,new.predicate,old_obj.name AS old_object,new_obj.name AS new_object,
            old.quote AS old_quote,new.quote AS new_quote,old.chunk_id AS old_chunk_id
            FROM assertions new JOIN chunks nc ON nc.id=new.chunk_id
            JOIN assertions old ON old.subject_id=new.subject_id AND old.predicate=new.predicate
                AND old.object_id<>new.object_id AND old.status IN ('accepted','contested')
            JOIN chunks oc ON oc.id=old.chunk_id
            JOIN document_versions ov ON ov.document_id=oc.document_id AND ov.status='active'
            JOIN entities s ON s.id=new.subject_id JOIN entities old_obj ON old_obj.id=old.object_id
            JOIN entities new_obj ON new_obj.id=new.object_id
            WHERE nc.document_id=? AND ov.series_id<>?""", (document_id, series_id))]

    def _record_conflicts(self, series_id, version, conflicts):
        for item in conflicts:
            conflict_id = digest(f"{series_id}:{version}:{item['old_id']}:{item['new_id']}")
            self.db.execute("""INSERT INTO version_conflicts(id,series_id,version,old_assertion_id,new_assertion_id)
                VALUES(?,?,?,?,?)""", (conflict_id, series_id, version, item["old_id"], item["new_id"]))

    def ingest(self, title, body, facts):
        document_id = digest(title + "\n" + body)
        series_id = document_id
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute("""SELECT v.status FROM document_series s JOIN document_versions v
                ON v.series_id=s.id AND v.version=1 WHERE s.id=?""", (series_id,)).fetchone()
            if existing:
                self.db.commit()
                return {"document_id": document_id, "series_id": series_id, "version": 1,
                        "status": existing["status"], "chunks": len(chunks(body)), "assertions": 0, "rejected": []}
            result = self._insert_snapshot(document_id, title, body, facts)
            if result["rejected"]:
                raise ValueError("Ingest contains unsupported facts: " + json.dumps(result["rejected"], ensure_ascii=False))
            conflicts = self._conflicting_assertions(series_id, document_id)
            status = "pending" if conflicts else "active"
            self.db.execute("INSERT INTO document_series(id,title,current_version) VALUES(?,?,?)",
                            (series_id, title, 0 if conflicts else 1))
            self.db.execute("""INSERT INTO document_versions(series_id,version,document_id,base_version,status)
                VALUES(?,1,?,0,?)""", (series_id, document_id, status))
            if conflicts:
                self._record_conflicts(series_id, 1, conflicts)
            else:
                self.db.execute("UPDATE assertions SET status='accepted' WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)", (document_id,))
            self.db.commit()
            return {**result, "series_id": series_id, "version": 1, "status": status, "conflicts": conflicts}
        except Exception:
            self.db.rollback()
            raise

    def update_document(self, series_id, base_version, title, body, facts, note=""):
        if not isinstance(base_version, int) or base_version < 1:
            raise ValueError("base_version must be a positive integer")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            series = self.db.execute("SELECT * FROM document_series WHERE id=?", (series_id,)).fetchone()
            if not series:
                raise ValueError("Document series not found")
            if series["current_version"] != base_version:
                raise VersionConflict(series["current_version"])
            if self.db.execute("SELECT 1 FROM document_versions WHERE series_id=? AND status='pending'", (series_id,)).fetchone():
                raise ValueError("A pending version must be resolved first")
            version = self.db.execute("SELECT coalesce(max(version),0)+1 FROM document_versions WHERE series_id=?",
                                      (series_id,)).fetchone()[0]
            document_id = digest(f"{series_id}:{version}:{title}:{body}")
            result = self._insert_snapshot(document_id, title, body, facts)
            if result["rejected"]:
                raise ValueError("Update contains unsupported facts: " + json.dumps(result["rejected"], ensure_ascii=False))
            conflicts = self._conflicting_assertions(series_id, document_id)
            status = "pending" if conflicts else "active"
            self.db.execute("""INSERT INTO document_versions(series_id,version,document_id,base_version,status,note)
                VALUES(?,?,?,?,?,?)""", (series_id, version, document_id, base_version, status, note.strip()))
            if conflicts:
                self._record_conflicts(series_id, version, conflicts)
            else:
                self._activate_version(series_id, version)
            self.db.commit()
            return {**result, "series_id": series_id, "version": version, "status": status, "conflicts": conflicts}
        except Exception:
            self.db.rollback()
            raise

    def _activate_version(self, series_id, version):
        row = self.db.execute("SELECT document_id FROM document_versions WHERE series_id=? AND version=?",
                              (series_id, version)).fetchone()
        self.db.execute("UPDATE document_versions SET status='superseded' WHERE series_id=? AND status='active'", (series_id,))
        self.db.execute("UPDATE document_versions SET status='active' WHERE series_id=? AND version=?", (series_id, version))
        self.db.execute("UPDATE document_series SET current_version=?,title=(SELECT title FROM documents WHERE id=?) WHERE id=?",
                        (version, row["document_id"], series_id))
        self.db.execute("UPDATE assertions SET status='accepted' WHERE status='pending' AND chunk_id IN (SELECT id FROM chunks WHERE document_id=?)",
                        (row["document_id"],))

    def resolve_version(self, series_id, version, action):
        if action not in ("replace", "coexist", "reject"):
            raise ValueError("action must be replace, coexist, or reject")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            proposal = self.db.execute("""SELECT v.*,s.current_version FROM document_versions v
                JOIN document_series s ON s.id=v.series_id WHERE v.series_id=? AND v.version=? AND v.status='pending'""",
                (series_id, version)).fetchone()
            if not proposal:
                raise ValueError("Pending version not found")
            if proposal["base_version"] != proposal["current_version"]:
                raise VersionConflict(proposal["current_version"])
            conflicts = self.db.execute("SELECT * FROM version_conflicts WHERE series_id=? AND version=?",
                                        (series_id, version)).fetchall()
            if action != "reject":
                fresh = self._conflicting_assertions(series_id, proposal["document_id"])
                recorded_pairs = {(item["old_assertion_id"], item["new_assertion_id"]) for item in conflicts}
                fresh_pairs = {(item["old_id"], item["new_id"]) for item in fresh}
                if fresh_pairs != recorded_pairs:
                    self.db.execute("DELETE FROM version_conflicts WHERE series_id=? AND version=?", (series_id, version))
                    self._record_conflicts(series_id, version, fresh)
                    if fresh:
                        self.db.commit()
                        return {"series_id": series_id, "version": version, "status": "pending",
                                "resolution": "refreshed", "conflicts": fresh}
                    self._activate_version(series_id, version)
                    self.db.commit()
                    return {"series_id": series_id, "version": version, "status": "active",
                            "resolution": "no_longer_conflicting"}
            if action == "reject":
                self.db.execute("UPDATE document_versions SET status='rejected' WHERE series_id=? AND version=?", (series_id, version))
                self.db.execute("UPDATE assertions SET status='rejected' WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)",
                                (proposal["document_id"],))
            else:
                self._activate_version(series_id, version)
                for item in conflicts:
                    self.db.execute("UPDATE assertions SET status=? WHERE id=?",
                                    ("superseded" if action == "replace" else "contested", item["old_assertion_id"]))
                    if action == "coexist":
                        self.db.execute("UPDATE assertions SET status='contested' WHERE id=?", (item["new_assertion_id"],))
            self.db.execute("UPDATE version_conflicts SET resolution=? WHERE series_id=? AND version=?",
                            (action, series_id, version))
            self.db.commit()
            return {"series_id": series_id, "version": version, "status": "rejected" if action == "reject" else "active",
                    "resolution": action}
        except Exception:
            self.db.rollback()
            raise

    def pending_versions(self):
        rows = self.db.execute("""SELECT v.series_id,v.version,v.base_version,v.document_id,d.title,d.body,v.note,v.created_at
            FROM document_versions v JOIN documents d ON d.id=v.document_id
            WHERE v.status='pending' ORDER BY v.created_at DESC""").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["conflicts"] = [dict(conflict) for conflict in self.db.execute("""SELECT c.id,
                s.name AS subject,n.predicate,o1.name AS old_object,o2.name AS new_object,
                o.quote AS old_quote,n.quote AS new_quote,o.chunk_id AS old_chunk_id,n.chunk_id AS new_chunk_id
                FROM version_conflicts c JOIN assertions o ON o.id=c.old_assertion_id
                JOIN assertions n ON n.id=c.new_assertion_id JOIN entities s ON s.id=n.subject_id
                JOIN entities o1 ON o1.id=o.object_id JOIN entities o2 ON o2.id=n.object_id
                WHERE c.series_id=? AND c.version=?""", (row["series_id"], row["version"]))]
            result.append(item)
        return result

    def versions(self, series_id):
        return [dict(row) for row in self.db.execute("""SELECT v.version,v.document_id,v.base_version,v.status,v.note,v.created_at
            FROM document_versions v WHERE v.series_id=? ORDER BY v.version DESC""", (series_id,))]

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
        return {
            "documents": self.db.execute("SELECT count(*) FROM document_versions WHERE status='active'").fetchone()[0],
            "chunks": self.db.execute("""SELECT count(*) FROM chunks c JOIN document_versions v
                ON v.document_id=c.document_id AND v.status='active'""").fetchone()[0],
            "entities": self.db.execute("""SELECT count(DISTINCT e.id) FROM entities e
                JOIN assertions a ON a.subject_id=e.id OR a.object_id=e.id
                JOIN chunks c ON c.id=a.chunk_id JOIN document_versions v
                ON v.document_id=c.document_id AND v.status='active'
                WHERE a.status IN ('accepted','contested')""").fetchone()[0],
            "assertions": self.db.execute("""SELECT count(*) FROM assertions a JOIN chunks c ON c.id=a.chunk_id
                JOIN document_versions v ON v.document_id=c.document_id AND v.status='active'
                WHERE a.status IN ('accepted','contested')""").fetchone()[0],
            "pending_versions": self.db.execute("SELECT count(*) FROM document_versions WHERE status='pending'").fetchone()[0],
        }

    def list_documents(self, query="", limit=25, offset=0):
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        pattern = f"%{query.strip()}%"
        total = self.db.execute("""SELECT count(*) FROM documents d JOIN document_versions v
            ON v.document_id=d.id AND v.status='active' WHERE d.title LIKE ? OR d.body LIKE ?""",
                                (pattern, pattern)).fetchone()[0]
        rows = self.db.execute("""SELECT d.id,d.title,d.created_at,substr(d.body,1,180) AS preview,
            v.series_id,v.version,
            count(DISTINCT c.id) AS chunk_count,count(DISTINCT a.id) AS assertion_count
            FROM documents d JOIN document_versions v ON v.document_id=d.id AND v.status='active'
            LEFT JOIN chunks c ON c.document_id=d.id
            LEFT JOIN assertions a ON a.chunk_id=c.id
            WHERE d.title LIKE ? OR d.body LIKE ?
            GROUP BY d.id ORDER BY d.created_at DESC,d.id LIMIT ? OFFSET ?""",
            (pattern, pattern, limit, offset)).fetchall()
        return {"total": total, "items": [dict(row) for row in rows], "limit": limit, "offset": offset}

    def document(self, document_id):
        row = self.db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        version = self.db.execute("""SELECT series_id,version,status,base_version,note FROM document_versions
            WHERE document_id=?""", (document_id,)).fetchone()
        result.update(dict(version) if version else {})
        result["chunks"] = [dict(chunk) for chunk in self.db.execute(
            "SELECT id,ordinal,body FROM chunks WHERE document_id=? ORDER BY ordinal", (document_id,))]
        result["assertions"] = [dict(item) for item in self.db.execute("""SELECT a.id,a.predicate,a.quote,a.confidence,a.status,
            a.chunk_id,s.name AS subject,s.type AS subject_type,o.name AS object,o.type AS object_type FROM assertions a
            JOIN chunks c ON c.id=a.chunk_id JOIN entities s ON s.id=a.subject_id
            JOIN entities o ON o.id=a.object_id WHERE c.document_id=? ORDER BY c.ordinal,a.id""",
            (document_id,))]
        return result

    def graph(self, query="", limit=150):
        limit = max(1, min(int(limit), 300))
        query = query.strip()
        if query:
            pattern = f"%{query}%"
            rows = self.db.execute("""SELECT a.id,a.subject_id,a.object_id,a.predicate,a.quote,a.confidence,a.status,
                a.chunk_id,d.id AS document_id,d.title AS document_title
                FROM assertions a JOIN chunks c ON c.id=a.chunk_id JOIN documents d ON d.id=c.document_id
                JOIN document_versions v ON v.document_id=d.id AND v.status='active'
                JOIN entities s ON s.id=a.subject_id JOIN entities o ON o.id=a.object_id
                WHERE a.status IN ('accepted','contested') AND (s.name LIKE ? OR o.name LIKE ? OR a.predicate LIKE ?)
                ORDER BY a.id LIMIT ?""", (pattern, pattern, pattern, limit + 1)).fetchall()
        else:
            rows = self.db.execute("""SELECT a.id,a.subject_id,a.object_id,a.predicate,a.quote,a.confidence,a.status,
                a.chunk_id,d.id AS document_id,d.title AS document_title
                FROM assertions a JOIN chunks c ON c.id=a.chunk_id JOIN documents d ON d.id=c.document_id
                JOIN document_versions v ON v.document_id=d.id AND v.status='active'
                WHERE a.status IN ('accepted','contested') ORDER BY a.id LIMIT ?""", (limit + 1,)).fetchall()
        truncated = len(rows) > limit
        edges = [dict(row) for row in rows[:limit]]
        ids = {entity_id for edge in edges for entity_id in (edge["subject_id"], edge["object_id"])}
        nodes = [dict(row) for row in self.db.execute("SELECT id,name,type FROM entities") if row["id"] in ids]
        nodes.sort(key=lambda item: (item["type"], item["name"]))
        return {"nodes": nodes, "edges": edges, "truncated": truncated, "limit": limit}

    def version_diff(self, series_id, from_version, to_version):
        versions = {}
        for version in (int(from_version), int(to_version)):
            row = self.db.execute("""SELECT d.id,d.body FROM document_versions v JOIN documents d ON d.id=v.document_id
                WHERE v.series_id=? AND v.version=?""", (series_id, version)).fetchone()
            if not row:
                raise ValueError(f"Version {version} not found")
            versions[version] = self.document(row["id"])
        before, after = versions[int(from_version)], versions[int(to_version)]
        text_diff = "".join(unified_diff([line + "\n" for line in before["body"].splitlines()],
                                         [line + "\n" for line in after["body"].splitlines()],
                                         fromfile=f"v{from_version}", tofile=f"v{to_version}"))
        def facts(document):
            return {f"{item['subject']} — {item['predicate']} → {item['object']}" for item in document["assertions"]}
        return {"from": int(from_version), "to": int(to_version), "text_diff": text_diff,
                "added_facts": sorted(facts(after) - facts(before)),
                "removed_facts": sorted(facts(before) - facts(after))}
