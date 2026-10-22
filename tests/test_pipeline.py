import json
import tempfile
import unittest
from pathlib import Path

from graphrag_lite.pipeline import answer
from graphrag_lite.store import Store


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
        rejected = self.store.ingest("坏证据", "甲负责项目。", [{"subject": "甲", "predicate": "负责", "object": "项目", "quote": "不存在的原文"}])
        self.assertEqual(rejected["assertions"], 0)
        self.assertEqual(len(rejected["rejected"]), 1)
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


if __name__ == "__main__":
    unittest.main()
