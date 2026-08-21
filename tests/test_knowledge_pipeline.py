from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.knowledge import build_index, collect_corpus, search_index  # noqa: E402


class KnowledgePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="dadc-knowledge-")
        self.root = Path(self._temporary.name)
        self.fixture = REPOSITORY_ROOT / "tests" / "fixtures" / "pyaedt_api_minimal.html"
        self.manifest = self.root / "sources.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "knowledge_manifest_version": "1.0",
                    "corpus_id": "pyaedt_minimal_fixture",
                    "retrieved_at": "2026-08-21T02:00:00Z",
                    "chunk_max_characters": 512,
                    "documents": [
                        {
                            "source_id": "pyaedt_hfss_fixture",
                            "url": str(self.fixture),
                            "source_type": "local_fixture",
                            "product": "PyAEDT",
                            "product_version": "2025.2-contract-fixture",
                            "license": "repository test fixture",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.corpus = self.root / "corpus"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_raw_bytes_sections_code_and_source_locator_are_preserved(self) -> None:
        result = collect_corpus(self.manifest, self.corpus)
        self.assertEqual(1, result["document_count"])
        self.assertGreaterEqual(result["chunk_count"], 3)
        document = json.loads((self.corpus / "documents.jsonl").read_text(encoding="utf-8"))
        raw = self.corpus / document["raw_artifact"]["relative_path"]
        self.assertEqual(self.fixture.read_bytes(), raw.read_bytes())
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), document["raw_artifact"]["sha256"])
        self.assertIn("code", {section["section_type"] for section in document["sections"]})
        self.assertTrue(all(section["locator"].startswith("section:") for section in document["sections"]))

    def test_index_is_rebuildable_and_search_returns_evidence(self) -> None:
        collect_corpus(self.manifest, self.corpus)
        build_index(self.corpus, dimensions=128)
        first = (self.corpus / "index" / "embeddings.npy").read_bytes()
        build_index(self.corpus, dimensions=128)
        self.assertEqual(first, (self.corpus / "index" / "embeddings.npy").read_bytes())
        results = search_index(self.corpus, "Hfss create_setup frequency sweep", top_k=2)
        self.assertTrue(results)
        self.assertIn("create_setup", results[0]["text"])
        self.assertEqual("pyaedt_hfss_fixture", results[0]["evidence"]["source_id"])
        self.assertIn("content_sha256", results[0]["evidence"])

    def test_changed_chunks_require_projection_rebuild(self) -> None:
        collect_corpus(self.manifest, self.corpus)
        build_index(self.corpus)
        with (self.corpus / "chunks.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(" \n")
        with self.assertRaisesRegex(ValueError, "rebuild"):
            search_index(self.corpus, "create setup")


if __name__ == "__main__":
    unittest.main(verbosity=2)
