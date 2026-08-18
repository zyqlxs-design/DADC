"""Non-destructive schema migration functions."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


_ACTIVITY_MAP = {
    "simulation": "simulation_run",
    "experiment": "experiment_run",
    "literature": "literature_record",
    "processing": "data_processing",
    "optimization": "optimization_step",
}

_STATUS_MAP = {"completed": "succeeded", "error": "failed"}


def migrate_record(record: dict[str, Any], target_version: str = "1.0") -> dict[str, Any]:
    """Return a migrated copy. The input object is never mutated."""

    source_version = str(record.get("schema_version", "0.9"))
    if target_version != "1.0":
        raise ValueError(f"Unsupported target schema version: {target_version}")
    if source_version == "1.0":
        return deepcopy(record)
    if source_version != "0.9":
        raise ValueError(f"Unsupported migration path: {source_version} -> {target_version}")
    if record.get("entity_type") != "Run":
        raise ValueError("The bundled v0.9 -> v1.0 migration currently supports Run records only")

    migrated = deepcopy(record)
    migrated["schema_version"] = "1.0"
    if migrated.get("entity_type") == "Run":
        old_activity = migrated.pop("run_type")
        migrated["activity_type"] = _ACTIVITY_MAP[old_activity]
        migrated["status"] = _STATUS_MAP.get(migrated["status"], migrated["status"])
        if "parent_id" in migrated:
            migrated["parent_run_id"] = migrated.pop("parent_id")
    history = migrated.setdefault("migration_history", [])
    history.append(
        {
            "from_version": source_version,
            "to_version": "1.0",
            "method": "dadc.migration.migrate_record",
            "migrated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return migrated
