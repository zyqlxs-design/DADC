from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.ingestion.touchstone import parse_touchstone  # noqa: E402
from dadc.integrity import sha256_file  # noqa: E402


FIXTURE = REPOSITORY_ROOT / "tests" / "fixtures" / "bandpass_filter_run_001_HFSSDesign1.s2p"
FIXTURE_SHA256 = "6b8c8e41071e29f262ac3c67cea69a01b81af1cd7d646857108ffc1fbffbe620"


class TouchstoneParserTests(unittest.TestCase):
    def test_real_hfss_ma_file_is_parsed_without_losing_complex_values(self) -> None:
        data = parse_touchstone(FIXTURE)
        self.assertEqual(FIXTURE_SHA256, sha256_file(FIXTURE))
        self.assertEqual(2, data.port_count)
        self.assertEqual(("T1", "T2"), data.port_names)
        self.assertEqual("MA", data.source_complex_format)
        self.assertEqual("2025.2.4", data.metadata["hfss_version"])
        self.assertEqual("HFSSDesign1", data.metadata["design"])
        self.assertEqual("1p5GHz", data.metadata["setup"])
        self.assertEqual((361, 2, 2), data.values.shape)
        self.assertEqual(("S11", "S12", "S21", "S22"), data.components)
        self.assertEqual(600_000_000.0, data.frequencies_hz[0])
        self.assertEqual(2_400_000_000.0, data.frequencies_hz[-1])
        self.assertIn("临时收件箱", data.metadata["file_decoded"])
        expected_s11 = 0.99970338993509 * np.exp(1j * np.deg2rad(-72.8259766348261))
        expected_s21 = 0.000389621644315649 * np.exp(1j * np.deg2rad(17.2058635494022))
        self.assertAlmostEqual(expected_s11.real, data.values[0, 0, 0].real, places=14)
        self.assertAlmostEqual(expected_s11.imag, data.values[0, 0, 0].imag, places=14)
        self.assertAlmostEqual(expected_s21.real, data.values[0, 1, 0].real, places=14)
        self.assertAlmostEqual(expected_s21.imag, data.values[0, 1, 0].imag, places=14)

    def test_ri_and_db_are_normalized_using_touchstone_port_order(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dadc-touchstone-formats-") as temporary:
            root = Path(temporary)
            ri_path = root / "fixture.s2p"
            ri_path.write_text("# GHz S RI R 50\n1 1 2 3 4 5 6 7 8\n", encoding="ascii")
            ri = parse_touchstone(ri_path)
            self.assertEqual(1 + 2j, ri.values[0, 0, 0])
            self.assertEqual(3 + 4j, ri.values[0, 1, 0])
            self.assertEqual(5 + 6j, ri.values[0, 0, 1])
            self.assertEqual(7 + 8j, ri.values[0, 1, 1])

            db_path = root / "fixture_db.s2p"
            db_path.write_text(
                "# GHz S DB R 50\n1 0 0 -6 90 -20 -180 6 -90\n",
                encoding="ascii",
            )
            db = parse_touchstone(db_path)
            self.assertAlmostEqual(1.0, db.values[0, 0, 0].real)
            self.assertAlmostEqual(10 ** (-6 / 20), db.values[0, 1, 0].imag)
            self.assertAlmostEqual(-0.1, db.values[0, 0, 1].real)
            self.assertAlmostEqual(-(10 ** (6 / 20)), db.values[0, 1, 1].imag)

    def test_windows_1252_vendor_comments_do_not_block_numeric_ingestion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dadc-touchstone-cp1252-") as temporary:
            path = Path(temporary) / "vendor.s2p"
            path.write_bytes(
                "! IF BW – 100 kHz\n"
                "# Hz S RI R 50\n"
                "100000000 0.01 0.02 0.98 -0.04 0.98 -0.04 0.01 0.02\n".encode(
                    "cp1252"
                )
            )
            parsed = parse_touchstone(path)
            self.assertEqual("windows-1252", parsed.source_text_encoding)
            self.assertIn("IF BW – 100 kHz", parsed.comments)
            self.assertEqual((1, 2, 2), parsed.values.shape)


if __name__ == "__main__":
    unittest.main(verbosity=2)
