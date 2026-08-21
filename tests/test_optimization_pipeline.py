from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.automation import run_optimization  # noqa: E402
from dadc.repository import DADCRepository  # noqa: E402
from dadc.warehouse import WarehouseManager, initialize_data_root  # noqa: E402


PLAN = REPOSITORY_ROOT / "examples" / "automation" / "analytic_fixture_plan.json"
CASE_ID = "mvp_optimization_001"


class OptimizationPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="dadc-optimization-")
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _run(self, name: str = "run") -> tuple[dict, Path]:
        output = self.root / name
        result = run_optimization(PLAN, output)
        return result, Path(result["bundle"])

    def test_budgeted_search_selects_and_independently_reruns_best_point(self) -> None:
        result, bundle_path = self._run()
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.assertEqual("search_004", result["best_search_trial_id"])
        self.assertEqual(0.25, result["best_metric"]["value"])
        self.assertEqual(5, len(bundle["search_trials"]))
        self.assertEqual(1, len(bundle["verification_trials"]))
        self.assertEqual(1, sum(item["status"] == "failed" for item in bundle["search_trials"]))
        self.assertEqual(
            bundle["search_trials"][3]["job"]["parameters"],
            bundle["verification_trials"][0]["job"]["parameters"],
        )
        self.assertFalse(bundle["backend"]["is_physical_solver"])
        self.assertEqual("ci_contract_only_non_physical", bundle["backend"]["evidence_level"])

    def test_bundle_ingests_as_one_run_per_backend_call_plus_summary_processing(self) -> None:
        _result, bundle_path = self._run()
        paths = initialize_data_root(self.root / "data")
        ingest = WarehouseManager(paths["warehouse"]).ingest(bundle_path, {})
        self.assertEqual("ingested", ingest.status, ingest.to_dict())
        repository = DADCRepository(paths["warehouse"])
        self.assertTrue(repository.validate().valid)
        runs = repository.records("Run")
        self.assertEqual(6, sum(item["activity_type"] == "optimization_step" for item in runs))
        self.assertEqual(1, sum(item["activity_type"] == "data_processing" for item in runs))
        failed = [item for item in runs if item["status"] == "failed"]
        self.assertEqual(1, len(failed))
        self.assertEqual("simulation_backend_failed", failed[0]["failure"]["error_code"])
        self.assertFalse(any(item["activity_type"] == "simulation_run" for item in runs))
        device = repository.records("Device")[0]
        self.assertIn("non_physical_contract_fixture", device["tags"])
        metric_id = f"metric_{CASE_ID}_verified_objective"
        trace = repository.trace_metric(metric_id)
        artifact_names = {Path(item["relative_path"]).name for item in trace["artifacts"]}
        self.assertIn("optimization_bundle.json", artifact_names)
        self.assertIn("optimization_trace.h5", artifact_names)
        self.assertTrue(any(name == "simulation_result.json" for name in artifact_names))
        validation = repository.records("Validation")[0]
        self.assertIn("non-physical CI fixture", validation["result"]["summary"])

    def test_tampered_companion_is_quarantined_before_warehouse_creation(self) -> None:
        _result, bundle_path = self._run("tampered")
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        companion = bundle_path.parent / bundle["artifacts"][0]["relative_path"]
        companion.write_bytes(companion.read_bytes() + b"tampered")
        paths = initialize_data_root(self.root / "tampered_data")
        ingest = WarehouseManager(paths["warehouse"]).ingest(bundle_path, {})
        self.assertEqual("quarantined", ingest.status, ingest.to_dict())
        self.assertIn("mismatch", str(ingest.message).lower())
        self.assertFalse(Path(paths["warehouse"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
