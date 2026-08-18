from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.cli import main  # noqa: E402
from dadc.ingestion.registry import AdapterRegistry  # noqa: E402


class AdapterCatalogTests(unittest.TestCase):
    def test_every_installed_adapter_has_one_stable_capability_record(self) -> None:
        registry = AdapterRegistry()
        catalog = registry.catalog()
        self.assertEqual(len(registry.adapters), len(catalog))
        ids = [item["adapter_id"] for item in catalog]
        self.assertEqual(sorted(ids), ids)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("tabular_experiment_csv", ids)
        for adapter, capability in zip(
            sorted(registry.adapters, key=lambda item: item.adapter_id), catalog
        ):
            self.assertEqual(adapter.adapter_id, capability["adapter_id"])
            self.assertEqual(adapter.adapter_version, capability["adapter_version"])
            self.assertTrue(capability["source_formats"])
            self.assertTrue(capability["activity_types"])
            self.assertTrue(capability["device_classes"])
            self.assertTrue(capability["physics_domains"])
            self.assertTrue(capability["manifest_required"])

    def test_cli_prints_machine_readable_adapter_catalog(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["adapters"])
        self.assertEqual(0, status)
        rendered = json.loads(output.getvalue())
        self.assertEqual("1.0", rendered["adapter_catalog_version"])
        self.assertEqual(5, len(rendered["adapters"]))


if __name__ == "__main__":
    unittest.main()
