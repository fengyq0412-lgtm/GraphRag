import json
from pathlib import Path

from . import model
from .grounding import verify_generated
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
            citation_valid = citation_ids <= source_ids and (not source_ids or bool(citation_ids))
            grounding_status = result["verification"]["status"]
            verified_generated = result["answer_method"] == "llm" and grounding_status == "verified"
            expected = expected_source_titles(store, case)
            recall = len(source_titles & expected) / len(expected) if expected else float(not source_titles)
            route_ok = result["route"] == case["route"]
            hops = max((path["hops"] for path in result["paths"]), default=0)
            hop_ok = hops >= case.get("min_hops", 0)
            abstain_ok = (result["answer_method"] == "abstain") == case.get("abstain", False)
            details.append({"id": case["id"], "route": result["route"], "route_ok": route_ok,
                            "source_recall_at_6": recall, "citation_valid": citation_valid,
                            "verified_generated": verified_generated, "grounding_status": grounding_status,
                            "hop_ok": hop_ok, "abstain_ok": abstain_ok,
                            "answer_method": result["answer_method"]})
        metrics = {key: round(sum(item[key] for item in details) / len(details), 3)
                   for key in ("route_ok", "source_recall_at_6", "citation_valid", "verified_generated", "hop_ok", "abstain_ok")}
        return {"metrics": metrics, "cases": details,
                "note": "Grounding gate reports model assessment or extractive fallback; human review remains necessary."}
    finally:
        store.close()


def run_grounding_review():
    """Score the verifier against a small, human-labeled claim/evidence set."""
    if not model.available():
        raise ValueError("Grounding review requires LLM_API_KEY and LLM_MODEL in .env")
    cases = json.loads((Path(__file__).parent.parent / "evals" / "grounding_review.json").read_text())
    details = []
    for case in cases:
        answer_text = case["claim"] + " " + " ".join(f"[{ref}]" for ref in case["citations"])
        outcome = verify_generated(answer_text, case["evidence"])
        predicted = {"verified": "supported", "rejected": "unsupported"}.get(outcome["status"], "unavailable")
        details.append({"id": case["id"], "expected": case["label"], "predicted": predicted,
                        "status": outcome["status"], "correct": predicted == case["label"] if predicted != "unavailable" else None})
    completed = [item for item in details if item["correct"] is not None]
    unsupported = [item for item in completed if item["expected"] == "unsupported"]
    return {"accuracy": round(sum(item["correct"] for item in completed) / len(completed), 3) if completed else None,
            "coverage": round(len(completed) / len(details), 3),
            "false_accept_rate": round(sum(item["predicted"] == "supported" for item in unsupported) / len(unsupported), 3) if unsupported else None,
            "cases": details, "note": "Sample labels are human authored; expand and review disagreements before deployment."}
