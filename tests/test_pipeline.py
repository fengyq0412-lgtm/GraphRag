import json
import tempfile
import unittest
from pathlib import Path

from graphrag_lite.pipeline import answer
from graphrag_lite.store import Store, VersionConflict


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "graph.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_evidence_gate_and_multihop_citations(self):
        corpus = json.loads((Path(__file__).parent.parent / "evals" / "corpus.json").read_text())
        for item in corpus:
            self.store.ingest(item["title"], item["text"], item["facts"])
        with self.assertRaises(ValueError):
            self.store.ingest("坏证据", "甲负责项目。", [{"subject": "甲", "predicate": "负责", "object": "项目", "quote": "不存在的原文"}])
        self.assertEqual(self.store.stats()["documents"], len(corpus) - 1)
        result = answer(self.store, "星图项目的测试负责人属于哪个团队？请给出关系路径。")
        self.assertEqual(result["route"], "hybrid")
        self.assertTrue(any(path["hops"] >= 2 for path in result["paths"]))
        self.assertTrue(set(result["citations"]) <= {source["id"] for source in result["sources"]})

    def test_ambiguous_entity_requires_review(self):
        self.store.ingest("a", "质量保障团队负责测试。", [{"subject": "质量保障团队", "subject_type": "team", "predicate": "负责", "object": "测试", "quote": "质量保障团队负责测试。"}])
        self.store.ingest("b", "质量保证团队负责验证。", [{"subject": "质量保证团队", "subject_type": "team", "predicate": "负责", "object": "验证", "quote": "质量保证团队负责验证。"}])
        pending = self.store.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.store.stats()["entities"], 4)
        self.store.resolve(pending[0]["id"], "merge")
        self.assertEqual(self.store.stats()["entities"], 3)
        self.assertEqual(len(self.store.pending()), 0)

    def test_abstain_without_evidence(self):
        result = answer(self.store, "量子海豚在哪里？")
        self.assertEqual(result["answer_method"], "abstain")
        self.assertEqual(result["citations"], [])

    def test_graph_and_document_browsing(self):
        corpus = json.loads((Path(__file__).parent.parent / "evals" / "corpus.json").read_text())
        ids = [self.store.ingest(item["title"], item["text"], item["facts"])["document_id"] for item in corpus]
        listing = self.store.list_documents("团队", limit=2)
        self.assertGreaterEqual(listing["total"], 3)
        self.assertEqual(len(listing["items"]), 2)
        document = self.store.document(ids[0])
        self.assertEqual(document["title"], "星图项目职责")
        self.assertEqual(len(document["chunks"]), 1)
        self.assertEqual(len(document["assertions"]), 2)
        self.assertIsNone(self.store.document("missing"))
        graph = self.store.graph("测试负责人")
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(len(graph["nodes"]), 2)
        self.assertTrue(all(edge["document_id"] == ids[0] for edge in graph["edges"]))
        self.assertEqual(len(self.store.graph(limit=1)["edges"]), 1)
        self.assertTrue(self.store.graph(limit=1)["truncated"])

    def test_version_update_and_stale_write(self):
        first = self.store.ingest("职责", "星图项目的负责人是李明。", [
            {"subject": "星图项目", "subject_type": "project", "predicate": "负责人", "object": "李明",
             "object_type": "person", "quote": "星图项目的负责人是李明。"}])
        updated = self.store.update_document(first["series_id"], 1, "职责", "星图项目的负责人是王芳。", [
            {"subject": "星图项目", "subject_type": "project", "predicate": "负责人", "object": "王芳",
             "object_type": "person", "quote": "星图项目的负责人是王芳。"}], "负责人调整")
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["version"], 2)
        self.assertEqual([v["status"] for v in self.store.versions(first["series_id"])], ["active", "superseded"])
        self.assertEqual(self.store.list_documents()["total"], 1)
        self.assertEqual(self.store.list_documents()["items"][0]["version"], 2)
        self.assertEqual(len(self.store.graph()["edges"]), 1)
        self.assertEqual(self.store.document(first["document_id"])["status"], "superseded")
        diff = self.store.version_diff(first["series_id"], 1, 2)
        self.assertIn("王芳", diff["text_diff"])
        self.assertIn("星图项目 — 负责人 → 王芳", diff["added_facts"])
        with self.assertRaises(VersionConflict):
            self.store.update_document(first["series_id"], 1, "职责", "再次修改", [])

    def test_conflict_review_replace_and_coexist(self):
        fact = {"subject": "星图项目", "subject_type": "project", "predicate": "负责人", "object": "李明",
                "object_type": "person", "quote": "星图项目的负责人是李明。"}
        first = self.store.ingest("职责甲", "星图项目的负责人是李明。", [fact])
        second = self.store.ingest("职责乙", "星图项目的负责人是李明。另据团队记录。", [fact])
        proposal = self.store.update_document(first["series_id"], 1, "职责甲", "星图项目的负责人是王芳。", [
            {**fact, "object": "王芳", "quote": "星图项目的负责人是王芳。"}])
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(self.store.stats()["documents"], 2)
        self.assertEqual(len(self.store.pending_versions()), 1)
        self.assertEqual(self.store.document(first["document_id"])["status"], "active")
        self.store.resolve_version(first["series_id"], 2, "coexist")
        self.assertEqual(self.store.document(proposal["document_id"])["status"], "active")
        self.assertEqual(len([e for e in self.store.graph()["edges"] if e["status"] == "contested"]), 2)
        self.assertTrue(answer(self.store, "星图项目的负责人是谁？")["has_conflicts"])

        # A later update can explicitly replace the remaining conflicting claim.
        next_proposal = self.store.update_document(second["series_id"], 1, "职责乙", "星图项目的负责人是张宁。", [
            {**fact, "object": "张宁", "quote": "星图项目的负责人是张宁。"}])
        self.assertEqual(next_proposal["status"], "pending")
        self.store.resolve_version(second["series_id"], 2, "replace")
        self.assertEqual(self.store.stats()["pending_versions"], 0)

    def test_conflicting_new_document_can_be_rejected(self):
        self.store.ingest("原始", "系统由甲维护。", [{"subject":"系统","predicate":"维护者",
            "object":"甲","quote":"系统由甲维护。"}])
        pending = self.store.ingest("提案", "系统由乙维护。", [{"subject":"系统","predicate":"维护者",
            "object":"乙","quote":"系统由乙维护。"}])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(self.store.list_documents()["total"], 2)
        self.assertEqual(self.store.list_documents()["items"][0]["status"], "pending")
        self.assertFalse(any(node["name"] == "乙" for node in self.store.graph()["nodes"]))
        self.store.resolve_version(pending["series_id"], 1, "reject")
        self.assertEqual(self.store.document(pending["document_id"])["status"], "rejected")
        self.assertEqual(self.store.list_documents()["total"], 1)
        self.assertEqual(len(self.store.graph()["edges"]), 1)

    def test_pending_conflict_is_rechecked_before_resolution(self):
        original = self.store.ingest("原始", "系统由甲维护。", [{"subject":"系统","predicate":"维护者",
            "object":"甲","quote":"系统由甲维护。"}])
        pending = self.store.ingest("提案", "系统由乙维护。", [{"subject":"系统","predicate":"维护者",
            "object":"乙","quote":"系统由乙维护。"}])
        self.store.update_document(original["series_id"], 1, "原始", "系统由乙维护。", [
            {"subject":"系统","predicate":"维护者","object":"乙","quote":"系统由乙维护。"}])
        result = self.store.resolve_version(pending["series_id"], 1, "replace")
        self.assertEqual(result["resolution"], "no_longer_conflicting")
        self.assertEqual(self.store.list_documents()["total"], 2)

    def test_replaced_relation_is_hidden_from_text_answer(self):
        self.store.ingest("项目职责", "星图项目的测试负责人是李明。星图项目由平台团队维护。", [
            {"subject":"星图项目","subject_type":"project","predicate":"测试负责人",
             "object":"李明","object_type":"person","quote":"星图项目的测试负责人是李明。"},
            {"subject":"星图项目","subject_type":"project","predicate":"维护团队",
             "object":"平台团队","object_type":"team","quote":"星图项目由平台团队维护。"}])
        proposal = self.store.ingest("负责人变更", "星图项目的测试负责人是王芳。", [
            {"subject":"星图项目","subject_type":"project","predicate":"测试负责人",
             "object":"王芳","object_type":"person","quote":"星图项目的测试负责人是王芳。"}])
        self.store.resolve_version(proposal["series_id"], 1, "replace")
        result = answer(self.store, "星图项目的测试负责人是谁？")
        self.assertNotIn("星图项目的测试负责人是李明。", result["answer"])
        self.assertTrue(all("星图项目的测试负责人是李明。" not in source["text"]
                            for source in result["sources"]))
        self.assertIn("王芳", result["answer"])
        self.assertIn("星图项目由平台团队维护。", " ".join(source["text"] for source in result["sources"]))

    def test_reversed_model_fact_is_canonicalized_and_reviewed(self):
        self.store.ingest("现任负责人", "星图项目的测试负责人是王芳。", [
            {"subject": "星图项目", "subject_type": "project", "predicate": "测试负责人",
             "object": "王芳", "object_type": "person", "quote": "星图项目的测试负责人是王芳。"}])
        proposal = self.store.ingest("新职责", "星图项目的测试负责人是李明。", [
            {"subject": "李明", "subject_type": "Person", "predicate": "is_test_lead_of",
             "object": "星图项目", "object_type": "Project", "quote": "星图项目的测试负责人是李明。"}])
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(proposal["assertions"], 1)
        self.assertFalse(any(edge["document_id"] == proposal["document_id"]
                             for edge in self.store.graph()["edges"]))
        self.assertEqual(self.store.pending_versions()[0]["conflicts"][0]["predicate"], "测试负责人")

    def test_update_rejects_quote_left_over_from_old_body(self):
        old = self.store.ingest("职责", "星图项目的测试负责人是李明。", [
            {"subject": "星图项目", "predicate": "测试负责人", "object": "李明",
             "quote": "星图项目的测试负责人是李明。"}])
        with self.assertRaisesRegex(ValueError, "引文.*不在当前版本"):
            self.store.update_document(old["series_id"], 1, "职责", "星图项目的测试负责人是王芳。", [
                {"subject": "星图项目", "predicate": "测试负责人", "object": "李明",
                 "quote": "星图项目的测试负责人是李明。"}])
        self.assertEqual(len(self.store.versions(old["series_id"])), 1)


if __name__ == "__main__":
    unittest.main()
