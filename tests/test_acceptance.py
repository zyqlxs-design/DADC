from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.demo import create_demo_repository  # noqa: E402
from dadc.integrity import sha256_file  # noqa: E402
from dadc.migration import migrate_record  # noqa: E402
from dadc.repository import DADCRepository  # noqa: E402


class DADCV10AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="dadc-v1-acceptance-")
        cls.demo_root = create_demo_repository(Path(cls._temporary.name) / "repository")
        cls.repository = DADCRepository(cls.demo_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_01_antenna_and_filter_use_same_core_structure(self) -> None:
        antenna = self.repository.get("Device", "device_ant_001")
        rf_filter = self.repository.get("Device", "device_filter_001")
        required_device_fields = {
            "entity_type", "schema_version", "device_id", "name", "device_class",
            "device_subtype", "physics_domains", "profile_schema", "extensions",
        }
        self.assertTrue(required_device_fields.issubset(antenna))
        self.assertTrue(required_device_fields.issubset(rf_filter))
        self.assertEqual([], self.repository.schemas.validate_record(antenna))
        self.assertEqual([], self.repository.schemas.validate_record(rf_filter))
        self.assertEqual({"antenna"}, set(antenna["extensions"]))
        self.assertEqual({"rf_filter"}, set(rf_filter["extensions"]))

        antenna_s = self.repository.get("Observable", "obs_ant_s_complex")
        filter_s = self.repository.get("Observable", "obs_filter_s_complex")
        self.assertEqual(set(antenna_s), set(filter_s))
        self.assertEqual("real_imaginary", antenna_s["complex_representation"])
        self.assertEqual("real_imaginary", filter_s["complex_representation"])

    def test_02_optional_fields_do_not_create_sparse_null_rows(self) -> None:
        statistics = self.repository.null_statistics()
        self.assertLess(statistics["ratio"], 0.01, statistics)

        null_locations: list[str] = []

        def find_nulls(value: object, path: str) -> None:
            if value is None:
                null_locations.append(path)
            elif isinstance(value, dict):
                for key, nested in value.items():
                    find_nulls(nested, f"{path}/{key}")
            elif isinstance(value, list):
                for index, nested in enumerate(value):
                    find_nulls(nested, f"{path}/{index}")

        for entity_type, records in self.repository.record_map.items():
            if entity_type.startswith("_"):
                continue
            for identifier, record in records.items():
                find_nulls(record, f"{entity_type}/{identifier}")
        self.assertTrue(null_locations)
        self.assertTrue(all(path.endswith("/coordinate_system_ref") for path in null_locations), null_locations)

    def test_03_new_device_profile_does_not_modify_existing_schema_or_data(self) -> None:
        core_schema = self.demo_root / "schemas" / "v1.0" / "device.schema.json"
        before_core = sha256_file(core_schema)
        before_json = {
            path.relative_to(self.demo_root).as_posix(): sha256_file(path)
            for path in self.demo_root.glob("cases/*/metadata/**/*.json")
        }
        profile_path = Path(self._temporary.name) / "acoustic_resonator.schema.json"
        profile_path.write_text(
            json.dumps({
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["acoustic_resonator"],
                "properties": {
                    "acoustic_resonator": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["mode_family"],
                        "properties": {"mode_family": {"type": "string"}},
                    }
                },
            }),
            encoding="utf-8",
        )
        profile_ref = "device_profiles/acoustic_resonator.schema.json"
        self.repository.schemas.register_device_profile(profile_ref, profile_path)
        new_device = {
            "entity_type": "Device", "schema_version": "1.0", "device_id": "device_acoustic_001",
            "name": "Acoustic fixture", "device_class": "acoustic_resonator",
            "device_subtype": "bulk_acoustic_resonator", "physics_domains": ["acoustics"],
            "profile_schema": profile_ref, "extensions": {"acoustic_resonator": {"mode_family": "longitudinal"}},
        }
        self.assertEqual([], self.repository.schemas.validate_record(new_device))
        self.assertEqual(before_core, sha256_file(core_schema))
        after_json = {
            path.relative_to(self.demo_root).as_posix(): sha256_file(path)
            for path in self.demo_root.glob("cases/*/metadata/**/*.json")
        }
        self.assertEqual(before_json, after_json)

    def test_04_metric_traces_to_original_complex_result(self) -> None:
        trace = self.repository.trace_metric("metric_ant_resonance")
        observable_ids = {record["observable_id"] for record in trace["observables"]}
        self.assertIn("obs_ant_s_complex", observable_ids)
        artifacts = {record["artifact_id"]: record for record in trace["artifacts"]}
        raw_result = artifacts["art_ant_results_h5"]
        self.assertEqual("raw_solver_output", raw_result["value_origin"])
        self.assertEqual(raw_result["sha256"], sha256_file(self.demo_root / raw_result["relative_path"]))
        self.assertEqual({"run_ant_sim_001"}, {record["run_id"] for record in trace["runs"]})

    def test_05_raw_calculated_and_manual_values_are_distinguishable(self) -> None:
        origins = {
            record["value_origin"]
            for entity_type in ("Observable", "Metric", "Artifact")
            for record in self.repository.records(entity_type)
        }
        self.assertTrue({"raw_solver_output", "calculated", "manual_entry"}.issubset(origins))
        calculated = self.repository.get("Observable", "obs_ant_s11_db")
        manual = self.repository.get("Metric", "metric_ant_visual_review")
        raw = self.repository.get("Observable", "obs_ant_s_complex")
        self.assertIn("derived_from_observable_ids", calculated)
        self.assertIn("derivation", calculated)
        self.assertIn("manual_entry_context", manual)
        self.assertNotIn("derivation", raw)

    def test_06_failed_run_is_preserved_and_retry_links_to_parent(self) -> None:
        failed = self.repository.get("Run", "run_filter_failed")
        retry = self.repository.get("Run", "run_filter_success")
        self.assertEqual("failed", failed["status"])
        self.assertEqual("E_MESH_042", failed["failure"]["error_code"])
        self.assertEqual(failed["run_id"], retry["parent_run_id"])
        log = self.repository.get("Artifact", failed["failure"]["log_artifact_id"])
        self.assertTrue((self.demo_root / log["relative_path"]).is_file())

    def test_07_multiphysics_coupling_is_explicit(self) -> None:
        device = self.repository.get("Device", "device_multi_001")
        study = self.repository.get("Study", "study_multi_coupled")
        self.assertEqual(
            {"electromagnetics", "thermal", "structural"},
            set(device["physics_domains"]),
        )
        self.assertEqual(2, len(study["coupling_edges"]))
        self.assertEqual(
            [("run_multi_em", "run_multi_thermal"), ("run_multi_thermal", "run_multi_structural")],
            [(edge["source_run_id"], edge["target_run_id"]) for edge in study["coupling_edges"]],
        )
        for observable_id in ("obs_multi_em_loss", "obs_multi_temperature", "obs_multi_displacement"):
            field = self.repository.get("Observable", observable_id)
            metadata = field["field_metadata"]
            self.assertTrue({"coordinate_system_ref", "coordinate_unit", "mesh_type", "components", "condition", "data_ref", "normalization"}.issubset(metadata))

    def test_08_v09_run_migrates_to_v10_without_mutating_source(self) -> None:
        v10 = copy.deepcopy(self.repository.get("Run", "run_filter_success"))
        v09 = copy.deepcopy(v10)
        v09["schema_version"] = "0.9"
        v09["run_type"] = "simulation"
        del v09["activity_type"]
        v09["status"] = "completed"
        v09["parent_id"] = v09.pop("parent_run_id")
        original = copy.deepcopy(v09)
        migrated = migrate_record(v09, "1.0")
        self.assertEqual(original, v09)
        self.assertEqual("simulation_run", migrated["activity_type"])
        self.assertEqual("succeeded", migrated["status"])
        self.assertEqual("run_filter_failed", migrated["parent_run_id"])
        self.assertEqual([], self.repository.schemas.validate_record(migrated))

    def test_09_deleted_or_tampered_artifact_fails_integrity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dadc-tamper-") as temporary:
            tampered_root = Path(temporary) / "repository"
            shutil.copytree(self.demo_root, tampered_root)
            target = tampered_root / "cases" / "antenna_patch_case" / "raw" / "antenna_model.py"
            target.write_text(target.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
            report = DADCRepository(tampered_root).validate()
            self.assertFalse(report.valid)
            self.assertTrue(any(issue.category == "integrity" and "mismatch" in issue.message for issue in report.issues))

        with tempfile.TemporaryDirectory(prefix="dadc-delete-") as temporary:
            deleted_root = Path(temporary) / "repository"
            shutil.copytree(self.demo_root, deleted_root)
            target = deleted_root / "cases" / "microstrip_filter_case" / "logs" / "run_failed.log"
            target.unlink()
            report = DADCRepository(deleted_root).validate()
            self.assertFalse(report.valid)
            self.assertTrue(any(issue.category == "integrity" and "missing file" in issue.message for issue in report.issues))

    def test_10_all_generated_examples_pass_schema_python_and_storage_checks(self) -> None:
        report = self.repository.validate()
        self.assertTrue(report.valid, json.dumps(report.to_dict(), indent=2))
        self.assertGreaterEqual(report.checked_records, 40)
        self.assertGreaterEqual(report.checked_artifacts, 15)
        self.assertGreaterEqual(report.checked_data_refs, 15)
        rows = self.repository.query_metrics(value_origin="calculated")
        self.assertGreaterEqual(len(rows), 3)
        self.assertTrue(all(row["source_observable_ids_json"].startswith("[") for row in rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)

