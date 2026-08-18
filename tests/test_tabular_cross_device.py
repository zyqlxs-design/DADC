from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import h5py


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.repository import DADCRepository  # noqa: E402
from dadc.warehouse import WarehouseManager, initialize_data_root  # noqa: E402


INTAKE_DIRECTORY = REPOSITORY_ROOT / "examples" / "intake"
THERMISTOR_SOURCE = INTAKE_DIRECTORY / "generic_experiment.csv"
THERMISTOR_MANIFEST = INTAKE_DIRECTORY / "generic_experiment_csv.dadc.json"
PHOTODIODE_SOURCE = INTAKE_DIRECTORY / "photodiode_spectral_experiment.csv"
PHOTODIODE_MANIFEST = INTAKE_DIRECTORY / "photodiode_spectral_experiment.dadc.json"


def _load_manifest(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):  # pragma: no cover - fixed fixture guard
        raise TypeError(path)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _core_schema_hashes(schema_root: Path) -> dict[str, str]:
    return {
        path.name: _sha256(path)
        for path in sorted((schema_root / "v1.0").glob("*.schema.json"))
    }


def _contains_none(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_contains_none(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_none(item) for item in value)
    return False


class CrossDeviceTabularContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="dadc-cross-device-")
        self.root = Path(self._temporary.name)
        initialized = initialize_data_root(self.root / "DADC_DATA")
        self.warehouse = Path(initialized["warehouse"])
        self.manager = WarehouseManager(self.warehouse)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _ingest_both(self) -> tuple[object, object]:
        thermistor = self.manager.ingest(
            THERMISTOR_SOURCE,
            _load_manifest(THERMISTOR_MANIFEST),
        )
        photodiode = self.manager.ingest(
            PHOTODIODE_SOURCE,
            _load_manifest(PHOTODIODE_MANIFEST),
        )
        return thermistor, photodiode

    def test_two_devices_share_one_adapter_without_core_or_adapter_mutation(self) -> None:
        adapter_path = REPOSITORY_ROOT / "src" / "dadc" / "ingestion" / "tabular.py"
        adapter_hash_before = _sha256(adapter_path)

        thermistor = self.manager.ingest(
            THERMISTOR_SOURCE,
            _load_manifest(THERMISTOR_MANIFEST),
        )
        self.assertEqual("ingested", thermistor.status, thermistor.message)
        warehouse_core_before = _core_schema_hashes(self.warehouse / "schemas")

        photodiode = self.manager.ingest(
            PHOTODIODE_SOURCE,
            _load_manifest(PHOTODIODE_MANIFEST),
        )
        self.assertEqual("ingested", photodiode.status, photodiode.message)
        self.assertEqual("tabular_experiment_csv", thermistor.adapter_id)
        self.assertEqual(thermistor.adapter_id, photodiode.adapter_id)
        self.assertEqual(warehouse_core_before, _core_schema_hashes(self.warehouse / "schemas"))
        self.assertEqual(adapter_hash_before, _sha256(adapter_path))

        repository = DADCRepository(self.warehouse)
        report = repository.validate()
        self.assertTrue(report.valid, report.to_dict())
        devices = {item["device_class"]: item for item in repository.records("Device")}
        self.assertEqual({"thermistor", "photodiode"}, set(devices))
        self.assertEqual(
            ["electromagnetics", "thermal"],
            devices["thermistor"]["physics_domains"],
        )
        self.assertEqual(
            ["optical", "electromagnetics"],
            devices["photodiode"]["physics_domains"],
        )

    def test_semicolon_spectrum_is_normalized_and_traceable(self) -> None:
        result = self.manager.ingest(
            PHOTODIODE_SOURCE,
            _load_manifest(PHOTODIODE_MANIFEST),
        )
        self.assertEqual("ingested", result.status, result.message)
        repository = DADCRepository(self.warehouse)
        observable = repository.get(
            "Observable",
            "obs_photodiode_spectral_exp_001_responsivity",
        )
        self.assertEqual(
            [{"name": "wavelength", "unit": "nm", "data_ref": observable["axes"][0]["data_ref"]}],
            observable["axes"],
        )
        relative_path, object_path = observable["data_ref"].split(":", 1)
        with h5py.File(self.warehouse / relative_path, "r") as handle:
            values = handle[object_path][:]
        self.assertEqual((6, 1), values.shape)
        self.assertAlmostEqual(0.55, float(values.max()))

        metric = repository.get(
            "Metric",
            "metric_photodiode_spectral_exp_001_maximum_responsivity",
        )
        self.assertAlmostEqual(0.55, metric["value"])
        trace = repository.trace_metric(metric["metric_id"])
        self.assertEqual(
            {"experiment_run", "data_processing"},
            {item["activity_type"] for item in trace["runs"]},
        )
        self.assertIn(
            "measurement_data",
            {item["artifact_role"] for item in trace["artifacts"]},
        )
        self.assertIn(
            "result_hdf5",
            {item["artifact_role"] for item in trace["artifacts"]},
        )

    def test_device_extensions_remain_sparse_and_do_not_form_a_cross_device_row(self) -> None:
        thermistor, photodiode = self._ingest_both()
        self.assertEqual("ingested", thermistor.status, thermistor.message)
        self.assertEqual("ingested", photodiode.status, photodiode.message)
        devices = {
            item["device_class"]: item
            for item in DADCRepository(self.warehouse).records("Device")
        }
        thermistor_attributes = devices["thermistor"]["extensions"]["generic_component"]["attributes"]
        photodiode_attributes = devices["photodiode"]["extensions"]["generic_component"]["attributes"]
        self.assertNotIn("active_area_mm2", thermistor_attributes)
        self.assertNotIn("window_material", thermistor_attributes)
        self.assertEqual(1.0, photodiode_attributes["active_area_mm2"])
        self.assertEqual("fixture_glass", photodiode_attributes["window_material"])
        self.assertNotEqual(set(thermistor_attributes), set(photodiode_attributes))
        self.assertFalse(_contains_none(devices["thermistor"]))
        self.assertFalse(_contains_none(devices["photodiode"]))


if __name__ == "__main__":
    unittest.main()
