"""Deterministic global Parquet index construction for DADC repositories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .constants import ENTITY_ID_FIELDS


def scan_records(root: str | Path) -> list[tuple[str, str, dict[str, Any], Path]]:
    """Return all entity and validation records in stable path order."""

    root_path = Path(root).resolve()
    rows: list[tuple[str, str, dict[str, Any], Path]] = []
    for path in sorted((root_path / "cases").glob("*/metadata/*/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        entity_type = record["entity_type"]
        rows.append((entity_type, record[ENTITY_ID_FIELDS[entity_type]], record, path))
    for path in sorted((root_path / "cases").glob("*/validation.json")):
        for record in json.loads(path.read_text(encoding="utf-8"))["validations"]:
            rows.append(("Validation", record["validation_id"], record, path))
    return rows


def rebuild_indexes(root: str | Path) -> None:
    """Rebuild catalog and metric indexes from the canonical JSON records.

    The Parquet files are derived query accelerators. JSON remains authoritative.
    Temporary files are replaced atomically so readers never see partial output.
    """

    root_path = Path(root).resolve()
    catalog_rows: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    for entity_type, identifier, record, path in scan_records(root_path):
        case_id = path.relative_to(root_path).parts[1]
        catalog_rows.append(
            {
                "case_id": case_id,
                "entity_type": entity_type,
                "entity_id": identifier,
                "schema_version": record["schema_version"],
                "json_path": path.relative_to(root_path).as_posix(),
            }
        )
        if entity_type == "Metric":
            metric_rows.append(
                {
                    "case_id": case_id,
                    "metric_id": record["metric_id"],
                    "run_id": record["run_id"],
                    "name": record["name"],
                    "quantity": record["quantity"],
                    "value": float(record["value"]),
                    "unit": record["unit"],
                    "value_origin": record["value_origin"],
                    "source_observable_ids_json": json.dumps(record["source_observable_ids"]),
                }
            )

    if not catalog_rows:
        raise ValueError("Cannot build DADC indexes without at least one entity record")

    index = root_path / "index"
    index.mkdir(parents=True, exist_ok=True)
    catalog_path = index / "catalog.parquet"
    metrics_path = index / "metrics.parquet"
    catalog_temporary = index / ".catalog.parquet.tmp"
    metrics_temporary = index / ".metrics.parquet.tmp"
    pq.write_table(pa.Table.from_pylist(catalog_rows), catalog_temporary, compression="zstd")
    # A valid DADC case always has metrics. Keep a clear failure if an adapter
    # violates that contract instead of emitting an unreadable empty table.
    if not metric_rows:
        catalog_temporary.unlink(missing_ok=True)
        raise ValueError("Cannot build metric index without at least one Metric record")
    pq.write_table(pa.Table.from_pylist(metric_rows), metrics_temporary, compression="zstd")
    catalog_temporary.replace(catalog_path)
    metrics_temporary.replace(metrics_path)

