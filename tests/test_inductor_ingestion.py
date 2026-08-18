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
from dadc.integrity import sha256_file  # noqa: E402
from dadc.warehouse import WarehouseManager, initialize_data_root  # noqa: E402


CASE_ID = "vendor_inductor_test_001"
FILTER_FIXTURE = REPOSITORY_ROOT / "tests" / "fixtures" / "bandpass_filter_run_001_HFSSDesign1.s2p"


def _intake(datasheet: Path) -> dict[str, object]:
    return {
        "intake_schema_version": "1.0",
        "adapter": "touchstone_inductor",
        "case_id": CASE_ID,
        "device_name": "Vendor 5.6 nH RF inductor",
        "device_class": "inductor",
        "device_subtype": "wire_wound_ceramic_rf_inductor",
        "activity_type": "experiment_run",
        "manufacturer": "Example Components",
        "part_number": "EX-5N6",
        "construction": "wire_wound_ceramic_smt_inductor",
        "package_size": "0402",
        "source_timezone": "+01:00",
        "source_timestamp": "2020-02-10T00:00:00+01:00",
        "operator_id": "vendor",
        "platform": "VNA fixture",
        "compute": "not_applicable",
        "processed_at": "2026-08-18T04:00:00Z",
        "measurement_context": {
            "provider": "Example Components",
            "instrument": "Example VNA",
            "instrument_version": "1",
            "calibration": "two-port",
            "de_embedding": "fixture removed",
            "measurement_date": "2020-02-10",
        },
        "literature_context": {
            "document_type": "manufacturer datasheet",
            "document_revision": "1",
            "published_at": "2021-05-12T00:00:00Z",
            "citation": "Example EX-5N6 datasheet revision 1",
            "uri": "https://example.invalid/EX-5N6.pdf",
            "accessed_at": "2026-08-18T03:50:00Z",
        },
        "metric_reference_frequencies_hz": {
            "inductance": 250000000.0,
            "q_factor": 250000000.0,
        },
        "datasheet_specifications": [
            {
                "property": "inductance",
                "value": 5.6,
                "unit": "nH",
                "qualifier": "nominal",
                "tolerance": {"kind": "plus_minus", "value": 5.0, "unit": "%"},
                "test_conditions": [
                    {"quantity": "frequency", "value": 250000000.0, "unit": "Hz"}
                ],
                "value_origin": "literature_extracted",
            },
            {
                "property": "quality_factor",
                "value": 23.0,
                "unit": "1",
                "qualifier": "minimum",
                "test_conditions": [
                    {"quantity": "frequency", "value": 250000000.0, "unit": "Hz"}
                ],
                "value_origin": "literature_extracted",
            },
        ],
        "companion_artifacts": [
            {
                "path": str(datasheet),
                "role": "literature_source",
                "media_type": "application/pdf",
                "value_origin": "literature_extracted",
            }
        ],
    }


class InductorWarehouseIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="dadc-inductor-")
        self.root = Path(self._temporary.name)
        self.source = self.root / "vendor.s2p"
        self.source.write_bytes(
            (
                "! Vendor measurement – fixture de-embedded\n"
                "# Hz S RI R 50\n"
                "100000000 0.0036 0.0401 0.9812 -0.0430 0.9813 -0.0429 0.0064 0.0393\n"
                "250000000 0.0162 0.0906 0.9707 -0.0971 0.9711 -0.0968 0.0182 0.0898\n"
                "500000000 0.0440 0.1670 0.9450 -0.1780 0.9452 -0.1778 0.0455 0.1665\n"
            ).encode("cp1252")
        )
        self.datasheet = self.root / "datasheet.pdf"
        self.datasheet.write_bytes(b"%PDF-1.4\n% deterministic test evidence\n")
        initialized = initialize_data_root(self.root / "DADC_DATA")
        self.warehouse = Path(initialized["warehouse"])
        self.result = WarehouseManager(self.warehouse).ingest(
            self.source,
            _intake(self.datasheet),
        )
        self.repository = DADCRepository(self.warehouse)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_three_activity_types_are_separate_and_repository_is_valid(self) -> None:
        self.assertEqual("ingested", self.result.status, self.result.to_dict())
        report = self.repository.validate()
        self.assertTrue(report.valid, json.dumps(report.to_dict(), indent=2))
        self.assertEqual(
            {"experiment_run", "literature_record", "data_processing"},
            {record["activity_type"] for record in self.repository.records("Run")},
        )
        for run in self.repository.records("Run"):
            marker = {
                "experiment_run": "experiment",
                "literature_record": "literature",
                "data_processing": "processing",
            }[run["activity_type"]]
            self.assertEqual({marker}, set(run["source_context"]) & {
                "solver", "experiment", "literature", "processing", "optimization"
            })

    def test_profile_is_sparse_and_specs_point_to_datasheet_artifact(self) -> None:
        device = self.repository.records("Device")[0]
        self.assertEqual("inductor", device["device_class"])
        self.assertEqual("device_profiles/inductor.schema.json", device["profile_schema"])
        extension = device["extensions"]["inductor"]
        self.assertNotIn("feed_type", extension)
        literature_artifact = next(
            item for item in self.repository.records("Artifact")
            if item["artifact_role"] == "literature_source"
        )
        self.assertTrue(all(
            item["source_artifact_id"] == literature_artifact["artifact_id"]
            for item in extension["datasheet_specifications"]
        ))

    def test_raw_measurement_and_calculated_curves_remain_distinguishable(self) -> None:
        observables = {item["quantity"]: item for item in self.repository.records("Observable")}
        self.assertEqual("raw_experiment_output", observables["complex_scattering_parameter"]["value_origin"])
        self.assertEqual("calculated", observables["complex_differential_impedance"]["value_origin"])
        self.assertEqual(
            [observables["complex_scattering_parameter"]["observable_id"]],
            observables["complex_differential_impedance"]["derived_from_observable_ids"],
        )
        h5_path = self.warehouse / "cases" / CASE_ID / "data" / "results.h5"
        with h5py.File(h5_path, "r") as handle:
            self.assertIn("real", handle["/observables/differential_impedance"])
            self.assertIn("imaginary", handle["/observables/differential_impedance"])
            self.assertEqual("windows-1252", handle["/observables/s_parameters"].attrs["source_text_encoding"])

    def test_metric_trace_reaches_measurement_but_not_unrelated_literature_run(self) -> None:
        metric_id = f"metric_{CASE_ID}_effective_inductance_at_reference_frequency"
        trace = self.repository.trace_metric(metric_id)
        self.assertEqual(
            {
                f"run_{CASE_ID}_vendor_vna_measurement",
                f"run_{CASE_ID}_touchstone_import",
            },
            {item["run_id"] for item in trace["runs"]},
        )
        self.assertIn("measurement_data", {item["artifact_role"] for item in trace["artifacts"]})
        self.assertNotIn("literature_source", {item["artifact_role"] for item in trace["artifacts"]})

    def test_datasheet_tampering_fails_integrity(self) -> None:
        copied = self.root / "tampered"
        shutil.copytree(self.warehouse, copied)
        artifact = next(
            item for item in DADCRepository(copied).records("Artifact")
            if item["artifact_role"] == "literature_source"
        )
        path = copied / artifact["relative_path"]
        path.write_bytes(path.read_bytes() + b"tampered")
        report = DADCRepository(copied).validate()
        self.assertFalse(report.valid)
        self.assertTrue(any(item.category == "integrity" for item in report.issues))

    def test_inductor_and_filter_append_without_rewriting_core_schemas(self) -> None:
        core_before = {
            path.name: sha256_file(path)
            for path in (self.warehouse / "schemas" / "v1.0").glob("*.schema.json")
        }
        result = WarehouseManager(self.warehouse).ingest(
            FILTER_FIXTURE,
            {
                "intake_schema_version": "1.0",
                "case_id": "filter_after_inductor_001",
                "device_name": "Filter appended after vendor inductor",
                "device_class": "rf_filter",
                "activity_type": "simulation_run",
                "filter_order": 8,
                "source_timezone": "+08:00",
                "processed_at": "2026-08-18T05:00:00Z",
            },
        )
        self.assertEqual("ingested", result.status, result.to_dict())
        repository = DADCRepository(self.warehouse)
        self.assertTrue(repository.validate().valid)
        self.assertEqual(
            {"inductor", "rf_filter"},
            {item["device_class"] for item in repository.records("Device")},
        )
        core_after = {
            path.name: sha256_file(path)
            for path in (self.warehouse / "schemas" / "v1.0").glob("*.schema.json")
        }
        self.assertEqual(core_before, core_after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
