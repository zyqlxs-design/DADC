"""JSON Schema registry and validation helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .constants import ENTITY_TYPES

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError as exc:  # pragma: no cover - dependency error path
    raise RuntimeError(
        "DADC requires jsonschema. Install the project with `python3 -m pip install -e .`."
    ) from exc


_SCHEMA_FILENAMES = {
    "Device": "device.schema.json",
    "DesignRevision": "design_revision.schema.json",
    "Study": "study.schema.json",
    "Run": "run.schema.json",
    "Observable": "observable.schema.json",
    "Metric": "metric.schema.json",
    "Artifact": "artifact.schema.json",
    "Validation": "validation.schema.json",
    "Provenance": "provenance.schema.json",
}


class SchemaRegistry:
    """Loads immutable core schemas and separately registered device profiles."""

    def __init__(self, schema_root: str | Path):
        self.schema_root = Path(schema_root).resolve()
        self.version_root = self.schema_root / "v1.0"
        self._cache: dict[Path, dict[str, Any]] = {}
        self._extra_profiles: dict[str, Path] = {}

    def _load(self, path: Path) -> dict[str, Any]:
        path = path.resolve()
        if path not in self._cache:
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            self._cache[path] = schema
        return self._cache[path]

    def entity_schema(self, entity_type: str) -> dict[str, Any]:
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"Unknown entity_type: {entity_type}")
        return self._load(self.version_root / _SCHEMA_FILENAMES[entity_type])

    def repository_schema(self) -> dict[str, Any]:
        return self._load(self.version_root / "repository.schema.json")

    def validation_collection_schema(self) -> dict[str, Any]:
        return self._load(self.version_root / "validation_collection.schema.json")

    def register_device_profile(self, profile_ref: str, schema_path: str | Path) -> None:
        """Register a profile without changing any core schema or stored record."""

        candidate = Path(schema_path).resolve()
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        self._extra_profiles[profile_ref] = candidate
        self._load(candidate)

    def profile_schema(self, profile_ref: str) -> dict[str, Any]:
        if profile_ref in self._extra_profiles:
            return self._load(self._extra_profiles[profile_ref])
        candidate = (self.version_root / profile_ref).resolve()
        if self.version_root not in candidate.parents:
            raise ValueError(f"Profile escapes schema root: {profile_ref}")
        return self._load(candidate)

    @staticmethod
    def _errors(instance: Any, schema: dict[str, Any]) -> list[str]:
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
        rendered: list[str] = []
        for error in errors:
            location = "/".join(str(part) for part in error.absolute_path) or "$"
            rendered.append(f"{location}: {error.message}")
        return rendered

    def validate_repository_manifest(self, manifest: dict[str, Any]) -> list[str]:
        return self._errors(manifest, self.repository_schema())

    def validate_validation_collection(self, collection: dict[str, Any]) -> list[str]:
        return self._errors(collection, self.validation_collection_schema())

    def validate_record(self, record: dict[str, Any]) -> list[str]:
        entity_type = record.get("entity_type")
        if entity_type not in ENTITY_TYPES:
            return [f"$/entity_type: unsupported entity type {entity_type!r}"]
        errors = self._errors(record, self.entity_schema(entity_type))
        if entity_type == "Device" and not errors:
            profile_ref = record["profile_schema"]
            try:
                profile = self.profile_schema(profile_ref)
            except (FileNotFoundError, ValueError) as exc:
                return [f"$/profile_schema: {exc}"]
            errors.extend(self._errors(record["extensions"], profile))
        return errors
