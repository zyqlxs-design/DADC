from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.integrity import sha256_file  # noqa: E402
from dadc.repository import DADCRepository  # noqa: E402
from dadc.warehouse import WarehouseManager, initialize_data_root  # noqa: E402


FIXTURE = REPOSITORY_ROOT / "tests" / "fixtures" / "bandpass_filter_run_001_HFSSDesign1.s2p"


def _intake(case_id: str) -> dict[str, object]:
    return {
        "intake_schema_version": "1.0",
        "case_id": case_id,
        "device_name": f"Warehouse fixture {case_id}",
        "device_class": "rf_filter",
        "activity_type": "simulation_run",
        "filter_order": 8,
        "source_timezone": "+08:00",
        "operator_id": "test_operator",
        "platform": "test",
        "compute": "test",
        "solver_edition": "Student",
        "processed_at": "2026-08-17T08:10:00Z",
    }


def _antenna_intake(case_id: str) -> dict[str, object]:
    return {
        "intake_schema_version": "1.0",
        "case_id": case_id,
        "device_name": "PyAEDT probe-fed patch antenna",
        "device_class": "antenna",
        "device_subtype": "probe_fed_patch_antenna",
        "activity_type": "simulation_run",
        "feed_type": "coaxial_probe",
        "radiation_mode": "broadside",
        "source_timezone": "+08:00",
        "operator_id": "test_operator",
        "platform": "test",
        "compute": "test",
        "solver_edition": "Student",
        "processed_at": "2026-08-17T08:20:00Z",
    }


class SharedWarehouseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="dadc-warehouse-")
        self.data_root = Path(self._temporary.name) / "DADC_DATA"
        initialized = initialize_data_root(self.data_root)
        self.warehouse = Path(initialized["warehouse"])
        self.manager = WarehouseManager(self.warehouse)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_first_ingestion_creates_shared_warehouse(self) -> None:
        result = self.manager.ingest(FIXTURE, _intake("warehouse_filter_001"))
        self.assertEqual("ingested", result.status, result.to_dict())
        repository = DADCRepository(self.warehouse)
        validation = repository.validate()
        self.assertTrue(validation.valid, validation.to_dict())
        self.assertEqual("dadc_shared_warehouse", repository.manifest["repository_id"])
        self.assertEqual(["warehouse_filter_001"], [case["case_id"] for case in repository.manifest["cases"]])
        registry = json.loads(
            (self.warehouse / "system" / "ingestion_registry.json").read_text(encoding="utf-8")
        )
        self.assertEqual(sha256_file(FIXTURE), registry["records"][0]["source_sha256"])

    def test_renamed_identical_source_is_deduplicated_by_bytes(self) -> None:
        first = self.manager.ingest(FIXTURE, _intake("warehouse_filter_001"))
        self.assertEqual("ingested", first.status)
        renamed = self.data_root / "inbox" / "renamed_copy.s2p"
        shutil.copyfile(FIXTURE, renamed)
        second = self.manager.ingest(renamed, _intake("warehouse_filter_renamed"))
        self.assertEqual("duplicate", second.status, second.to_dict())
        self.assertEqual("warehouse_filter_001", second.duplicate_of_case_id)
        repository = DADCRepository(self.warehouse)
        self.assertEqual(1, len(repository.manifest["cases"]))

    def test_filter_and_antenna_share_core_and_indexes_are_rebuilt(self) -> None:
        first = self.manager.ingest(FIXTURE, _intake("warehouse_filter_001"))
        self.assertEqual("ingested", first.status)
        antenna = self.data_root / "inbox" / "pyaedt_patch.s1p"
        antenna.write_text(
            "! deterministic one-port solver fixture\n"
            "# GHz S RI R 50\n"
            "8 0.5 0\n"
            "10 0.1 0\n"
            "12 0.6 0\n",
            encoding="utf-8",
        )
        native_project = self.data_root / "inbox" / "pyaedt_patch.aedt"
        native_project.write_bytes(b"native AEDT test evidence")
        antenna_intake = _antenna_intake("warehouse_antenna_001")
        antenna_intake["companion_artifacts"] = [
            {
                "path": str(native_project),
                "role": "native_project",
                "media_type": "application/octet-stream",
                "value_origin": "raw_solver_output",
            }
        ]
        second = self.manager.ingest(antenna, antenna_intake)
        self.assertEqual("ingested", second.status, second.to_dict())
        repository = DADCRepository(self.warehouse)
        validation = repository.validate()
        self.assertTrue(validation.valid, validation.to_dict())
        self.assertEqual(
            ["warehouse_antenna_001", "warehouse_filter_001"],
            [case["case_id"] for case in repository.manifest["cases"]],
        )
        self.assertEqual(7, len(repository.query_metrics()))
        self.assertEqual(
            {"antenna", "rf_filter"},
            {device["device_class"] for device in repository.records("Device")},
        )
        self.assertIn("native_project", {item["artifact_role"] for item in repository.records("Artifact")})
        antenna_revision = repository.get("DesignRevision", "rev_warehouse_antenna_001_001")
        self.assertEqual(
            "native_project_preserved_not_structurally_extracted",
            antenna_revision["topology"]["reconstruction_status"],
        )

    def test_schema_formatting_and_windows_line_endings_do_not_create_conflict(self) -> None:
        first = self.manager.ingest(FIXTURE, _intake("warehouse_filter_001"))
        self.assertEqual("ingested", first.status)
        schema_path = self.warehouse / "schemas" / "v1.0" / "artifact.schema.json"
        value = json.loads(schema_path.read_text(encoding="utf-8"))
        windows_rendered = json.dumps(value, ensure_ascii=False, indent=4).replace("\n", "\r\n")
        schema_path.write_bytes(b"\xef\xbb\xbf" + windows_rendered.encode("utf-8"))

        antenna = self.data_root / "inbox" / "line_ending_patch.s1p"
        antenna.write_text(
            "! schema formatting compatibility fixture\n"
            "# GHz S RI R 50\n"
            "8 0.5 0\n"
            "10 0.1 0\n"
            "12 0.6 0\n",
            encoding="utf-8",
        )
        second = self.manager.ingest(antenna, _antenna_intake("warehouse_antenna_001"))
        self.assertEqual("ingested", second.status, second.to_dict())
        self.assertTrue(DADCRepository(self.warehouse).validate().valid)

    def test_semantically_changed_existing_schema_is_still_rejected(self) -> None:
        first = self.manager.ingest(FIXTURE, _intake("warehouse_filter_001"))
        self.assertEqual("ingested", first.status)
        schema_path = self.warehouse / "schemas" / "v1.0" / "artifact.schema.json"
        value = json.loads(schema_path.read_text(encoding="utf-8"))
        value["title"] = "Semantically changed schema fixture"
        schema_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

        antenna = self.data_root / "inbox" / "semantic_conflict_patch.s1p"
        antenna.write_text(
            "! semantic schema conflict fixture\n"
            "# GHz S RI R 50\n"
            "8 0.5 0\n"
            "10 0.1 0\n"
            "12 0.6 0\n",
            encoding="utf-8",
        )
        second = self.manager.ingest(antenna, _antenna_intake("warehouse_antenna_001"))
        self.assertEqual("quarantined", second.status, second.to_dict())
        self.assertIn("Schema conflict", str(second.message))

    def test_id_conflict_and_unknown_format_are_quarantined_without_mutation(self) -> None:
        first = self.manager.ingest(FIXTURE, _intake("warehouse_filter_001"))
        self.assertEqual("ingested", first.status)
        conflicting = self.data_root / "inbox" / "different_bytes.s2p"
        conflicting.write_bytes(b"! changed source\r\n" + FIXTURE.read_bytes())
        conflict_result = self.manager.ingest(conflicting, _intake("warehouse_filter_001"))
        self.assertEqual("quarantined", conflict_result.status, conflict_result.to_dict())
        self.assertTrue(Path(str(conflict_result.quarantine_path)).is_dir())

        unknown = self.data_root / "inbox" / "unknown.bin"
        unknown.write_bytes(b"not an engineering format")
        unknown_result = self.manager.ingest(unknown, _intake("unknown_case_001"))
        self.assertEqual("quarantined", unknown_result.status, unknown_result.to_dict())
        self.assertIn("No source adapter accepted", str(unknown_result.message))

        repository = DADCRepository(self.warehouse)
        validation = repository.validate()
        self.assertTrue(validation.valid, validation.to_dict())
        self.assertEqual(1, len(repository.manifest["cases"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
