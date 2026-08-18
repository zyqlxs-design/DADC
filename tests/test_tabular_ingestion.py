from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import h5py


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.repository import DADCRepository  # noqa: E402
from dadc.warehouse import WarehouseManager, initialize_data_root  # noqa: E402


def _intake(case_id: str = "thermistor_csv_exp_001") -> dict:
    return {
        "intake_schema_version": "1.0",
        "adapter": "tabular_experiment_csv",
        "case_id": case_id,
        "device_name": "NTC thermistor voltage-current experiment",
        "device_class": "thermistor",
        "device_subtype": "ntc_thermistor",
        "physics_domains": ["electromagnetics", "thermal"],
        "activity_type": "experiment_run",
        "source_timestamp": "2026-08-18T14:00:00+08:00",
        "processed_at": "2026-08-18T14:05:00Z",
        "operator_id": "test_operator",
        "platform": "laboratory_bench",
        "compute": "local_cpu",
        "experiment_context": {
            "instrument": "DADC fixture source meter",
            "instrument_version": "1.0",
            "calibration": "fixture calibration certificate CAL-001",
            "ambient_temperature_K": 298.15,
        },
        "device_attributes": {
            "manufacturer": "fixture_vendor",
            "part_number": "NTC-TEST-10K",
        },
        "tabular_contract": {
            "encoding": "utf-8",
            "delimiter": ",",
            "axis": {
                "column": "time_s",
                "name": "time",
                "unit": "s",
                "strictly_increasing": True,
            },
            "observables": [
                {
                    "id_suffix": "voltage",
                    "observable_type": "curve",
                    "quantity": "electric_potential",
                    "complex_representation": "not_applicable",
                    "components": [
                        {"name": "voltage", "column": "voltage_v", "unit": "V"}
                    ],
                },
                {
                    "id_suffix": "current",
                    "observable_type": "curve",
                    "quantity": "electric_current",
                    "complex_representation": "not_applicable",
                    "components": [
                        {"name": "current", "column": "current_a", "unit": "A"}
                    ],
                },
                {
                    "id_suffix": "impedance",
                    "observable_type": "response",
                    "quantity": "complex_impedance",
                    "complex_representation": "real_imaginary",
                    "components": [
                        {
                            "name": "Z",
                            "real_column": "z_real_ohm",
                            "imaginary_column": "z_imag_ohm",
                            "unit": "ohm",
                        }
                    ],
                },
            ],
            "metrics": [
                {
                    "id_suffix": "maximum_current",
                    "name": "Maximum measured current",
                    "quantity": "maximum_electric_current",
                    "observable": "current",
                    "component": "current",
                    "operation": "max",
                    "unit": "A",
                },
                {
                    "id_suffix": "minimum_impedance_magnitude",
                    "name": "Minimum impedance magnitude",
                    "quantity": "minimum_impedance_magnitude",
                    "observable": "impedance",
                    "component": "Z",
                    "operation": "magnitude_min",
                    "unit": "ohm",
                },
            ],
        },
    }


class TabularExperimentIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="dadc-tabular-")
        self.root = Path(self._temporary.name)
        self.source = self.root / "measurement.csv"
        self.source.write_text(
            "time_s,voltage_v,current_a,z_real_ohm,z_imag_ohm\n"
            "0.0,0.0,0.00,10.0,0.0\n"
            "0.1,1.0,0.10,9.5,0.5\n"
            "0.2,2.0,0.19,9.0,1.0\n"
            "0.3,3.0,0.28,8.5,1.5\n",
            encoding="utf-8",
        )
        initialized = initialize_data_root(self.root / "DADC_DATA")
        self.warehouse = Path(initialized["warehouse"])
        self.manager = WarehouseManager(self.warehouse)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_real_and_complex_curves_ingest_without_core_schema_changes(self) -> None:
        result = self.manager.ingest(self.source, _intake())
        self.assertEqual("ingested", result.status, result.message)
        self.assertEqual("tabular_experiment_csv", result.adapter_id)
        repository = DADCRepository(self.warehouse)
        report = repository.validate()
        self.assertTrue(report.valid, report.to_dict())
        self.assertEqual(1, len(repository.records("Device")))
        self.assertEqual(2, len(repository.records("Run")))
        self.assertEqual(3, len(repository.records("Observable")))
        self.assertEqual(2, len(repository.records("Metric")))
        device = repository.records("Device")[0]
        self.assertEqual("thermistor", device["device_class"])
        self.assertEqual(
            "device_profiles/generic_component.schema.json",
            device["profile_schema"],
        )
        impedance = repository.get(
            "Observable", "obs_thermistor_csv_exp_001_impedance"
        )
        h5_relative, group_path = impedance["data_ref"].split(":", 1)
        with h5py.File(self.warehouse / h5_relative, "r") as handle:
            self.assertIn(f"{group_path}/real", handle)
            self.assertIn(f"{group_path}/imaginary", handle)
            self.assertEqual((4, 1), handle[f"{group_path}/real"].shape)

    def test_metric_trace_reaches_original_measurement_csv(self) -> None:
        result = self.manager.ingest(self.source, _intake())
        self.assertEqual("ingested", result.status, result.message)
        trace = DADCRepository(self.warehouse).trace_metric(
            "metric_thermistor_csv_exp_001_maximum_current"
        )
        roles = {item["artifact_role"] for item in trace["artifacts"]}
        self.assertIn("measurement_data", roles)
        self.assertIn("result_hdf5", roles)
        activities = {item["activity_type"] for item in trace["runs"]}
        self.assertEqual({"experiment_run", "data_processing"}, activities)

    def test_identical_csv_bytes_are_deduplicated(self) -> None:
        first = self.manager.ingest(self.source, _intake())
        self.assertEqual("ingested", first.status, first.message)
        renamed = self.root / "renamed.csv"
        shutil.copyfile(self.source, renamed)
        duplicate = self.manager.ingest(renamed, _intake("different_case_id"))
        self.assertEqual("duplicate", duplicate.status)
        self.assertEqual("thermistor_csv_exp_001", duplicate.duplicate_of_case_id)

    def test_nonmonotonic_axis_is_quarantined_without_creating_warehouse(self) -> None:
        bad_root = self.root / "bad_data"
        initialized = initialize_data_root(bad_root)
        bad_source = self.root / "nonmonotonic.csv"
        bad_source.write_text(
            "time_s,voltage_v,current_a,z_real_ohm,z_imag_ohm\n"
            "0.0,0.0,0.00,10.0,0.0\n"
            "0.2,1.0,0.10,9.5,0.5\n"
            "0.1,2.0,0.19,9.0,1.0\n",
            encoding="utf-8",
        )
        result = WarehouseManager(initialized["warehouse"]).ingest(
            bad_source,
            _intake("thermistor_bad_axis_001"),
        )
        self.assertEqual("quarantined", result.status)
        self.assertIn("strictly increasing", result.message)
        self.assertFalse(Path(initialized["warehouse"]).exists())
        self.assertTrue(Path(result.quarantine_path, "quarantine.json").is_file())

    def test_tampered_raw_csv_fails_integrity(self) -> None:
        result = self.manager.ingest(self.source, _intake())
        self.assertEqual("ingested", result.status, result.message)
        repository = DADCRepository(self.warehouse)
        raw_artifact = next(
            item
            for item in repository.records("Artifact")
            if item["artifact_role"] == "measurement_data"
        )
        (self.warehouse / raw_artifact["relative_path"]).write_text(
            "tampered\n", encoding="utf-8"
        )
        report = DADCRepository(self.warehouse).validate()
        self.assertFalse(report.valid)
        self.assertTrue(any(item.category == "integrity" for item in report.issues))


if __name__ == "__main__":
    unittest.main()
