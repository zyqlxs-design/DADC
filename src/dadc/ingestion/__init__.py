"""Deterministic ingestion adapters for external engineering data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .touchstone import TouchstoneData, TouchstoneError, parse_touchstone

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .field_bundle import FieldBundleIngestionResult
    from .importer import TouchstoneIngestionResult
    from .tabular import TabularIngestionResult

__all__ = [
    "TouchstoneData",
    "TouchstoneError",
    "FieldBundleIngestionResult",
    "TabularIngestionResult",
    "TouchstoneIngestionResult",
    "ingest_joule_thermal_field_bundle",
    "ingest_tabular_experiment_repository",
    "ingest_touchstone_antenna_repository",
    "ingest_touchstone_filter_repository",
    "ingest_touchstone_inductor_repository",
    "parse_touchstone",
]


def __getattr__(name: str) -> Any:
    if name in {"TabularIngestionResult", "ingest_tabular_experiment_repository"}:
        from .tabular import TabularIngestionResult, ingest_tabular_experiment_repository

        return {
            "TabularIngestionResult": TabularIngestionResult,
            "ingest_tabular_experiment_repository": ingest_tabular_experiment_repository,
        }[name]
    if name in {"FieldBundleIngestionResult", "ingest_joule_thermal_field_bundle"}:
        from .field_bundle import FieldBundleIngestionResult, ingest_joule_thermal_field_bundle

        return {
            "FieldBundleIngestionResult": FieldBundleIngestionResult,
            "ingest_joule_thermal_field_bundle": ingest_joule_thermal_field_bundle,
        }[name]
    if name in {
        "TouchstoneIngestionResult",
        "ingest_touchstone_antenna_repository",
        "ingest_touchstone_filter_repository",
        "ingest_touchstone_inductor_repository",
    }:
        from .importer import (
            TouchstoneIngestionResult,
            ingest_touchstone_antenna_repository,
            ingest_touchstone_filter_repository,
            ingest_touchstone_inductor_repository,
        )

        return {
            "TouchstoneIngestionResult": TouchstoneIngestionResult,
            "ingest_touchstone_antenna_repository": ingest_touchstone_antenna_repository,
            "ingest_touchstone_filter_repository": ingest_touchstone_filter_repository,
            "ingest_touchstone_inductor_repository": ingest_touchstone_inductor_repository,
        }[name]
    raise AttributeError(name)
