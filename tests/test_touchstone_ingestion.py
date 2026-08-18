from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.ingestion.importer import ingest_touchstone_filter_repository  # noqa: E402
from dadc.ingestion.touchstone import parse_touchstone  # noqa: E402
from dadc.integrity import sha256_file  # noqa: E402
from dadc.repository import DADCRepository  # noqa: E402


FIXTURE = REPOSITORY_ROOT / "tests" / "fixtures" / "bandpass_filter_run_001_HFSSDesign1.s2p"
FIXTURE_SHA256 = "6b8c8e41071e29f262ac3c67cea69a01b81af1cd7d646857108ffc1fbffbe620"
CASE_ID = "hfss_bandpass_real_001"


class RealTouchstoneRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="dadc-real-touchstone-")
        cls.root = Path(cls._temporary.name) / "repository"
        cls.result = ingest_touchstone_filter_repository(
            FIXTURE,
            cls.root,
            case_id=CASE_ID,
            device_name="HFSS official interdigital bandpass filter",
            filter_order=8,
            source_timezone="+08:00",
            operator_id="local_user",
            platform="Windows 10",
            compute="not_recorded",
            solver_edition="Student",
            processed_at="2026-08-17T08:10:00Z",
        )
        cls.repository = DADCRepository(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_ingested_repository_is_valid_and_preserves_raw_bytes(self) -> None:
        self.assertTrue(self.result.validation["valid"], self.result.validation)
        raw = self.root / "cases" / CASE_ID / "raw" / FIXTURE.name
        self.assertEqual(FIXTURE_SHA256, sha256_file(raw))
        self.assertEqual(FIXTURE.read_bytes(), raw.read_bytes())

    def test_canonical_hdf5_contains_real_and_imaginary_arrays(self) -> None:
        h5_path = self.root / "cases" / CASE_ID / "data" / "results.h5"
        parsed = parse_touchstone(FIXTURE)
        with h5py.File(h5_path, "r") as handle:
            group = handle["/observables/s_parameters"]
            self.assertEqual((361, 2, 2), group["real"].shape)
            np.testing.assert_allclose(group["real"][:], parsed.values.real, rtol=0.0, atol=0.0)
            np.testing.assert_allclose(group["imaginary"][:], parsed.values.imag, rtol=0.0, atol=0.0)
            self.assertEqual("MA", group.attrs["source_complex_format"])
            self.assertEqual("real_imaginary", group.attrs["complex_representation"])

    def test_unknown_geometry_is_not_invented(self) -> None:
        revision = self.repository.get("DesignRevision", f"rev_{CASE_ID}_001")
        self.assertEqual([], revision["geometry"]["parameters"])
        self.assertEqual("incomplete_from_touchstone_only", revision["topology"]["reconstruction_status"])
        self.assertEqual(
            "unknown_in_source",
            revision["topology"]["source_variables"]["Cavity_x"]["value_origin"],
        )

    def test_metrics_and_physical_screens_are_reproducible(self) -> None:
        # Decimal GHz values are not always exactly representable as binary
        # floats after conversion to Hz. One millihertz is far below the source
        # sweep resolution and still detects any meaningful regression.
        self.assertAlmostEqual(
            1_050_000_000.0,
            self.result.metrics["lower_3db_frequency"],
            delta=1.0e-3,
        )
        self.assertAlmostEqual(
            2_030_000_000.0,
            self.result.metrics["upper_3db_frequency"],
            delta=1.0e-3,
        )
        self.assertAlmostEqual(
            980_000_000.0,
            self.result.metrics["bandwidth_3db"],
            delta=1.0e-3,
        )
        self.assertAlmostEqual(-0.05293336520879887, self.result.metrics["peak_transmission_db"])
        self.assertAlmostEqual(
            0.9997050161124931,
            self.result.physical_checks["maximum_singular_value"],
        )
        self.assertTrue(self.result.physical_checks["passivity_screen_passed"])
        self.assertLess(self.result.physical_checks["maximum_reciprocity_error"], 1.0e-12)

    def test_metric_trace_reaches_source_run_raw_file_and_adapter(self) -> None:
        trace = self.repository.trace_metric(f"metric_{CASE_ID}_bandwidth_3db")
        self.assertEqual(
            {f"run_{CASE_ID}_hfss", f"run_{CASE_ID}_touchstone_import"},
            {record["run_id"] for record in trace["runs"]},
        )
        roles = {record["artifact_role"] for record in trace["artifacts"]}
        self.assertTrue({"raw_input", "result_hdf5", "script"}.issubset(roles))
        provenance_ids = {record["provenance_id"] for record in trace["provenance"]}
        self.assertEqual({f"prov_{CASE_ID}_hfss", f"prov_{CASE_ID}_touchstone_import"}, provenance_ids)

    def test_tampering_real_touchstone_fails_integrity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dadc-real-touchstone-tamper-") as temporary:
            copied = Path(temporary) / "repository"
            shutil.copytree(self.root, copied)
            raw = copied / "cases" / CASE_ID / "raw" / FIXTURE.name
            raw.write_bytes(raw.read_bytes() + b"! tampered\r\n")
            report = DADCRepository(copied).validate()
            self.assertFalse(report.valid)
            self.assertTrue(
                any(issue.category == "integrity" and "mismatch" in issue.message for issue in report.issues),
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
