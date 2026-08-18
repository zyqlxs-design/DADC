"""Ingest explicitly described experimental CSV curves into DADC V1.0."""

from __future__ import annotations

import csv
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from ..indexing import rebuild_indexes
from ..integrity import sha256_file
from ..repository import DADCRepository
from .importer import (
    _artifact,
    _h5_ref,
    _require_aware_datetime,
    _source_schemas,
    _subject,
    _utc_now,
    _write_json,
    _write_record,
)

ADAPTER_VERSION = "1.0.0"
_CASE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SUFFIX = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_ENCODINGS = {"utf-8", "utf-8-sig", "cp1252"}
_REAL_OPERATIONS = {"min", "max", "mean", "rms", "first", "last"}
_COMPLEX_OPERATIONS = {
    "magnitude_min",
    "magnitude_max",
    "magnitude_mean",
    "magnitude_rms",
}


@dataclass(frozen=True)
class TabularIngestionResult:
    repository: Path
    case_id: str
    source_sha256: str
    row_count: int
    observable_count: int
    metrics: dict[str, float]
    validation: dict[str, Any]


def _required_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{name} must be a non-empty object")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _suffix(value: Any, name: str) -> str:
    rendered = _required_text(value, name)
    if not _SUFFIX.fullmatch(rendered):
        raise ValueError(f"{name} must match {_SUFFIX.pattern}")
    return rendered


def _load_contract(intake: dict[str, Any]) -> dict[str, Any]:
    required = (
        "case_id",
        "device_name",
        "device_class",
        "device_subtype",
        "physics_domains",
        "source_timestamp",
        "experiment_context",
        "tabular_contract",
    )
    missing = [key for key in required if intake.get(key) in (None, "", [], {})]
    if missing:
        raise ValueError(f"Tabular experiment intake is missing: {', '.join(missing)}")
    if intake.get("activity_type", "experiment_run") != "experiment_run":
        raise ValueError("tabular_experiment_csv requires activity_type=experiment_run")
    case_id = str(intake["case_id"])
    if not _CASE_ID.fullmatch(case_id):
        raise ValueError("case_id must match ^[a-z][a-z0-9_]{2,63}$")
    physics_domains = intake["physics_domains"]
    if not isinstance(physics_domains, list) or not physics_domains:
        raise ValueError("physics_domains must be a non-empty array")
    if any(not isinstance(item, str) or not item for item in physics_domains):
        raise ValueError("Every physics_domains value must be a non-empty string")
    if len(set(physics_domains)) != len(physics_domains):
        raise ValueError("physics_domains must not contain duplicates")
    _require_aware_datetime(str(intake["source_timestamp"]), "source_timestamp")
    _required_mapping(intake["experiment_context"], "experiment_context")

    contract = _required_mapping(intake["tabular_contract"], "tabular_contract")
    encoding = str(contract.get("encoding", "utf-8-sig")).lower()
    if encoding not in _ENCODINGS:
        raise ValueError(f"tabular_contract.encoding must be one of {sorted(_ENCODINGS)}")
    delimiter = str(contract.get("delimiter", ","))
    if len(delimiter) != 1 or delimiter in {"\r", "\n", '"'}:
        raise ValueError("tabular_contract.delimiter must be one safe character")

    axis = _required_mapping(contract.get("axis"), "tabular_contract.axis")
    for key in ("column", "name", "unit"):
        _required_text(axis.get(key), f"tabular_contract.axis.{key}")

    observables = contract.get("observables")
    if not isinstance(observables, list) or not observables:
        raise ValueError("tabular_contract.observables must be a non-empty array")
    seen_observables: set[str] = set()
    for index, observable in enumerate(observables):
        path = f"tabular_contract.observables[{index}]"
        if not isinstance(observable, dict):
            raise ValueError(f"{path} must be an object")
        observable_suffix = _suffix(observable.get("id_suffix"), f"{path}.id_suffix")
        if observable_suffix in seen_observables:
            raise ValueError(f"Duplicate observable id_suffix: {observable_suffix}")
        seen_observables.add(observable_suffix)
        _required_text(observable.get("quantity"), f"{path}.quantity")
        observable_type = str(observable.get("observable_type", "curve"))
        if observable_type not in {"curve", "response", "table"}:
            raise ValueError(f"{path}.observable_type must be curve, response, or table")
        representation = str(observable.get("complex_representation", "not_applicable"))
        if representation not in {"not_applicable", "real_imaginary"}:
            raise ValueError(f"{path}.complex_representation is unsupported")
        components = observable.get("components")
        if not isinstance(components, list) or not components:
            raise ValueError(f"{path}.components must be a non-empty array")
        seen_components: set[str] = set()
        for component_index, component in enumerate(components):
            component_path = f"{path}.components[{component_index}]"
            if not isinstance(component, dict):
                raise ValueError(f"{component_path} must be an object")
            component_name = _required_text(component.get("name"), f"{component_path}.name")
            if component_name in seen_components:
                raise ValueError(f"Duplicate component name in {path}: {component_name}")
            seen_components.add(component_name)
            _required_text(component.get("unit"), f"{component_path}.unit")
            if representation == "not_applicable":
                _required_text(component.get("column"), f"{component_path}.column")
                if "real_column" in component or "imaginary_column" in component:
                    raise ValueError(f"{component_path} mixes real and complex column declarations")
            else:
                _required_text(component.get("real_column"), f"{component_path}.real_column")
                _required_text(component.get("imaginary_column"), f"{component_path}.imaginary_column")
                if "column" in component:
                    raise ValueError(f"{component_path} mixes complex and real column declarations")

    metrics = contract.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise ValueError("tabular_contract.metrics must define at least one reproducible metric")
    seen_metrics: set[str] = set()
    for index, metric in enumerate(metrics):
        path = f"tabular_contract.metrics[{index}]"
        if not isinstance(metric, dict):
            raise ValueError(f"{path} must be an object")
        metric_suffix = _suffix(metric.get("id_suffix"), f"{path}.id_suffix")
        if metric_suffix in seen_metrics:
            raise ValueError(f"Duplicate metric id_suffix: {metric_suffix}")
        seen_metrics.add(metric_suffix)
        observable_suffix = _required_text(metric.get("observable"), f"{path}.observable")
        if observable_suffix not in seen_observables:
            raise ValueError(f"{path}.observable references unknown id_suffix {observable_suffix!r}")
        for key in ("name", "quantity", "component", "operation"):
            _required_text(metric.get(key), f"{path}.{key}")
    return contract


def _read_arrays(
    source: Path,
    contract: dict[str, Any],
) -> tuple[np.ndarray, dict[str, np.ndarray], list[str], dict[str, dict[str, str]]]:
    encoding = str(contract.get("encoding", "utf-8-sig")).lower()
    delimiter = str(contract.get("delimiter", ","))
    try:
        with source.open("r", encoding=encoding, newline="") as stream:
            reader = csv.DictReader(stream, delimiter=delimiter)
            fieldnames = reader.fieldnames
            if not fieldnames:
                raise ValueError("CSV has no header row")
            if len(set(fieldnames)) != len(fieldnames):
                raise ValueError("CSV header contains duplicate column names")
            rows = list(reader)
    except UnicodeDecodeError as exc:
        raise ValueError(f"CSV cannot be decoded as {encoding}") from exc
    if not rows:
        raise ValueError("CSV has no data rows")

    required_columns = {str(contract["axis"]["column"])}
    component_metadata: dict[str, dict[str, str]] = {}
    for observable in contract["observables"]:
        representation = str(observable.get("complex_representation", "not_applicable"))
        for component in observable["components"]:
            key = f"{observable['id_suffix']}::{component['name']}"
            component_metadata[key] = {
                "unit": str(component["unit"]),
                "representation": representation,
            }
            if representation == "not_applicable":
                required_columns.add(str(component["column"]))
            else:
                required_columns.add(str(component["real_column"]))
                required_columns.add(str(component["imaginary_column"]))
    missing_columns = sorted(required_columns.difference(fieldnames))
    if missing_columns:
        raise ValueError(f"CSV is missing declared columns: {', '.join(missing_columns)}")

    def numeric(column: str) -> np.ndarray:
        values: list[float] = []
        for row_index, row in enumerate(rows, start=2):
            raw = row.get(column)
            if raw is None or not raw.strip():
                raise ValueError(f"CSV row {row_index} column {column!r} is empty")
            try:
                value = float(raw)
            except ValueError as exc:
                raise ValueError(
                    f"CSV row {row_index} column {column!r} is not numeric: {raw!r}"
                ) from exc
            if not np.isfinite(value):
                raise ValueError(f"CSV row {row_index} column {column!r} is not finite")
            values.append(value)
        return np.asarray(values, dtype=np.float64)

    axis = numeric(str(contract["axis"]["column"]))
    if bool(contract["axis"].get("strictly_increasing", True)) and not np.all(np.diff(axis) > 0.0):
        raise ValueError("Declared axis must be strictly increasing")

    arrays: dict[str, np.ndarray] = {}
    for observable in contract["observables"]:
        representation = str(observable.get("complex_representation", "not_applicable"))
        columns: list[np.ndarray] = []
        for component in observable["components"]:
            if representation == "not_applicable":
                columns.append(numeric(str(component["column"])))
            else:
                real = numeric(str(component["real_column"]))
                imaginary = numeric(str(component["imaginary_column"]))
                columns.append(real + 1j * imaginary)
        arrays[str(observable["id_suffix"])] = np.stack(columns, axis=1)
    return axis, arrays, fieldnames, component_metadata


def _metric_values(
    contract: dict[str, Any],
    arrays: dict[str, np.ndarray],
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    observable_specs = {
        str(item["id_suffix"]): item for item in contract["observables"]
    }
    rendered: dict[str, float] = {}
    normalized: list[dict[str, Any]] = []
    for metric in contract["metrics"]:
        observable_suffix = str(metric["observable"])
        observable = observable_specs[observable_suffix]
        component_names = [str(item["name"]) for item in observable["components"]]
        component_name = str(metric["component"])
        if component_name not in component_names:
            raise ValueError(
                f"Metric {metric['id_suffix']!r} references unknown component {component_name!r}"
            )
        component_index = component_names.index(component_name)
        values = arrays[observable_suffix][:, component_index]
        representation = str(observable.get("complex_representation", "not_applicable"))
        operation = str(metric["operation"])
        if representation == "not_applicable":
            if operation not in _REAL_OPERATIONS:
                raise ValueError(
                    f"Real metric operation must be one of {sorted(_REAL_OPERATIONS)}"
                )
            operands = np.real(values)
            operation_name = operation
        else:
            if operation not in _COMPLEX_OPERATIONS:
                raise ValueError(
                    f"Complex metric operation must be one of {sorted(_COMPLEX_OPERATIONS)}"
                )
            operands = np.abs(values)
            operation_name = operation.removeprefix("magnitude_")
        if operation_name == "min":
            value = float(np.min(operands))
        elif operation_name == "max":
            value = float(np.max(operands))
        elif operation_name == "mean":
            value = float(np.mean(operands))
        elif operation_name == "rms":
            value = float(np.sqrt(np.mean(np.square(operands))))
        elif operation_name == "first":
            value = float(operands[0])
        elif operation_name == "last":
            value = float(operands[-1])
        else:  # pragma: no cover - guarded by operation sets
            raise AssertionError(operation_name)
        component_spec = observable["components"][component_index]
        unit = str(metric.get("unit") or component_spec["unit"])
        suffix = str(metric["id_suffix"])
        rendered[suffix] = value
        normalized.append({**metric, "value": value, "unit": unit})
    return rendered, normalized


def ingest_tabular_experiment_repository(
    source: str | Path,
    target: str | Path,
    *,
    intake: dict[str, Any],
) -> TabularIngestionResult:
    """Create one validated case from a CSV plus explicit semantic intake."""

    contract = _load_contract(intake)
    source_path = Path(source).resolve()
    if source_path.suffix.lower() != ".csv" or not source_path.is_file():
        raise ValueError("Tabular experiment source must be one existing .csv file")
    root = Path(target).resolve()
    if root.exists():
        if root.is_file() or any(root.iterdir()):
            raise FileExistsError(f"Refusing to overwrite non-empty target: {root}")
    else:
        root.mkdir(parents=True)

    processed = _require_aware_datetime(
        str(intake.get("processed_at") or _utc_now()),
        "processed_at",
    )
    source_time = _require_aware_datetime(
        str(intake["source_timestamp"]),
        "source_timestamp",
    )
    axis, arrays, fieldnames, component_metadata = _read_arrays(source_path, contract)
    metrics, metric_specs = _metric_values(contract, arrays)

    case_id = str(intake["case_id"])
    case = root / "cases" / case_id
    raw_path = case / "raw" / source_path.name
    h5_path = case / "data" / "results.h5"
    script_path = case / "scripts" / "tabular_adapter.py"
    intake_path = case / "evidence" / "intake_snapshot.json"
    checks_path = case / "evidence" / "tabular_checks.json"
    for directory in (raw_path.parent, h5_path.parent, script_path.parent, intake_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, raw_path)
    if sha256_file(raw_path) != sha256_file(source_path):
        raise IOError("Raw CSV copy failed byte-integrity verification")
    shutil.copyfile(Path(__file__), script_path)
    _write_json(intake_path, intake)
    shutil.copytree(_source_schemas(), root / "schemas")

    axis_name = str(contract["axis"]["name"])
    with h5py.File(h5_path, "x") as handle:
        axis_dataset = handle.create_group("axes").create_dataset(axis_name, data=axis)
        axis_dataset.attrs["unit"] = str(contract["axis"]["unit"])
        axis_dataset.attrs["source_column"] = str(contract["axis"]["column"])
        observables_group = handle.create_group("observables")
        for observable in contract["observables"]:
            suffix = str(observable["id_suffix"])
            values = arrays[suffix]
            components = [str(item["name"]) for item in observable["components"]]
            units = [str(item["unit"]) for item in observable["components"]]
            representation = str(observable.get("complex_representation", "not_applicable"))
            if representation == "real_imaginary":
                group = observables_group.create_group(suffix)
                group.create_dataset("real", data=np.real(values), compression="gzip")
                group.create_dataset("imaginary", data=np.imag(values), compression="gzip")
                group.attrs["complex_representation"] = "real_imaginary"
                group.attrs["components"] = json.dumps(components)
                group.attrs["units"] = json.dumps(units)
            else:
                dataset = observables_group.create_dataset(suffix, data=values, compression="gzip")
                dataset.attrs["complex_representation"] = "not_applicable"
                dataset.attrs["components"] = json.dumps(components)
                dataset.attrs["units"] = json.dumps(units)
        handle.attrs["source_sha256"] = sha256_file(source_path)
        handle.attrs["adapter_id"] = "tabular_experiment_csv"
        handle.attrs["adapter_version"] = ADAPTER_VERSION

    _write_json(
        checks_path,
        {
            "check_version": "1.0",
            "source_sha256": sha256_file(source_path),
            "row_count": int(axis.size),
            "columns": fieldnames,
            "declared_column_count": len(fieldnames),
            "numeric_nonfinite_count": 0,
            "axis": {
                "name": axis_name,
                "unit": str(contract["axis"]["unit"]),
                "strictly_increasing": bool(np.all(np.diff(axis) > 0.0)),
            },
            "component_metadata": component_metadata,
        },
    )

    slug = case_id
    device_id = f"device_{slug}"
    revision_id = f"rev_{slug}_001"
    study_id = f"study_{slug}_measurement"
    experiment_run_id = f"run_{slug}_experiment"
    processing_run_id = f"run_{slug}_tabular_import"
    experiment_prov_id = f"prov_{slug}_experiment"
    processing_prov_id = f"prov_{slug}_tabular_import"
    raw_artifact_id = f"art_{slug}_measurement_csv"
    h5_artifact_id = f"art_{slug}_results_h5"
    script_artifact_id = f"art_{slug}_adapter_script"
    intake_artifact_id = f"art_{slug}_intake_snapshot"
    checks_artifact_id = f"art_{slug}_tabular_checks"

    observable_ids = {
        str(item["id_suffix"]): f"obs_{slug}_{item['id_suffix']}"
        for item in contract["observables"]
    }
    metric_ids = {
        str(item["id_suffix"]): f"metric_{slug}_{item['id_suffix']}"
        for item in contract["metrics"]
    }

    experiment_provenance = {
        "entity_type": "Provenance",
        "schema_version": "1.0",
        "provenance_id": experiment_prov_id,
        "subject_refs": [
            _subject("Run", experiment_run_id),
            _subject("Artifact", raw_artifact_id),
            *[_subject("Observable", identifier) for identifier in observable_ids.values()],
        ],
        "source_type": "experiment",
        "sources": [
            {
                "source_id": sha256_file(source_path),
                "source_type": "file",
                "title": source_path.name,
            }
        ],
        "software": [
            {
                "name": str(intake["experiment_context"].get("instrument", "not_recorded")),
                "version": str(intake["experiment_context"].get("instrument_version", "not_recorded")),
                "role": "measurement acquisition",
            }
        ],
        "scripts": [],
        "people": [
            {"person_id": str(intake.get("operator_id", "local_user")), "role": "experiment operator"}
        ],
        "generated_at": source_time,
    }
    processing_provenance = {
        "entity_type": "Provenance",
        "schema_version": "1.0",
        "provenance_id": processing_prov_id,
        "subject_refs": [
            _subject("Run", processing_run_id),
            _subject("Artifact", h5_artifact_id),
            _subject("Artifact", script_artifact_id),
            _subject("Artifact", intake_artifact_id),
            _subject("Artifact", checks_artifact_id),
            *[_subject("Metric", identifier) for identifier in metric_ids.values()],
        ],
        "source_type": "data_processing",
        "sources": [
            {
                "source_id": sha256_file(source_path),
                "source_type": "file",
                "title": source_path.name,
            }
        ],
        "software": [
            {
                "name": "DADC Tabular Experiment Adapter",
                "version": ADAPTER_VERSION,
                "role": "deterministic CSV normalization and metric extraction",
            }
        ],
        "scripts": [script_artifact_id],
        "people": [
            {"person_id": str(intake.get("operator_id", "local_user")), "role": "data importer"}
        ],
        "generated_at": processed,
    }
    _write_record(root, case_id, experiment_provenance)
    _write_record(root, case_id, processing_provenance)

    _artifact(
        root,
        case_id,
        raw_path,
        raw_artifact_id,
        [
            _subject("Run", experiment_run_id),
            _subject("Run", processing_run_id),
            _subject("DesignRevision", revision_id),
        ],
        "measurement_data",
        "text/csv",
        "raw_experiment_output",
        experiment_prov_id,
        source_time,
    )
    _artifact(
        root,
        case_id,
        h5_path,
        h5_artifact_id,
        [
            _subject("Run", processing_run_id),
            *[_subject("Observable", identifier) for identifier in observable_ids.values()],
        ],
        "result_hdf5",
        "application/x-hdf5",
        "calculated",
        processing_prov_id,
        processed,
    )
    _artifact(
        root,
        case_id,
        script_path,
        script_artifact_id,
        [_subject("Run", processing_run_id)],
        "script",
        "text/x-python",
        "manual_entry",
        processing_prov_id,
        processed,
    )
    _artifact(
        root,
        case_id,
        intake_path,
        intake_artifact_id,
        [_subject("DesignRevision", revision_id), _subject("Run", processing_run_id)],
        "report",
        "application/json",
        "manual_entry",
        processing_prov_id,
        processed,
    )
    _artifact(
        root,
        case_id,
        checks_path,
        checks_artifact_id,
        [
            _subject("Validation", f"val_{slug}_schema"),
            _subject("Validation", f"val_{slug}_tabular_integrity"),
        ],
        "validation_evidence",
        "application/json",
        "calculated",
        processing_prov_id,
        processed,
    )

    device = {
        "entity_type": "Device",
        "schema_version": "1.0",
        "device_id": device_id,
        "name": str(intake["device_name"]),
        "device_class": str(intake["device_class"]),
        "device_subtype": str(intake["device_subtype"]),
        "physics_domains": list(intake["physics_domains"]),
        "profile_schema": "device_profiles/generic_component.schema.json",
        "extensions": {
            "generic_component": {
                "identity_basis": "explicit_intake_manifest",
                "attributes": dict(intake.get("device_attributes", {})),
            }
        },
        "tags": ["experimental_csv", "generic_device_profile"],
        "created_at": processed,
    }
    revision = {
        "entity_type": "DesignRevision",
        "schema_version": "1.0",
        "design_revision_id": revision_id,
        "device_id": device_id,
        "revision_label": str(intake.get("revision_label", "experiment_sample_001")),
        "geometry": {
            "representation": "analytical",
            "parameters": list(intake.get("design_parameters", [])),
        },
        "materials": [],
        "topology": {
            "reconstruction_status": "not_provided_in_tabular_measurement",
            "identity_basis": "explicit_intake_manifest",
        },
        "artifact_ids": [raw_artifact_id, intake_artifact_id],
        "created_at": processed,
    }
    study = {
        "entity_type": "Study",
        "schema_version": "1.0",
        "study_id": study_id,
        "device_id": device_id,
        "design_revision_ids": [revision_id],
        "study_type": "validation",
        "physics_domains": list(intake["physics_domains"]),
        "objectives": [
            {"metric": str(metric["quantity"]), "operation": str(metric["operation"])}
            for metric in contract["metrics"]
        ],
        "run_ids": [experiment_run_id, processing_run_id],
        "created_at": processed,
    }
    experiment_run = {
        "entity_type": "Run",
        "schema_version": "1.0",
        "run_id": experiment_run_id,
        "study_id": study_id,
        "design_revision_id": revision_id,
        "activity_type": "experiment_run",
        "status": "succeeded",
        "physics_domains": list(intake["physics_domains"]),
        "started_at": source_time,
        "ended_at": source_time,
        "input_artifact_ids": [intake_artifact_id],
        "output_artifact_ids": [raw_artifact_id],
        "provenance_id": experiment_prov_id,
        "environment": {
            "platform": str(intake.get("platform", "laboratory_instrument")),
            "compute": str(intake.get("compute", "not_applicable")),
        },
        "source_context": {"experiment": dict(intake["experiment_context"])},
    }
    processing_run = {
        "entity_type": "Run",
        "schema_version": "1.0",
        "run_id": processing_run_id,
        "study_id": study_id,
        "design_revision_id": revision_id,
        "activity_type": "data_processing",
        "status": "succeeded",
        "physics_domains": list(intake["physics_domains"]),
        "started_at": processed,
        "ended_at": processed,
        "input_artifact_ids": [raw_artifact_id, script_artifact_id, intake_artifact_id],
        "output_artifact_ids": [h5_artifact_id, checks_artifact_id],
        "provenance_id": processing_prov_id,
        "environment": {
            "platform": str(intake.get("platform", "local_python")),
            "compute": str(intake.get("compute", "local_cpu")),
        },
        "source_context": {
            "processing": {
                "adapter": "tabular_experiment_csv",
                "adapter_version": ADAPTER_VERSION,
                "normalization": "declared numeric CSV columns copied to HDF5 without unit conversion",
            }
        },
    }
    for record in (device, revision, study, experiment_run, processing_run):
        _write_record(root, case_id, record)

    axis_ref = _h5_ref(root, h5_path, f"/axes/{axis_name}")
    for observable in contract["observables"]:
        suffix = str(observable["id_suffix"])
        representation = str(observable.get("complex_representation", "not_applicable"))
        record = {
            "entity_type": "Observable",
            "schema_version": "1.0",
            "observable_id": observable_ids[suffix],
            "run_id": experiment_run_id,
            "observable_type": str(observable.get("observable_type", "curve")),
            "quantity": str(observable["quantity"]),
            "axes": [
                {
                    "name": axis_name,
                    "unit": str(contract["axis"]["unit"]),
                    "data_ref": axis_ref,
                }
            ],
            "components": [str(item["name"]) for item in observable["components"]],
            "complex_representation": representation,
            "data_ref": _h5_ref(root, h5_path, f"/observables/{suffix}"),
            "artifact_id": h5_artifact_id,
            "coordinate_system_ref": None,
            "value_origin": "raw_experiment_output",
            "provenance_id": experiment_prov_id,
        }
        _write_record(root, case_id, record)

    for metric in metric_specs:
        suffix = str(metric["id_suffix"])
        observable_suffix = str(metric["observable"])
        record = {
            "entity_type": "Metric",
            "schema_version": "1.0",
            "metric_id": metric_ids[suffix],
            "run_id": processing_run_id,
            "name": str(metric["name"]),
            "quantity": str(metric["quantity"]),
            "value": float(metric["value"]),
            "unit": str(metric["unit"]),
            "value_origin": "calculated",
            "source_observable_ids": [observable_ids[observable_suffix]],
            "extraction_method": (
                f"{metric['operation']} over component {metric['component']} from declared CSV columns"
            ),
            "calculation": {
                "operation": str(metric["operation"]),
                "component": str(metric["component"]),
                "script_artifact_id": script_artifact_id,
            },
            "provenance_id": processing_prov_id,
        }
        _write_record(root, case_id, record)

    validations = [
        {
            "entity_type": "Validation",
            "schema_version": "1.0",
            "validation_id": f"val_{slug}_schema",
            "subject_refs": [_subject("Device", device_id), _subject("Study", study_id)],
            "validation_type": "schema_validation",
            "method": {
                "name": "DADC V1.0 repository validation",
                "description": "Validate all normalized records, references, HDF5 paths, indexes, and hashes.",
                "script_artifact_id": script_artifact_id,
            },
            "threshold": {
                "name": "schema_error_count",
                "operator": "==",
                "value": 0,
                "unit": "count",
            },
            "result": {
                "status": "passed",
                "summary": "The staged tabular repository passed the frozen DADC V1.0 checks.",
                "measured_values": [{"schema_error_count": 0}],
            },
            "evidence_artifact_ids": [checks_artifact_id],
            "executed_at": processed,
            "provenance_id": processing_prov_id,
        },
        {
            "entity_type": "Validation",
            "schema_version": "1.0",
            "validation_id": f"val_{slug}_tabular_integrity",
            "subject_refs": [
                _subject("Run", experiment_run_id),
                *[_subject("Observable", identifier) for identifier in observable_ids.values()],
            ],
            "validation_type": "physical_rule_check",
            "method": {
                "name": "tabular numeric integrity",
                "description": "Reject missing, nonnumeric, nonfinite, or nonmonotonic declared curve data.",
                "script_artifact_id": script_artifact_id,
            },
            "threshold": {
                "name": "numeric_nonfinite_count",
                "operator": "==",
                "value": 0,
                "unit": "count",
            },
            "result": {
                "status": "passed",
                "summary": "All declared columns are finite and the declared axis is strictly increasing.",
                "measured_values": [
                    {
                        "numeric_nonfinite_count": 0,
                        "row_count": int(axis.size),
                        "axis_strictly_increasing": bool(np.all(np.diff(axis) > 0.0)),
                    }
                ],
            },
            "evidence_artifact_ids": [checks_artifact_id],
            "executed_at": processed,
            "provenance_id": processing_prov_id,
        },
    ]
    _write_json(
        case / "validation.json",
        {"schema_version": "1.0", "validations": validations},
    )
    _write_json(
        root / "repository.json",
        {
            "repository_id": f"dadc_{case_id}",
            "schema_version": "1.0",
            "created_at": processed,
            "cases": [{"case_id": case_id, "path": f"cases/{case_id}"}],
            "indexes": [
                {"name": "catalog", "path": "index/catalog.parquet", "format": "parquet"},
                {"name": "metrics", "path": "index/metrics.parquet", "format": "parquet"},
            ],
        },
    )
    rebuild_indexes(root)
    validation = DADCRepository(root).validate()
    if not validation.valid:
        raise RuntimeError(json.dumps(validation.to_dict(), ensure_ascii=False, indent=2))
    return TabularIngestionResult(
        repository=root,
        case_id=case_id,
        source_sha256=sha256_file(source_path),
        row_count=int(axis.size),
        observable_count=len(observable_ids),
        metrics=metrics,
        validation=validation.to_dict(),
    )
