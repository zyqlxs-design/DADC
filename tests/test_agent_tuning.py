from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.agent import prepare_agent_plan, run_agent_tuning  # noqa: E402
from dadc.automation import run_optimization, write_optimization_report  # noqa: E402
from dadc.knowledge import build_index, collect_corpus  # noqa: E402
from dadc.warehouse import WarehouseManager, initialize_data_root  # noqa: E402


REQUEST = REPOSITORY_ROOT / "examples" / "agent" / "analytic_fixture_request.json"
PYAEDT_REQUEST = REPOSITORY_ROOT / "examples" / "agent" / "pyaedt_patch_deepseek_request.json"
KNOWLEDGE_MANIFEST = (
    REPOSITORY_ROOT / "examples" / "knowledge" / "device_partition_fixture_sources.json"
)
PLAN = REPOSITORY_ROOT / "examples" / "automation" / "analytic_fixture_plan.json"


class _OutOfBoundsProvider:
    provider_id = "malicious_test_provider"
    model = "test"

    def select(self, context):
        value = {
            "agent_parameter_selection_version": "1.0",
            "selected_values": [
                {"name": "patch_length_mm", "values": [999.0]},
                {"name": "patch_width_mm", "values": [5.0]},
            ],
            "knowledge_chunk_ids": [],
            "rationale": "Attempt an out-of-policy value.",
        }
        return value, {
            "provider_id": self.provider_id,
            "model": self.model,
            "network_call": False,
            "raw_response": value,
        }


class AgentTuningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="dadc-agent-")
        self.root = Path(self._temporary.name)
        self.corpus = self.root / "corpus"
        collect_corpus(KNOWLEDGE_MANIFEST, self.corpus)
        build_index(self.corpus, dimensions=128)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_plan_contains_retrieved_evidence_and_only_allowed_values(self) -> None:
        result = prepare_agent_plan(REQUEST, self.corpus, self.root / "planning")
        self.assertEqual("planned", result["status"])
        self.assertGreater(result["knowledge_chunks_supplied"], 0)
        plan = json.loads(Path(result["plan"]).read_text(encoding="utf-8"))
        values = {item["name"]: item["values"] for item in plan["parameters"]}
        self.assertEqual([10.0, 9.0], values["patch_length_mm"])
        self.assertEqual([5.0, 4.0], values["patch_width_mm"])
        agent_provenance = plan["device"]["attributes"]["agent_planning"]
        self.assertEqual("deterministic_fixture", agent_provenance["provider_id"])
        self.assertTrue(agent_provenance["knowledge_evidence"])
        proposal = json.loads(Path(result["proposal"]).read_text(encoding="utf-8"))
        self.assertTrue(proposal["constraints_checked"])

    def test_out_of_bounds_provider_value_is_rejected_before_plan_or_execution(self) -> None:
        target = self.root / "rejected"
        with patch("dadc.agent.tuning.create_provider", return_value=_OutOfBoundsProvider()):
            with self.assertRaisesRegex(ValueError, "outside allowed_values"):
                prepare_agent_plan(REQUEST, self.corpus, target)
        self.assertFalse((target / "optimization_plan.json").exists())

    def test_execution_requires_explicit_approval(self) -> None:
        with self.assertRaisesRegex(ValueError, "approve_execution"):
            run_agent_tuning(REQUEST, self.corpus, self.root / "not_approved")
        self.assertFalse((self.root / "not_approved").exists())

    def test_physical_execution_rejects_test_only_knowledge_before_backend_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "test-only knowledge"):
            run_agent_tuning(
                PYAEDT_REQUEST,
                self.corpus,
                self.root / "physical_rejected",
                approve_execution=True,
            )
        self.assertFalse((self.root / "physical_rejected" / "optimization").exists())

    def test_agent_loop_executes_verifies_and_computes_acceptance(self) -> None:
        result = run_agent_tuning(
            REQUEST,
            self.corpus,
            self.root / "executed",
            approve_execution=True,
        )
        self.assertEqual("accepted", result["status"])
        self.assertEqual("passed", result["acceptance"]["status"])
        self.assertTrue(result["acceptance"]["checks"]["independent_verification"])
        self.assertEqual(0.25, result["acceptance"]["verified_value"])
        bundle = json.loads(Path(result["optimization"]["bundle"]).read_text(encoding="utf-8"))
        self.assertEqual(4, len(bundle["search_trials"]))
        self.assertEqual(1, len(bundle["verification_trials"]))

    def test_dadc_optimization_history_is_supplied_to_planner(self) -> None:
        previous = run_optimization(PLAN, self.root / "previous")
        paths = initialize_data_root(self.root / "data")
        ingest = WarehouseManager(paths["warehouse"]).ingest(previous["bundle"], {})
        self.assertEqual("ingested", ingest.status)
        result = prepare_agent_plan(
            REQUEST,
            self.corpus,
            self.root / "history_plan",
            warehouse=paths["warehouse"],
        )
        self.assertGreater(result["dadc_history_trials_supplied"], 0)
        context = json.loads(
            (self.root / "history_plan" / "agent_context.json").read_text(encoding="utf-8")
        )
        self.assertTrue(context["dadc_history"]["repository_valid"])
        self.assertTrue(context["dadc_history"]["optimization_trials"])

    def test_optimization_report_lists_search_and_verification_points(self) -> None:
        optimization = run_optimization(PLAN, self.root / "report_source")
        report_path = self.root / "optimization_report.md"
        result = write_optimization_report(optimization["bundle"], report_path)
        rendered = report_path.read_text(encoding="utf-8")
        self.assertEqual("search_004", result["best_search_trial_id"])
        self.assertIn("search_001", rendered)
        self.assertIn("verify_001", rendered)
        self.assertIn("参数点与结果", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
