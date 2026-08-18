from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import h5py

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.integrity import sha256_file  # noqa: E402
from dadc.repository import DADCRepository  # noqa: E402
from dadc.warehouse import WarehouseManager, initialize_data_root  # noqa: E402


def _load_generator():
    path = REPOSITORY_ROOT / "scripts" / "generate_joule_thermal_resistor.py"
    spec = importlib.util.spec_from_file_location("dadc_joule_generator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load generator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class JouleThermalFieldBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="dadc-joule-thermal-")
        cls.root = Path(cls._temporary.name)
        cls.source_dir = cls.root / "source"
        cls.generator = _load_generator()
        cls.bundle = cls.generator.generate(cls.source_dir, operator_id="test_operator")
        initialized = initialize_data_root(cls.root / "DADC_DATA")
        cls.warehouse = Path(initialized["warehouse"])
        cls.result = WarehouseManager(cls.warehouse).ingest(
            cls.bundle,
            {
                "intake_schema_version": "1.0",
                "adapter": "joule_thermal_field_bundle",
                "case_id": "power_resistor_test_001",
                "device_name": "Power resistor field-bundle test",
                "device_class": "power_resistor",
                "device_subtype": "thin_film_power_resistor",
                "activity_type": "simulation_run",
                "operator_id": "test_operator",
                "platform": "test",
                "compute": "test_cpu",
                "processed_at": "2026-08-18T06:00:00Z",
            },
        )
        cls.repository = DADCRepository(cls.warehouse)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_bundle_hashes_every_relative_companion(self) -> None:
        bundle = json.loads(self.bundle.read_text(encoding="utf-8"))
        self.assertEqual("joule_thermal_field_bundle", bundle["bundle_type"])
        self.assertEqual(9, len(bundle["files"]))
        for item in bundle["files"]:
            relative = Path(item["path"])
            self.assertFalse(relative.is_absolute())
            self.assertNotIn("..", relative.parts)
            self.assertEqual(item["sha256"], sha256_file(self.source_dir / relative))

    def test_repository_and_multiphysics_activity_chain_are_valid(self) -> None:
        self.assertEqual("ingested", self.result.status, self.result.to_dict())
        report = self.repository.validate()
        self.assertTrue(report.valid, json.dumps(report.to_dict(), indent=2))
        device = self.repository.records("Device")[0]
        self.assertEqual("power_resistor", device["device_class"])
        self.assertEqual({"electromagnetics", "thermal"}, set(device["physics_domains"]))
        self.assertEqual("device_profiles/power_resistor.schema.json", device["profile_schema"])
        runs = self.repository.records("Run")
        self.assertEqual(2, sum(item["activity_type"] == "simulation_run" for item in runs))
        self.assertEqual(2, sum(item["activity_type"] == "data_processing" for item in runs))
        study = self.repository.records("Study")[0]
        self.assertEqual(1, len(study["coupling_edges"]))
        self.assertEqual("one_way", study["coupling_edges"][0]["coupling_type"])

    def test_mesh_and_fields_are_normalized_to_hdf5(self) -> None:
        case_data = self.warehouse / "cases" / "power_resistor_test_001" / "data"
        with h5py.File(case_data / "electrical_fields.h5", "r") as handle:
            self.assertEqual((527, 3), handle["/mesh/coordinates"].shape)
            self.assertEqual((480, 4), handle["/mesh/connectivity"].shape)
            self.assertEqual((17, 31), handle["/fields/electric_potential"].shape)
            self.assertEqual((17, 31, 2), handle["/fields/electric_field"].shape)
            self.assertEqual((17, 31), handle["/fields/joule_loss_density"].shape)
        with h5py.File(case_data / "thermal_fields.h5", "r") as handle:
            self.assertEqual((17, 31), handle["/fields/temperature"].shape)
            self.assertEqual((17, 31, 2), handle["/fields/heat_flux"].shape)

    def test_every_field_has_complete_coordinate_mesh_and_condition_metadata(self) -> None:
        fields = self.repository.records("Observable")
        self.assertEqual(5, len(fields))
        required = {
            "coordinate_system_ref",
            "coordinate_unit",
            "mesh_type",
            "components",
            "condition",
            "data_ref",
            "normalization",
        }
        for field in fields:
            self.assertEqual("field", field["observable_type"])
            self.assertTrue(required.issubset(field["field_metadata"]))
            self.assertEqual("structured", field["field_metadata"]["mesh_type"])
            self.assertEqual(field["data_ref"], field["field_metadata"]["data_ref"])

    def test_thermal_metric_trace_crosses_coupling_to_original_electrical_files(self) -> None:
        trace = self.repository.trace_metric("metric_power_resistor_test_001_maximum_temperature")
        run_ids = {item["run_id"] for item in trace["runs"]}
        self.assertEqual(
            {
                "run_power_resistor_test_001_electrical_fd",
                "run_power_resistor_test_001_electrical_import",
                "run_power_resistor_test_001_thermal_fd",
                "run_power_resistor_test_001_thermal_import",
            },
            run_ids,
        )
        observable_quantities = {item["quantity"] for item in trace["observables"]}
        self.assertIn("temperature", observable_quantities)
        self.assertIn("joule_loss_density", observable_quantities)
        artifact_names = {Path(item["relative_path"]).name for item in trace["artifacts"]}
        self.assertIn("electrical_fields.csv", artifact_names)
        self.assertIn("thermal_fields.csv", artifact_names)
        self.assertIn("coupling_map.json", artifact_names)
        self.assertEqual(1, len(trace["coupling_edges"]))

    def test_declared_numerical_validations_are_evidence_backed(self) -> None:
        validations = self.repository.records("Validation")
        self.assertEqual(
            {"solver_convergence", "mesh_independence", "physical_rule_check"},
            {item["validation_type"] for item in validations},
        )
        self.assertTrue(all(item["evidence_artifact_ids"] for item in validations))
        self.assertTrue(all(item["result"]["status"] == "passed" for item in validations))

    def test_companion_tampering_is_quarantined_before_case_creation(self) -> None:
        tampered_root = self.root / "tampered_source"
        shutil.copytree(self.source_dir, tampered_root)
        field_path = tampered_root / "thermal_fields.csv"
        field_path.write_text(field_path.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
        isolated = initialize_data_root(self.root / "tampered_data")
        result = WarehouseManager(isolated["warehouse"]).ingest(
            tampered_root / "joule_thermal_bundle.json",
            {
                "intake_schema_version": "1.0",
                "adapter": "joule_thermal_field_bundle",
                "case_id": "power_resistor_tampered_001",
                "device_name": "Tampered field bundle",
                "device_class": "power_resistor",
                "activity_type": "simulation_run",
            },
        )
        self.assertEqual("quarantined", result.status, result.to_dict())
        self.assertIn("SHA-256 mismatch", str(result.message))
        self.assertFalse(Path(isolated["warehouse"]).exists())

    def test_power_profile_appends_without_modifying_existing_core_schemas(self) -> None:
        isolated = initialize_data_root(self.root / "append_data")
        warehouse = Path(isolated["warehouse"])
        manager = WarehouseManager(warehouse)
        filter_result = manager.ingest(
            REPOSITORY_ROOT / "tests" / "fixtures" / "bandpass_filter_run_001_HFSSDesign1.s2p",
            {
                "intake_schema_version": "1.0",
                "case_id": "filter_before_power_001",
                "device_name": "Filter before power-resistor append",
                "device_class": "rf_filter",
                "activity_type": "simulation_run",
                "filter_order": 8,
                "source_timezone": "+08:00",
                "processed_at": "2026-08-18T05:50:00Z",
            },
        )
        self.assertEqual("ingested", filter_result.status, filter_result.to_dict())
        core_before = {
            path.name: sha256_file(path)
            for path in (warehouse / "schemas" / "v1.0").glob("*.schema.json")
        }
        power_result = manager.ingest(
            self.bundle,
            {
                "intake_schema_version": "1.0",
                "adapter": "joule_thermal_field_bundle",
                "case_id": "power_after_filter_001",
                "device_name": "Power resistor appended after filter",
                "device_class": "power_resistor",
                "activity_type": "simulation_run",
                "processed_at": "2026-08-18T06:00:00Z",
            },
        )
        self.assertEqual("ingested", power_result.status, power_result.to_dict())
        repository = DADCRepository(warehouse)
        self.assertTrue(repository.validate().valid)
        self.assertEqual(
            {"rf_filter", "power_resistor"},
            {item["device_class"] for item in repository.records("Device")},
        )
        core_after = {
            path.name: sha256_file(path)
            for path in (warehouse / "schemas" / "v1.0").glob("*.schema.json")
        }
        self.assertEqual(core_before, core_after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
