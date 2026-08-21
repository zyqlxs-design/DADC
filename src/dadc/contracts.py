"""Versioned contracts that sit beside, but do not alter, DADC V1.0 entities."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def contract_root() -> Path:
    """Return the bundled non-entity contract directory."""

    candidate = Path(__file__).resolve().parents[2] / "contracts"
    if candidate.is_dir():
        return candidate
    candidate = Path(sys.prefix) / "share" / "dadc" / "contracts"
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError("Bundled DADC workflow contracts were not found")


def validate_contract(value: Any, name: str, version: str = "1.0") -> None:
    """Validate a workflow object and raise one compact, deterministic error."""

    path = contract_root() / f"v{version}" / f"{name}.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    if not errors:
        return
    first = errors[0]
    location = ".".join(str(item) for item in first.absolute_path) or "$"
    raise ValueError(f"{name} contract error at {location}: {first.message}")
