from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.cli import main  # noqa: E402
from dadc.ingestion.registry import (  # noqa: E402
    AdapterCapability,
    AdapterRegistry,
    ProbeResult,
)


class _FixedProbeAdapter:
    adapter_version = "1.0.0"

    def __init__(self, adapter_id: str, confidence: float):
        self.adapter_id = adapter_id
        self.confidence = confidence
        self.capability = AdapterCapability(
            adapter_id=adapter_id,
            adapter_version=self.adapter_version,
            source_kinds=("test",),
            source_formats=("test",),
            device_classes=("test",),
            activity_types=("experiment_run",),
            physics_domains=("electromagnetics",),
            automation_level="test_only",
            manifest_required=True,
            required_intake_fields=(),
            maturity="test_only",
        )

    def probe(self, source: Path, intake: dict[str, object]) -> ProbeResult:
        return ProbeResult(self.adapter_id, self.confidence, "fixed test score")


class IngestionPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="dadc-preflight-")
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    @staticmethod
    def _cli(*arguments: str) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(list(arguments))
        return status, json.loads(output.getvalue())

    def test_csv_without_manifest_reports_missing_semantics_without_mutation(self) -> None:
        source = self.root / "instrument.csv"
        source.write_text("time_s,value\n0,1\n1,2\n", encoding="utf-8")
        before = {path.relative_to(self.root) for path in self.root.rglob("*")}

        status, rendered = self._cli("preflight", str(source))

        self.assertEqual(1, status)
        self.assertEqual("needs_metadata", rendered["decision"])
        self.assertEqual("tabular_experiment_csv", rendered["recommended_adapter"])
        candidate = rendered["candidates"][0]
        self.assertEqual("tabular_experiment_csv", candidate["adapter_id"])
        self.assertIn("tabular_contract", candidate["missing_intake_fields"])
        self.assertIn("physics_domains", candidate["missing_intake_fields"])
        self.assertFalse(rendered["guarantees"]["warehouse_mutated"])
        self.assertEqual(before, {path.relative_to(self.root) for path in self.root.rglob("*")})

    def test_complete_manifest_is_ready_but_does_not_claim_full_case_validation(self) -> None:
        source = REPOSITORY_ROOT / "examples" / "intake" / "generic_experiment.csv"
        manifest = (
            REPOSITORY_ROOT
            / "examples"
            / "intake"
            / "generic_experiment_csv.dadc.json"
        )
        source_bytes = source.read_bytes()
        manifest_bytes = manifest.read_bytes()

        status, rendered = self._cli(
            "preflight",
            str(source),
            "--manifest",
            str(manifest),
        )

        self.assertEqual(0, status)
        self.assertEqual("ready", rendered["decision"])
        self.assertEqual("tabular_experiment_csv", rendered["recommended_adapter"])
        self.assertEqual([], rendered["candidates"][0]["missing_intake_fields"])
        self.assertFalse(rendered["guarantees"]["warehouse_mutated"])
        self.assertFalse(rendered["guarantees"]["full_case_validation_performed"])
        self.assertEqual(source_bytes, source.read_bytes())
        self.assertEqual(manifest_bytes, manifest.read_bytes())

    def test_unknown_binary_format_is_reported_as_unsupported(self) -> None:
        source = self.root / "unknown.bin"
        source.write_bytes(b"\x00\x01\x02DADC-unknown")

        status, rendered = self._cli("preflight", str(source))

        self.assertEqual(1, status)
        self.assertEqual("unsupported", rendered["decision"])
        self.assertIsNone(rendered["recommended_adapter"])
        self.assertTrue(all(item["confidence"] == 0.0 for item in rendered["candidates"]))
        self.assertEqual([Path("unknown.bin")], [path.relative_to(self.root) for path in self.root.iterdir()])

    def test_equal_high_confidence_candidates_are_reported_as_ambiguous(self) -> None:
        source = self.root / "ambiguous.dat"
        source.write_text("ambiguous", encoding="utf-8")
        registry = AdapterRegistry(
            adapters=[
                _FixedProbeAdapter("adapter_a", 0.90),
                _FixedProbeAdapter("adapter_b", 0.90),
            ]
        )

        rendered = registry.preflight(source, {})

        self.assertEqual("ambiguous", rendered["decision"])
        self.assertIsNone(rendered["recommended_adapter"])
        self.assertEqual(
            ["adapter_a", "adapter_b"],
            [item["adapter_id"] for item in rendered["candidates"]],
        )
        self.assertFalse(rendered["guarantees"]["warehouse_mutated"])


if __name__ == "__main__":
    unittest.main()
