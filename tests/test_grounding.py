import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graphrag_lite import model
from graphrag_lite.grounding import parse_claims, verify_generated
from graphrag_lite.pipeline import answer
from graphrag_lite.store import Store


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "graph.sqlite")
        self.store.ingest("项目职责", "星图项目的测试负责人是王芳。", [
            {"subject": "星图项目", "subject_type": "project", "predicate": "测试负责人",
             "object": "王芳", "object_type": "person", "quote": "星图项目的测试负责人是王芳。"}])
        self.source = self.store.graph()["edges"][0]["chunk_id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_each_claim_needs_its_own_valid_citation(self):
        sources = [{"id": self.source, "text": "星图项目的测试负责人是王芳。"}]
        self.assertIsNone(parse_claims("星图项目的测试负责人是李明。", sources))
        self.assertIsNone(parse_claims(f"星图项目的测试负责人是王芳。 [{self.source}]\n李明也负责。", sources))
        self.assertIsNone(parse_claims(f"王芳负责。李明也负责。 [{self.source}]", sources))
        self.assertIsNone(parse_claims("王芳负责。 [ffffffffffffffff:0]", sources))

    def test_verifier_rejects_missing_or_fabricated_support_spans(self):
        sources = [{"id": self.source, "text": "星图项目的测试负责人是王芳。"}]
        claim = f"星图项目的测试负责人是李明。 [{self.source}]"
        with patch.object(model, "chat", return_value=json.dumps({"verdicts": [
                {"index": 0, "supported": False, "quotes": []}]})):
            self.assertEqual(verify_generated(claim, sources)["status"], "rejected")
        with patch.object(model, "chat", return_value=json.dumps({"verdicts": [
                {"index": 0, "supported": True, "quotes": [{"id": self.source, "text": "李明"}]}]})):
            self.assertEqual(verify_generated(claim, sources)["status"], "rejected")

    def test_multihop_requires_spans_from_each_cited_chunk(self):
        second = "bbbbbbbbbbbbbbbb:0"
        sources = [{"id": self.source, "text": "星图项目的测试负责人是王芳。"},
                   {"id": second, "text": "王芳属于质量保障团队。"}]
        claim = f"星图项目的测试负责人属于质量保障团队。 [{self.source}] [{second}]"
        partial = {"verdicts": [{"index": 0, "supported": True, "quotes": [
            {"id": self.source, "text": "星图项目的测试负责人是王芳。"}]}]}
        with patch.object(model, "chat", return_value=json.dumps(partial)):
            self.assertEqual(verify_generated(claim, sources)["status"], "rejected")
        complete = {"verdicts": [{"index": 0, "supported": True, "quotes": [
            {"id": self.source, "text": "星图项目的测试负责人是王芳。"},
            {"id": second, "text": "王芳属于质量保障团队。"}]}]}
        with patch.object(model, "chat", return_value=json.dumps(complete)):
            self.assertEqual(verify_generated(claim, sources)["status"], "verified")

    def test_manual_review_set_has_both_labels_and_conflicts(self):
        cases = json.loads((Path(__file__).parent.parent / "evals" / "grounding_review.json").read_text())
        self.assertEqual(len(cases), 8)
        self.assertEqual({case["label"] for case in cases}, {"supported", "unsupported"})
        self.assertTrue(any(case["id"].startswith("conflict-") for case in cases))

    def test_answer_falls_back_when_claim_unsupported_or_verifier_unavailable(self):
        claim = f"星图项目的测试负责人是李明。 [{self.source}]"
        verdict = json.dumps({"verdicts": [{"index": 0, "supported": False, "quotes": []}]})
        with patch.object(model, "available", return_value=True), patch.object(model, "chat", side_effect=[claim, verdict]):
            result = answer(self.store, "星图项目的测试负责人是谁？")
        self.assertEqual(result["answer_method"], "evidence_excerpt")
        self.assertEqual(result["verification"]["status"], "extractive")
        self.assertIn("王芳", result["answer"])
        self.assertNotIn("李明", result["answer"])
        with patch.object(model, "available", return_value=True), patch.object(
                model, "chat", side_effect=[claim, model.ModelError("timeout")]):
            result = answer(self.store, "星图项目的测试负责人是谁？")
        self.assertEqual(result["answer_method"], "evidence_excerpt")

    def test_verified_answer_and_conflict_use_different_paths(self):
        claim = f"星图项目的测试负责人是王芳。 [{self.source}]"
        verdict = json.dumps({"verdicts": [{"index": 0, "supported": True, "quotes": [
            {"id": self.source, "text": "星图项目的测试负责人是王芳。"}]}]})
        with patch.object(model, "available", return_value=True), patch.object(model, "chat", side_effect=[claim, verdict]):
            result = answer(self.store, "星图项目的测试负责人是谁？")
        self.assertEqual(result["answer_method"], "llm")
        self.assertEqual(result["verification"]["status"], "verified")
        self.assertEqual(result["citations"], [self.source])

        pending = self.store.ingest("冲突记载", "星图项目的测试负责人是李明。", [
            {"subject": "星图项目", "subject_type": "project", "predicate": "测试负责人",
             "object": "李明", "object_type": "person", "quote": "星图项目的测试负责人是李明。"}])
        self.store.resolve_version(pending["series_id"], 1, "coexist")
        with patch.object(model, "available", return_value=True), patch.object(model, "chat") as chat:
            result = answer(self.store, "星图项目的测试负责人是谁？")
        chat.assert_not_called()
        self.assertTrue(result["has_conflicts"])
        self.assertIn("冲突", result["answer"])
        self.assertIn("王芳", result["answer"])
        self.assertIn("李明", result["answer"])


if __name__ == "__main__":
    unittest.main()
