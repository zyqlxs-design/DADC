from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "generate_patch_antenna.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("dadc_generate_patch_antenna", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PyAedtGeneratorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.generator = _load_generator()
        schema = json.loads(
            (REPOSITORY_ROOT / "schemas" / "v1.0" / "artifact.schema.json").read_text(encoding="utf-8")
        )
        cls.allowed_roles = set(schema["properties"]["artifact_role"]["enum"])

    def test_every_generated_companion_uses_a_frozen_artifact_role(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidates = self.generator._companion_candidates(
                root,
                root / "model.aedt",
                root / "generation_manifest.json",
                root / "generation_recipe.py",
            )
        generated_roles = {role for _, role, _, _ in candidates}
        self.assertTrue(generated_roles)
        self.assertLessEqual(generated_roles, self.allowed_roles)

    def test_source_timezone_is_rejected_before_solver_launch(self) -> None:
        self.assertEqual("+08:00", self.generator._source_timezone_offset("+08:00"))
        with self.assertRaises(argparse.ArgumentTypeError):
            self.generator._source_timezone_offset("Asia/Shanghai")

    def test_tuning_parameters_are_rejected_before_solver_launch(self) -> None:
        self.assertEqual(9.57, self.generator._positive_mm("9.57"))
        self.assertEqual(0.485, self.generator._unit_interval("0.485"))
        self.assertEqual("patch_trial_001", self.generator._case_id("patch_trial_001"))
        with self.assertRaises(argparse.ArgumentTypeError):
            self.generator._positive_mm("0")
        with self.assertRaises(argparse.ArgumentTypeError):
            self.generator._unit_interval("1")
        with self.assertRaises(argparse.ArgumentTypeError):
            self.generator._case_id("Unsafe-Case")


if __name__ == "__main__":
    unittest.main()
