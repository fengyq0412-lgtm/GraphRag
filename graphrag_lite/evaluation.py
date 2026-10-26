import json
from pathlib import Path

from .pipeline import answer, retrieve
from .store import Store


def expected_source_titles(store, case):
    chain = case.get("source_assertion_chain")
    if not chain:
        return set(case.get("source_titles", []))
    subjects = {chain[0]["subject"]}
    titles = set()
    for step in chain:
        next_subjects = set()
        for subject in subjects:
            rows = store.db.execute("""SELECT d.title,o.name AS object FROM assertions a
                JOIN entities s ON s.id=a.subject_id JOIN entities o ON o.id=a.object_id
                JOIN chunks c ON c.id=a.chunk_id JOIN documents d ON d.id=c.document_id
                JOIN document_versions v ON v.document_id=d.id AND v.status='active'
                WHERE s.name=? AND a.predicate=? AND a.status IN ('accepted','contested')""",
                (subject, step["predicate"])).fetchall()
            titles.update(row["title"] for row in rows)
            next_subjects.update(row["object"] for row in rows)
        subjects = next_subjects
    return titles


def run(database):
    cases = json.loads((Path(__file__).parent.parent / "evals" / "questions.json").read_text())
    store = Store(database)
    try:
        if not store.stats()["documents"]:
            raise ValueError("Empty database; run seed first")
        details = []
        for case in cases:
            result = answer(store, case["question"])
            source_ids = {s["id"] for s in result["sources"]}
            source_titles = {s["title"] for s in result["sources"]}
            citation_ids = set(result["citations"])
            citation_valid = citation_ids <= source_ids
            expected = expected_source_titles(store, case)
            recall = len(source_titles & expected) / len(expected) if expected else float(not source_titles)
            route_ok = result["route"] == case["route"]
            hops = max((path["hops"] for path in result["paths"]), default=0)
            hop_ok = hops >= case.get("min_hops", 0)
            abstain_ok = (result["answer_method"] == "abstain") == case.get("abstain", False)
            details.append({"id": case["id"], "route": result["route"], "route_ok": route_ok,
                            "source_recall_at_6": recall, "citation_valid": citation_valid,
                            "hop_ok": hop_ok, "abstain_ok": abstain_ok,
                            "answer_method": result["answer_method"]})
        metrics = {key: round(sum(item[key] for item in details) / len(details), 3)
                   for key in ("route_ok", "source_recall_at_6", "citation_valid", "hop_ok", "abstain_ok")}
        return {"metrics": metrics, "cases": details,
                "note": "Answer factuality and evidence entailment need human or independent judge review."}
    finally:
        store.close()
