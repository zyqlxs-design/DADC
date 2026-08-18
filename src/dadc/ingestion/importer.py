"""Build a validated DADC repository from a real Touchstone filter result."""

from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import h5py
import numpy as np
from ..constants import ENTITY_ID_FIELDS
from ..indexing import rebuild_indexes
from ..integrity import artifact_file_record, sha256_file
from ..repository import DADCRepository
from .touchstone import TouchstoneData, parse_touchstone

ADAPTER_VERSION = "1.0.0"
PASSIVITY_TOLERANCE = 1.001
RECIPROCITY_TOLERANCE = 1.0e-6

_CASE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_TIMEZONE = re.compile(r"^(?P<sign>[+-])(?P<hours>\d{2}):(?P<minutes>\d{2})$")


@dataclass(frozen=True)
class TouchstoneIngestionResult:
    repository: Path
    case_id: str
    source_sha256: str
    source_format: str
    frequency_points: int
    port_count: int
    metrics: dict[str, float]
    physical_checks: dict[str, float | bool]
    validation: dict[str, Any]


def _subject(entity_type: str, entity_id: str) -> dict[str, str]:
    return {"entity_type": entity_type, "entity_id": entity_id}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _write_record(root: Path, case_id: str, record: dict[str, Any]) -> None:
    entity_type = record["entity_type"]
    identifier = record[ENTITY_ID_FIELDS[entity_type]]
    path = root / "cases" / case_id / "metadata" / entity_type.lower() / f"{identifier}.json"
    _write_json(path, record)


def _h5_ref(root: Path, path: Path, object_path: str) -> str:
    return f"{path.relative_to(root).as_posix()}:{object_path}"


def _artifact(
    root: Path,
    case_id: str,
    path: Path,
    artifact_id: str,
    subject_refs: list[dict[str, str]],
    role: str,
    media_type: str,
    value_origin: str,
    provenance_id: str,
    created_at: str,
) -> dict[str, Any]:
    record = artifact_file_record(
        root,
        path,
        artifact_id=artifact_id,
        subject_refs=subject_refs,
        artifact_role=role,
        media_type=media_type,
        immutable=True,
        value_origin=value_origin,
        provenance_id=provenance_id,
        created_at=created_at,
    )
    _write_record(root, case_id, record)
    return record


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timezone_from_offset(offset: str) -> timezone:
    match = _TIMEZONE.fullmatch(offset)
    if not match:
        raise ValueError("source_timezone must use +HH:MM or -HH:MM")
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    if hours > 23 or minutes > 59:
        raise ValueError(f"Invalid source timezone offset: {offset}")
    sign = 1 if match.group("sign") == "+" else -1
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _require_aware_datetime(value: str, field_name: str) -> str:
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC 3339 date-time") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include an explicit UTC offset or Z")
    return value


def _source_time(data: TouchstoneData, source_timezone: str, fallback: str) -> tuple[str, str]:
    raw = data.metadata.get("generated")
    if not raw:
        return fallback, "processing_time_fallback; Touchstone Generated comment missing"
    try:
        parsed = datetime.strptime(raw, "%I:%M:%S %p %b %d, %Y")
    except ValueError:
        return fallback, f"processing_time_fallback; unparsed Touchstone time {raw!r}"
    aware = parsed.replace(tzinfo=_timezone_from_offset(source_timezone))
    return aware.isoformat(), f"Touchstone Generated comment with explicit importer offset {source_timezone}"


def _largest_true_segment(mask: np.ndarray) -> tuple[int, int]:
    indices = np.flatnonzero(mask)
    if not len(indices):
        raise ValueError("No frequency point satisfies the -3 dB passband rule")
    splits = np.where(np.diff(indices) > 1)[0] + 1
    groups = np.split(indices, splits)
    segment = max(groups, key=len)
    return int(segment[0]), int(segment[-1])


def _filter_metrics(data: TouchstoneData) -> dict[str, float]:
    if data.port_count < 2:
        raise ValueError("An RF filter ingestion requires at least two ports")
    transmission = data.values[:, 1, 0]
    transmission_db = 20.0 * np.log10(np.maximum(np.abs(transmission), 1.0e-300))
    lower_index, upper_index = _largest_true_segment(transmission_db >= -3.0)
    lower = float(data.frequencies_hz[lower_index])
    upper = float(data.frequencies_hz[upper_index])
    return {
        "lower_3db_frequency": lower,
        "upper_3db_frequency": upper,
        "bandwidth_3db": upper - lower,
        "center_frequency_3db": (lower + upper) / 2.0,
        "peak_transmission_db": float(np.max(transmission_db)),
    }


def _antenna_metrics(data: TouchstoneData) -> dict[str, float]:
    if data.port_count != 1:
        raise ValueError("A single-feed antenna Touchstone ingestion requires exactly one port")
    reflection_db = 20.0 * np.log10(np.maximum(np.abs(data.values[:, 0, 0]), 1.0e-300))
    resonance_index = int(np.argmin(reflection_db))
    return {
        "resonance_frequency": float(data.frequencies_hz[resonance_index]),
        "minimum_return_loss_db": float(reflection_db[resonance_index]),
    }


def _inductor_products(data: TouchstoneData) -> dict[str, np.ndarray]:
    """Calculate explicitly defined differential two-port products.

    This is a mathematical S-to-Z transformation, not an assertion that the
    vendor datasheet used the same fixture or extraction method. The distinction
    is retained in Observable derivation text and in the literature metadata.
    """

    if data.port_count != 2:
        raise ValueError("An inductor Touchstone ingestion requires exactly two ports")
    identity = np.eye(2, dtype=np.complex128)
    z_matrix = np.empty_like(data.values)
    for index, scattering in enumerate(data.values):
        try:
            z_matrix[index] = (
                data.reference_impedance_ohm
                * (identity + scattering)
                @ np.linalg.inv(identity - scattering)
            )
        except np.linalg.LinAlgError as exc:
            raise ValueError(
                f"S-to-Z conversion is singular at {data.frequencies_hz[index]:.17g} Hz"
            ) from exc
    differential_impedance = (
        z_matrix[:, 0, 0]
        + z_matrix[:, 1, 1]
        - z_matrix[:, 0, 1]
        - z_matrix[:, 1, 0]
    )
    angular_frequency = 2.0 * np.pi * data.frequencies_hz
    effective_inductance = np.imag(differential_impedance) / angular_frequency
    effective_q = np.divide(
        np.abs(np.imag(differential_impedance)),
        np.real(differential_impedance),
        out=np.full(len(differential_impedance), np.nan, dtype=np.float64),
        where=np.real(differential_impedance) > 0.0,
    )
    if not (
        np.all(np.isfinite(differential_impedance))
        and np.all(np.isfinite(effective_inductance))
        and np.all(np.isfinite(effective_q))
    ):
        raise ValueError("Inductor derived products contain NaN or infinity")
    return {
        "differential_impedance": differential_impedance,
        "effective_inductance": effective_inductance,
        "effective_q": effective_q,
    }


def _interpolate_at(
    frequencies_hz: np.ndarray,
    values: np.ndarray,
    target_frequency_hz: float,
) -> float:
    if not frequencies_hz[0] <= target_frequency_hz <= frequencies_hz[-1]:
        raise ValueError(
            f"Reference frequency {target_frequency_hz:.17g} Hz is outside the source sweep"
        )
    return float(np.interp(target_frequency_hz, frequencies_hz, values))


def _source_schemas() -> Path:
    candidate = Path(__file__).resolve().parents[3] / "schemas"
    if candidate.is_dir():
        return candidate
    candidate = Path(sys.prefix) / "share" / "dadc" / "schemas"
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError("Bundled DADC schemas were not found")


def _ingest_touchstone_repository(
    source: str | Path,
    target: str | Path,
    *,
    case_id: str,
    device_name: str,
    device_class: str,
    device_subtype: str,
    profile_extension: dict[str, Any],
    topology_family: str,
    metric_family: str,
    source_timezone: str,
    companion_artifacts: list[dict[str, Any]] | None = None,
    operator_id: str = "local_user",
    platform: str = "windows",
    compute: str = "not_recorded",
    solver_edition: str = "Student",
    processed_at: str | None = None,
    source_activity_type: str = "simulation_run",
    source_timestamp: str | None = None,
    source_run_suffix: str = "hfss",
    source_provenance_suffix: str = "hfss",
    source_artifact_role: str = "raw_input",
    source_value_origin: str = "raw_solver_output",
    source_provenance_type: str = "simulation",
    source_context: dict[str, Any] | None = None,
    source_software: list[dict[str, str]] | None = None,
    source_operator_role: str = "simulation operator",
    source_citation: str | None = None,
    device_tags: list[str] | None = None,
    revision_label: str | None = None,
    study_objective: str | None = None,
    literature_context: dict[str, Any] | None = None,
    metric_reference_frequencies_hz: dict[str, float] | None = None,
) -> TouchstoneIngestionResult:
    """Create one new repository from an immutable real ``.sNp`` file.

    The target must not already contain data. The source is copied byte-for-byte,
    and all normalized values are placed in a separate HDF5 artifact.
    """

    if not _CASE_ID.fullmatch(case_id):
        raise ValueError("case_id must match ^[a-z][a-z0-9_]{2,63}$")
    _timezone_from_offset(source_timezone)
    source_path = Path(source).resolve()
    data = parse_touchstone(source_path)
    root = Path(target).resolve()
    if root.exists():
        if root.is_file() or any(root.iterdir()):
            raise FileExistsError(f"Refusing to overwrite non-empty target: {root}")
    else:
        root.mkdir(parents=True)

    processed = _require_aware_datetime(processed_at or _utc_now(), "processed_at")
    if source_activity_type not in {"simulation_run", "experiment_run"}:
        raise ValueError("Touchstone primary source must be simulation_run or experiment_run")
    if source_activity_type == "simulation_run":
        source_time, timestamp_basis = _source_time(data, source_timezone, processed)
    else:
        source_time = _require_aware_datetime(
            source_timestamp or processed,
            "source_timestamp",
        )
        timestamp_basis = (
            "explicit source timestamp supplied by intake"
            if source_timestamp
            else "processing_time_fallback; measurement timestamp not recorded"
        )
    source_hash = sha256_file(source_path)
    case = root / "cases" / case_id
    raw_path = case / "raw" / source_path.name
    h5_path = case / "data" / "results.h5"
    script_path = case / "scripts" / "touchstone_adapter.py"
    evidence_path = case / "evidence" / "touchstone_checks.json"
    for directory in (raw_path.parent, h5_path.parent, script_path.parent, evidence_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, raw_path)
    if sha256_file(raw_path) != source_hash:
        raise IOError("Raw Touchstone copy failed byte-integrity verification")
    shutil.copyfile(Path(__file__).with_name("touchstone.py"), script_path)
    shutil.copytree(_source_schemas(), root / "schemas")

    slug = case_id
    device_id = f"device_{slug}"
    revision_id = f"rev_{slug}_001"
    study_id = f"study_{slug}_frequency_sweep"
    source_run_id = f"run_{slug}_{source_run_suffix}"
    processing_run_id = f"run_{slug}_touchstone_import"
    observable_id = f"obs_{slug}_s_parameters_complex"
    impedance_observable_id = f"obs_{slug}_differential_impedance_complex"
    inductance_observable_id = f"obs_{slug}_effective_inductance"
    q_observable_id = f"obs_{slug}_effective_q"
    source_prov_id = f"prov_{slug}_{source_provenance_suffix}"
    processing_prov_id = f"prov_{slug}_touchstone_import"
    raw_artifact_id = f"art_{slug}_touchstone_raw"
    h5_artifact_id = f"art_{slug}_results_h5"
    script_artifact_id = f"art_{slug}_adapter_script"
    evidence_artifact_id = f"art_{slug}_physical_checks"
    passivity_validation_id = f"val_{slug}_passivity"
    reciprocity_validation_id = f"val_{slug}_reciprocity"
    literature_run_id = f"run_{slug}_datasheet_record"
    literature_prov_id = f"prov_{slug}_datasheet_record"

    companions: list[dict[str, Any]] = []
    for index, specification in enumerate(companion_artifacts or [], start=1):
        if not isinstance(specification, dict) or not specification.get("path"):
            raise ValueError("Each companion_artifacts item requires a path")
        companion_source = Path(str(specification["path"])).resolve()
        if not companion_source.is_file():
            raise FileNotFoundError(f"Companion artifact is not a file: {companion_source}")
        if companion_source == source_path:
            raise ValueError("The primary Touchstone source must not be repeated as a companion artifact")
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", companion_source.stem).strip("_") or "file"
        companion_id = f"art_{slug}_companion_{index:03d}_{safe_stem}"
        companion_target = raw_path.parent / f"companion_{index:03d}_{companion_source.name}"
        shutil.copyfile(companion_source, companion_target)
        if sha256_file(companion_target) != sha256_file(companion_source):
            raise IOError(f"Companion copy failed byte-integrity verification: {companion_source}")
        companions.append(
            {
                "source": companion_source,
                "target": companion_target,
                "artifact_id": companion_id,
                "role": str(specification.get("role", "native_project")),
                "media_type": str(specification.get("media_type", "application/octet-stream")),
                "value_origin": str(specification.get("value_origin", "raw_solver_output")),
                "activity_scope": str(specification.get("activity_scope", "source")),
            }
        )

    literature_companions = [item for item in companions if item["role"] == "literature_source"]
    processing_companions = [
        item
        for item in companions
        if item["role"] != "literature_source" and item["activity_scope"] == "processing"
    ]
    source_companions = [
        item
        for item in companions
        if item["role"] != "literature_source" and item["activity_scope"] != "processing"
    ]
    if literature_companions and not literature_context:
        raise ValueError(
            "A literature_source companion requires explicit literature_context; "
            "literature evidence must not be attached to a simulation or experiment Run"
        )
    if literature_context and not literature_companions:
        raise ValueError("literature_context requires at least one literature_source companion")
    if metric_family == "inductor":
        if len(literature_companions) != 1:
            raise ValueError("Inductor intake requires exactly one datasheet literature_source")
        datasheet_artifact_id = literature_companions[0]["artifact_id"]
        for specification in profile_extension.get("datasheet_specifications", []):
            specification["source_artifact_id"] = datasheet_artifact_id

    inductor_products = _inductor_products(data) if metric_family == "inductor" else None
    with h5py.File(h5_path, "x") as handle:
        axes = handle.create_group("axes")
        frequency = axes.create_dataset("frequency", data=data.frequencies_hz)
        frequency.attrs["unit"] = "Hz"
        group = handle.create_group("observables/s_parameters")
        group.create_dataset("real", data=np.real(data.values), compression="gzip")
        group.create_dataset("imaginary", data=np.imag(data.values), compression="gzip")
        group.attrs["complex_representation"] = "real_imaginary"
        group.attrs["source_complex_format"] = data.source_complex_format
        group.attrs["source_text_encoding"] = data.source_text_encoding
        group.attrs["source_option_line"] = data.option_line
        group.attrs["source_sha256"] = source_hash
        group.attrs["components"] = json.dumps(data.components)
        group.attrs["port_names"] = json.dumps(data.port_names)
        group.attrs["matrix_axis_order"] = json.dumps(["frequency", "port_out", "port_in"])
        group.attrs["reference_impedance_ohm"] = data.reference_impedance_ohm
        if inductor_products is not None:
            impedance = handle.create_group("observables/differential_impedance")
            impedance.create_dataset(
                "real",
                data=np.real(inductor_products["differential_impedance"]),
                compression="gzip",
            )
            impedance.create_dataset(
                "imaginary",
                data=np.imag(inductor_products["differential_impedance"]),
                compression="gzip",
            )
            impedance.attrs["unit"] = "ohm"
            impedance.attrs["complex_representation"] = "real_imaginary"
            impedance.attrs["method"] = (
                "Z=Z0*(I+S)*inv(I-S); Zdiff=Z11+Z22-Z12-Z21"
            )
            inductance = handle.create_dataset(
                "observables/effective_inductance",
                data=inductor_products["effective_inductance"],
                compression="gzip",
            )
            inductance.attrs["unit"] = "H"
            inductance.attrs["method"] = "imag(Zdiff)/(2*pi*frequency)"
            quality = handle.create_dataset(
                "observables/effective_q",
                data=inductor_products["effective_q"],
                compression="gzip",
            )
            quality.attrs["unit"] = "1"
            quality.attrs["method"] = "abs(imag(Zdiff))/real(Zdiff), for real(Zdiff)>0"

    singular_values = np.linalg.svd(data.values, compute_uv=False)
    max_singular_value = float(np.max(singular_values))
    reciprocity_error = float(np.max(np.abs(data.values - np.swapaxes(data.values, 1, 2))))
    # Apply one declared screen to simulation and measurement data alike. A
    # measured file may legitimately fail because of noise, fixture residuals,
    # or model conversion. The Case remains ingestible; Validation records the
    # failure instead of silently widening thresholds to make it pass.
    passivity_tolerance = PASSIVITY_TOLERANCE
    reciprocity_tolerance = RECIPROCITY_TOLERANCE
    checks = {
        "adapter": {"name": "DADC Touchstone Adapter", "version": ADAPTER_VERSION},
        "source_sha256": source_hash,
        "source_option_line": data.option_line,
        "source_complex_format": data.source_complex_format,
        "source_text_encoding": data.source_text_encoding,
        "canonical_complex_format": "real_imaginary",
        "frequency_points": int(len(data.frequencies_hz)),
        "port_count": data.port_count,
        "frequency_strictly_increasing": bool(np.all(np.diff(data.frequencies_hz) > 0.0)),
        "all_values_finite": bool(np.all(np.isfinite(data.values))),
        "maximum_singular_value": max_singular_value,
        "passivity_threshold": passivity_tolerance,
        "maximum_reciprocity_error": reciprocity_error,
        "reciprocity_threshold": reciprocity_tolerance,
    }
    _write_json(evidence_path, checks)

    manifest = {
        "repository_id": f"dadc_{slug}_repository",
        "schema_version": "1.0",
        "created_at": processed,
        "cases": [{"case_id": case_id, "path": f"cases/{case_id}"}],
        "indexes": [
            {"name": "global_catalog", "path": "index/catalog.parquet", "format": "parquet"},
            {"name": "metric_query", "path": "index/metrics.parquet", "format": "parquet"},
        ],
    }
    _write_json(root / "repository.json", manifest)

    if source_context is None:
        source_context = {
            "solver": {
                "name": "Ansys HFSS",
                "version": data.metadata.get("hfss_version", "not_recorded"),
                "edition": solver_edition,
                "project": data.metadata.get("project", "not_recorded"),
                "design": data.metadata.get("design", "not_recorded"),
                "setup": data.metadata.get("setup", "not_recorded"),
                "solution": data.metadata.get("solution", "not_recorded"),
                "source_generated": data.metadata.get("generated", "not_recorded"),
                "timestamp_basis": timestamp_basis,
                "time_semantics": (
                    "Touchstone export time is used for required Run timestamps; "
                    "actual solver start and end were not exported"
                ),
                "duration": "not_recorded",
            }
        }
    expected_context_key = "solver" if source_activity_type == "simulation_run" else "experiment"
    if set(source_context).intersection({"solver", "experiment", "literature", "processing", "optimization"}) != {
        expected_context_key
    }:
        raise ValueError(
            f"{source_activity_type} requires source_context.{expected_context_key} only"
        )
    if source_software is None:
        source_software = [
            {
                "name": "Ansys HFSS",
                "version": data.metadata.get("hfss_version", "not_recorded"),
                "role": f"electromagnetic solver ({solver_edition})",
            }
        ]

    source_provenance = {
        "entity_type": "Provenance",
        "schema_version": "1.0",
        "provenance_id": source_prov_id,
        "subject_refs": [
            _subject("Run", source_run_id),
            _subject("Artifact", raw_artifact_id),
            *[_subject("Artifact", item["artifact_id"]) for item in source_companions],
        ],
        "source_type": source_provenance_type,
        "sources": [
            {
                "source_id": source_hash,
                "source_type": "file",
                "title": source_path.name,
                "citation": source_citation
                or "Raw Touchstone exported by HFSS; preserved byte-for-byte.",
            },
            *[
                {
                    "source_id": sha256_file(item["source"]),
                    "source_type": "file",
                    "title": item["source"].name,
                    "citation": "Companion source evidence preserved byte-for-byte.",
                }
                for item in source_companions
            ],
        ],
        "software": source_software,
        "scripts": [],
        "people": [{"person_id": operator_id, "role": source_operator_role}],
        "generated_at": source_time,
    }
    derived_observable_ids = (
        [impedance_observable_id, inductance_observable_id, q_observable_id]
        if metric_family == "inductor"
        else []
    )
    processing_provenance = {
        "entity_type": "Provenance",
        "schema_version": "1.0",
        "provenance_id": processing_prov_id,
        "subject_refs": [
            _subject("Run", processing_run_id),
            _subject("Observable", observable_id),
            *[_subject("Observable", item) for item in derived_observable_ids],
            *[_subject("Artifact", item["artifact_id"]) for item in processing_companions],
        ],
        "source_type": "data_processing",
        "sources": [
            {
                "source_id": source_hash,
                "source_type": "file",
                "title": source_path.name,
                "citation": f"Parsed from {data.source_complex_format} and normalized to real/imaginary.",
            },
            *[
                {
                    "source_id": sha256_file(item["source"]),
                    "source_type": "file",
                    "title": item["source"].name,
                    "citation": "Source-acquisition evidence used by the deterministic import.",
                }
                for item in processing_companions
            ],
        ],
        "software": [
            {"name": "DADC Touchstone Adapter", "version": ADAPTER_VERSION, "role": "deterministic import"}
        ],
        "scripts": [script_artifact_id],
        "people": [{"person_id": operator_id, "role": "data importer"}],
        "generated_at": processed,
    }
    _write_record(root, case_id, source_provenance)
    if literature_companions:
        literature_time = _require_aware_datetime(
            str(literature_context["published_at"]),
            "literature_context.published_at",
        )
        literature_provenance = {
            "entity_type": "Provenance",
            "schema_version": "1.0",
            "provenance_id": literature_prov_id,
            "subject_refs": [
                _subject("Run", literature_run_id),
                *[_subject("Artifact", item["artifact_id"]) for item in literature_companions],
            ],
            "source_type": "literature",
            "sources": [
                {
                    "source_id": sha256_file(item["source"]),
                    "source_type": "datasheet",
                    "title": item["source"].name,
                    "citation": str(literature_context["citation"]),
                    "uri": str(literature_context["uri"]),
                    "accessed_at": str(literature_context["accessed_at"]),
                }
                for item in literature_companions
            ],
            "software": [],
            "scripts": [],
            "people": [{"person_id": operator_id, "role": "literature importer"}],
            "generated_at": literature_time,
        }
        _write_record(root, case_id, literature_provenance)
    _write_record(root, case_id, processing_provenance)

    _artifact(
        root,
        case_id,
        raw_path,
        raw_artifact_id,
        [_subject("Run", source_run_id), _subject("Run", processing_run_id), _subject("DesignRevision", revision_id)],
        source_artifact_role,
        "text/plain; profile=touchstone",
        source_value_origin,
        source_prov_id,
        source_time,
    )
    for item in companions:
        is_literature = item in literature_companions
        is_processing = item in processing_companions
        _artifact(
            root,
            case_id,
            item["target"],
            item["artifact_id"],
            [
                _subject(
                    "Run",
                    literature_run_id
                    if is_literature
                    else processing_run_id
                    if is_processing
                    else source_run_id,
                ),
                _subject("DesignRevision", revision_id),
            ],
            item["role"],
            item["media_type"],
            item["value_origin"],
            literature_prov_id
            if is_literature
            else processing_prov_id
            if is_processing
            else source_prov_id,
            literature_time if is_literature else processed if is_processing else source_time,
        )
    _artifact(
        root,
        case_id,
        h5_path,
        h5_artifact_id,
        [
            _subject("Run", processing_run_id),
            _subject("Observable", observable_id),
            *[_subject("Observable", item) for item in derived_observable_ids],
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
        evidence_path,
        evidence_artifact_id,
        (
            [_subject("Validation", passivity_validation_id), _subject("Validation", reciprocity_validation_id)]
            if data.port_count > 1
            else [_subject("Validation", passivity_validation_id)]
        ),
        "validation_evidence",
        "application/json",
        "calculated",
        processing_prov_id,
        processed,
    )

    source_variables = {
        name: {
            "value": value["value"],
            "unit": str(value["unit"]),
            "value_origin": "unknown_in_source",
            "evidence": "Touchstone Variables comment",
        }
        for name, value in sorted(data.variables.items())
    }
    has_native_project = any(item["role"] == "native_project" for item in companions)
    if source_activity_type == "experiment_run":
        reconstruction_status = "measurement_and_datasheet_preserved_not_structurally_reconstructed"
        missing_evidence = [
            "machine_readable_internal_geometry",
            "machine_readable_material_assignments",
            "complete_measurement_fixture_geometry",
        ]
    elif has_native_project:
        reconstruction_status = "native_project_preserved_not_structurally_extracted"
        missing_evidence = [
            "normalized_geometry_parameter_extraction",
            "normalized_material_assignment_extraction",
            "normalized_boundary_and_excitation_extraction",
        ]
    else:
        reconstruction_status = "incomplete_from_touchstone_only"
        missing_evidence = [
            "native_aedt_project_or_geometry_export",
            "material_assignments",
            "boundary_and_excitation_definitions",
        ]
    records: list[dict[str, Any]] = [
        {
            "entity_type": "Device",
            "schema_version": "1.0",
            "device_id": device_id,
            "name": device_name,
            "device_class": device_class,
            "device_subtype": device_subtype,
            "physics_domains": ["electromagnetics"],
            "profile_schema": f"device_profiles/{device_class}.schema.json",
            "extensions": {device_class: {**profile_extension, "port_count": data.port_count}},
            "tags": device_tags or ["real_solver_data", "hfss", "touchstone"],
            "created_at": source_time,
        },
        {
            "entity_type": "DesignRevision",
            "schema_version": "1.0",
            "design_revision_id": revision_id,
            "device_id": device_id,
            "revision_label": revision_label or "HFSS-export-001",
            "geometry": {
                "representation": "parametric",
                "coordinate_system_ref": (
                    "hfss_global_coordinate_system"
                    if source_activity_type == "simulation_run"
                    else "measurement_reference_planes"
                ),
                "parameters": [],
            },
            "materials": [],
            "topology": {
                "family": topology_family,
                "source_project": data.metadata.get("project", "not_recorded"),
                "source_design": data.metadata.get("design", "not_recorded"),
                "port_names": list(data.port_names),
                "source_variables": source_variables,
                "reconstruction_status": reconstruction_status,
                "missing_evidence": missing_evidence,
            },
            "artifact_ids": [raw_artifact_id, *[item["artifact_id"] for item in companions]],
            "created_at": source_time,
        },
        {
            "entity_type": "Study",
            "schema_version": "1.0",
            "study_id": study_id,
            "device_id": device_id,
            "design_revision_ids": [revision_id],
            "study_type": "parameter_sweep",
            "physics_domains": ["electromagnetics"],
            "objectives": [
                {
                    "metric": study_objective
                    or ("transmission" if metric_family == "rf_filter" else "input_match"),
                    "operator": "characterize",
                }
            ],
            "parameter_space": [
                {
                    "name": "frequency",
                    "start": float(data.frequencies_hz[0]),
                    "stop": float(data.frequencies_hz[-1]),
                    "points": int(len(data.frequencies_hz)),
                    "unit": "Hz",
                }
            ],
            "run_ids": [
                source_run_id,
                *([literature_run_id] if literature_companions else []),
                processing_run_id,
            ],
            "created_at": source_time,
        },
        {
            "entity_type": "Run",
            "schema_version": "1.0",
            "run_id": source_run_id,
            "study_id": study_id,
            "design_revision_id": revision_id,
            "activity_type": source_activity_type,
            "status": "succeeded",
            "physics_domains": ["electromagnetics"],
            "started_at": source_time,
            "ended_at": source_time,
            "input_artifact_ids": [],
            "output_artifact_ids": [
                raw_artifact_id,
                *[item["artifact_id"] for item in source_companions],
            ],
            "provenance_id": source_prov_id,
            "environment": {"platform": platform, "compute": compute},
            "source_context": source_context,
        },
        {
            "entity_type": "Run",
            "schema_version": "1.0",
            "run_id": processing_run_id,
            "parent_run_id": source_run_id,
            "study_id": study_id,
            "design_revision_id": revision_id,
            "activity_type": "data_processing",
            "status": "succeeded",
            "physics_domains": ["electromagnetics"],
            "started_at": processed,
            "ended_at": processed,
            "input_artifact_ids": [
                raw_artifact_id,
                *[item["artifact_id"] for item in processing_companions],
            ],
            "output_artifact_ids": [h5_artifact_id, evidence_artifact_id],
            "provenance_id": processing_prov_id,
            "environment": {"platform": platform, "compute": "local deterministic adapter"},
            "source_context": {
                "processing": {
                    "adapter": "touchstone",
                    "adapter_version": ADAPTER_VERSION,
                    "source_representation": data.source_complex_format,
                    "canonical_representation": "real_imaginary",
                    "conversion": "RI direct; MA magnitude*cos/sin(angle); DB 10^(dB/20)*cos/sin(angle)",
                    "import_parameters": {
                        "device_class": {"value": device_class, "value_origin": "manual_entry"},
                        "source_timezone": {
                            "value": source_timezone,
                            "value_origin": "manual_entry",
                        },
                    },
                }
            },
        },
        {
            "entity_type": "Observable",
            "schema_version": "1.0",
            "observable_id": observable_id,
            "run_id": processing_run_id,
            "observable_type": "s_parameters",
            "quantity": "complex_scattering_parameter",
            "axes": [
                {"name": "frequency", "unit": "Hz", "data_ref": _h5_ref(root, h5_path, "/axes/frequency")}
            ],
            "components": list(data.components),
            "complex_representation": "real_imaginary",
            "data_ref": _h5_ref(root, h5_path, "/observables/s_parameters"),
            "artifact_id": h5_artifact_id,
            "coordinate_system_ref": None,
            "value_origin": source_value_origin,
            "provenance_id": processing_prov_id,
        },
    ]

    if literature_companions:
        records.append(
            {
                "entity_type": "Run",
                "schema_version": "1.0",
                "run_id": literature_run_id,
                "study_id": study_id,
                "design_revision_id": revision_id,
                "activity_type": "literature_record",
                "status": "succeeded",
                "physics_domains": ["electromagnetics"],
                "started_at": literature_time,
                "ended_at": literature_time,
                "input_artifact_ids": [],
                "output_artifact_ids": [item["artifact_id"] for item in literature_companions],
                "provenance_id": literature_prov_id,
                "environment": {"platform": platform, "compute": "document ingestion"},
                "source_context": {
                    "literature": {
                        **literature_context,
                        "time_semantics": (
                            "published_at is a date-derived anchor; publication time of day was not recorded"
                        ),
                    }
                },
            }
        )

    if inductor_products is not None:
        records.extend(
            [
                {
                    "entity_type": "Observable",
                    "schema_version": "1.0",
                    "observable_id": impedance_observable_id,
                    "run_id": processing_run_id,
                    "observable_type": "curve",
                    "quantity": "complex_differential_impedance",
                    "axes": [
                        {
                            "name": "frequency",
                            "unit": "Hz",
                            "data_ref": _h5_ref(root, h5_path, "/axes/frequency"),
                        }
                    ],
                    "components": ["Z_diff"],
                    "complex_representation": "real_imaginary",
                    "data_ref": _h5_ref(root, h5_path, "/observables/differential_impedance"),
                    "artifact_id": h5_artifact_id,
                    "coordinate_system_ref": None,
                    "value_origin": "calculated",
                    "provenance_id": processing_prov_id,
                    "derived_from_observable_ids": [observable_id],
                    "derivation": {
                        "method": "Z=Z0*(I+S)*inv(I-S); Zdiff=Z11+Z22-Z12-Z21",
                        "script_artifact_id": script_artifact_id,
                    },
                },
                {
                    "entity_type": "Observable",
                    "schema_version": "1.0",
                    "observable_id": inductance_observable_id,
                    "run_id": processing_run_id,
                    "observable_type": "curve",
                    "quantity": "effective_series_inductance",
                    "axes": [
                        {
                            "name": "frequency",
                            "unit": "Hz",
                            "data_ref": _h5_ref(root, h5_path, "/axes/frequency"),
                        }
                    ],
                    "components": ["L_series"],
                    "complex_representation": "not_applicable",
                    "data_ref": _h5_ref(root, h5_path, "/observables/effective_inductance"),
                    "artifact_id": h5_artifact_id,
                    "coordinate_system_ref": None,
                    "value_origin": "calculated",
                    "provenance_id": processing_prov_id,
                    "derived_from_observable_ids": [impedance_observable_id],
                    "derivation": {
                        "method": "imag(Zdiff)/(2*pi*frequency)",
                        "script_artifact_id": script_artifact_id,
                    },
                },
                {
                    "entity_type": "Observable",
                    "schema_version": "1.0",
                    "observable_id": q_observable_id,
                    "run_id": processing_run_id,
                    "observable_type": "curve",
                    "quantity": "effective_quality_factor",
                    "axes": [
                        {
                            "name": "frequency",
                            "unit": "Hz",
                            "data_ref": _h5_ref(root, h5_path, "/axes/frequency"),
                        }
                    ],
                    "components": ["Q_series"],
                    "complex_representation": "not_applicable",
                    "data_ref": _h5_ref(root, h5_path, "/observables/effective_q"),
                    "artifact_id": h5_artifact_id,
                    "coordinate_system_ref": None,
                    "value_origin": "calculated",
                    "provenance_id": processing_prov_id,
                    "derived_from_observable_ids": [impedance_observable_id],
                    "derivation": {
                        "method": "abs(imag(Zdiff))/real(Zdiff), for real(Zdiff)>0",
                        "script_artifact_id": script_artifact_id,
                    },
                },
            ]
        )

    if metric_family == "rf_filter":
        metrics = _filter_metrics(data)
        metric_source_ids = {key: observable_id for key in metrics}
        metric_algorithm = "DADC deterministic sampled -3 dB extraction"
        metric_specs = [
            ("lower_3db_frequency", "lower_3db_frequency", "Hz", "first point of largest contiguous S21 >= -3 dB segment"),
            ("upper_3db_frequency", "upper_3db_frequency", "Hz", "last point of largest contiguous S21 >= -3 dB segment"),
            ("bandwidth_3db", "bandwidth_3db", "Hz", "upper_3db_frequency - lower_3db_frequency"),
            ("center_frequency_3db", "center_frequency_3db", "Hz", "midpoint of the sampled -3 dB passband"),
            ("peak_transmission_db", "peak_transmission", "dB", "max(20*log10(abs(S21)))"),
        ]
    elif metric_family == "antenna":
        metrics = _antenna_metrics(data)
        metric_source_ids = {key: observable_id for key in metrics}
        metric_algorithm = "DADC deterministic sampled S11 minimum extraction"
        metric_specs = [
            (
                "resonance_frequency",
                "resonance_frequency",
                "Hz",
                "sampled frequency at minimum 20*log10(abs(S11))",
            ),
            (
                "minimum_return_loss_db",
                "return_loss",
                "dB",
                "minimum sampled 20*log10(abs(S11))",
            ),
        ]
    elif metric_family == "inductor":
        references = metric_reference_frequencies_hz or {}
        inductance_frequency = float(references.get("inductance", 250.0e6))
        q_frequency = float(references.get("q_factor", inductance_frequency))
        metrics = {
            "effective_inductance_at_reference_frequency": _interpolate_at(
                data.frequencies_hz,
                inductor_products["effective_inductance"],
                inductance_frequency,
            ),
            "effective_q_at_reference_frequency": _interpolate_at(
                data.frequencies_hz,
                inductor_products["effective_q"],
                q_frequency,
            ),
        }
        metric_source_ids = {
            "effective_inductance_at_reference_frequency": inductance_observable_id,
            "effective_q_at_reference_frequency": q_observable_id,
        }
        metric_algorithm = "DADC differential S-to-Z conversion and linear frequency interpolation"
        metric_specs = [
            (
                "effective_inductance_at_reference_frequency",
                "effective_series_inductance",
                "H",
                f"linear interpolation of imag(Zdiff)/(2*pi*f) at {inductance_frequency:.17g} Hz",
            ),
            (
                "effective_q_at_reference_frequency",
                "effective_quality_factor",
                "1",
                f"linear interpolation of abs(imag(Zdiff))/real(Zdiff) at {q_frequency:.17g} Hz",
            ),
        ]
    else:
        raise ValueError(f"Unsupported Touchstone metric family: {metric_family}")
    for key, quantity, unit, method in metric_specs:
        metric_id = f"metric_{slug}_{key}"
        records.append(
            {
                "entity_type": "Metric",
                "schema_version": "1.0",
                "metric_id": metric_id,
                "run_id": processing_run_id,
                "name": key,
                "quantity": quantity,
                "value": metrics[key],
                "unit": unit,
                "value_origin": "calculated",
                "source_observable_ids": [metric_source_ids[key]],
                "extraction_method": method,
                "calculation": {
                    "algorithm": metric_algorithm,
                    "script_artifact_id": script_artifact_id,
                },
                "provenance_id": processing_prov_id,
            }
        )
    for record in records:
        _write_record(root, case_id, record)

    validations = [
        {
            "entity_type": "Validation",
            "schema_version": "1.0",
            "validation_id": passivity_validation_id,
            "subject_refs": [_subject("Observable", observable_id)],
            "validation_type": "physical_rule_check",
            "method": {
                "name": "scattering-matrix passivity screen",
                "description": "Maximum singular value of S over all sampled frequencies.",
                "script_artifact_id": script_artifact_id,
            },
            "threshold": {
                "name": "maximum_singular_value",
                "operator": "<=",
                "value": passivity_tolerance,
                "unit": "1",
            },
            "result": {
                "status": "passed" if max_singular_value <= passivity_tolerance else "failed",
                "summary": "Passivity screen completed on the normalized complex S matrix.",
                "measured_values": [{"maximum_singular_value": max_singular_value}],
            },
            "evidence_artifact_ids": [evidence_artifact_id],
            "executed_at": processed,
            "provenance_id": processing_prov_id,
        }
    ]
    if data.port_count > 1:
        validations.append(
            {
                "entity_type": "Validation",
                "schema_version": "1.0",
                "validation_id": reciprocity_validation_id,
                "subject_refs": [_subject("Observable", observable_id)],
                "validation_type": "physical_rule_check",
                "method": {
                    "name": "reciprocity screen",
                    "description": "Maximum absolute elementwise difference between S and transpose(S).",
                    "script_artifact_id": script_artifact_id,
                },
                "threshold": {
                    "name": "maximum_reciprocity_error",
                    "operator": "<=",
                    "value": reciprocity_tolerance,
                    "unit": "1",
                },
                "result": {
                    "status": "passed" if reciprocity_error <= reciprocity_tolerance else "failed",
                    "summary": "Reciprocity screen completed on the normalized complex S matrix.",
                    "measured_values": [{"maximum_reciprocity_error": reciprocity_error}],
                },
                "evidence_artifact_ids": [evidence_artifact_id],
                "executed_at": processed,
                "provenance_id": processing_prov_id,
            },
        )
    _write_json(case / "validation.json", {"schema_version": "1.0", "validations": validations})
    rebuild_indexes(root)

    validation = DADCRepository(root).validate()
    if not validation.valid:
        raise RuntimeError(json.dumps(validation.to_dict(), ensure_ascii=False, indent=2))
    return TouchstoneIngestionResult(
        repository=root,
        case_id=case_id,
        source_sha256=source_hash,
        source_format=data.source_complex_format,
        frequency_points=int(len(data.frequencies_hz)),
        port_count=data.port_count,
        metrics=metrics,
        physical_checks={
            "frequency_strictly_increasing": checks["frequency_strictly_increasing"],
            "all_values_finite": checks["all_values_finite"],
            "maximum_singular_value": max_singular_value,
            "passivity_screen_passed": max_singular_value <= passivity_tolerance,
            "maximum_reciprocity_error": reciprocity_error,
            "reciprocity_screen_passed": reciprocity_error <= reciprocity_tolerance,
        },
        validation=validation.to_dict(),
    )


def ingest_touchstone_filter_repository(
    source: str | Path,
    target: str | Path,
    *,
    case_id: str,
    device_name: str,
    filter_order: int,
    source_timezone: str,
    companion_artifacts: list[dict[str, Any]] | None = None,
    operator_id: str = "local_user",
    platform: str = "windows",
    compute: str = "not_recorded",
    solver_edition: str = "Student",
    processed_at: str | None = None,
) -> TouchstoneIngestionResult:
    """Create a validated DADC RF-filter repository from a Touchstone file."""

    if filter_order < 1:
        raise ValueError("filter_order must be a positive integer supplied by the user")
    return _ingest_touchstone_repository(
        source,
        target,
        case_id=case_id,
        device_name=device_name,
        device_class="rf_filter",
        device_subtype="interdigital_bandpass_filter",
        profile_extension={"filter_response": "bandpass", "order": filter_order, "port_count": 2},
        topology_family="interdigital_resonator_filter",
        metric_family="rf_filter",
        source_timezone=source_timezone,
        companion_artifacts=companion_artifacts,
        operator_id=operator_id,
        platform=platform,
        compute=compute,
        solver_edition=solver_edition,
        processed_at=processed_at,
    )


def ingest_touchstone_antenna_repository(
    source: str | Path,
    target: str | Path,
    *,
    case_id: str,
    device_name: str,
    source_timezone: str,
    feed_type: str,
    radiation_mode: str,
    companion_artifacts: list[dict[str, Any]] | None = None,
    device_subtype: str = "probe_fed_patch_antenna",
    operator_id: str = "local_user",
    platform: str = "windows",
    compute: str = "not_recorded",
    solver_edition: str = "Student",
    processed_at: str | None = None,
) -> TouchstoneIngestionResult:
    """Create a validated single-feed antenna repository from a Touchstone file."""

    if not feed_type or not radiation_mode:
        raise ValueError("feed_type and radiation_mode must be explicit")
    return _ingest_touchstone_repository(
        source,
        target,
        case_id=case_id,
        device_name=device_name,
        device_class="antenna",
        device_subtype=device_subtype,
        profile_extension={
            "feed_type": feed_type,
            "radiation_mode": radiation_mode,
            "port_count": 1,
        },
        topology_family="microstrip_patch",
        metric_family="antenna",
        source_timezone=source_timezone,
        companion_artifacts=companion_artifacts,
        operator_id=operator_id,
        platform=platform,
        compute=compute,
        solver_edition=solver_edition,
        processed_at=processed_at,
    )


def ingest_touchstone_inductor_repository(
    source: str | Path,
    target: str | Path,
    *,
    case_id: str,
    device_name: str,
    manufacturer: str,
    part_number: str,
    construction: str,
    package_size: str,
    datasheet_specifications: list[dict[str, Any]],
    measurement_context: dict[str, Any],
    literature_context: dict[str, Any],
    source_timezone: str,
    source_timestamp: str,
    metric_reference_frequencies_hz: dict[str, float],
    companion_artifacts: list[dict[str, Any]],
    device_subtype: str = "wire_wound_ceramic_rf_inductor",
    operator_id: str = "local_user",
    platform: str = "vendor_measurement",
    compute: str = "not_applicable",
    processed_at: str | None = None,
) -> TouchstoneIngestionResult:
    """Create a vendor-measurement inductor Case plus a separate datasheet Run."""

    if not all((manufacturer, part_number, construction, package_size)):
        raise ValueError("Inductor manufacturer, part_number, construction, and package_size are required")
    if not datasheet_specifications:
        raise ValueError("Inductor intake requires at least one datasheet specification")
    if not isinstance(measurement_context, dict) or not measurement_context:
        raise ValueError("measurement_context must be a non-empty object")
    if not isinstance(literature_context, dict):
        raise ValueError("literature_context must be an object")
    required_literature = {"citation", "uri", "accessed_at", "published_at"}
    missing_literature = sorted(required_literature.difference(literature_context))
    if missing_literature:
        raise ValueError(
            "literature_context is missing: " + ", ".join(missing_literature)
        )
    _require_aware_datetime(str(literature_context["accessed_at"]), "literature_context.accessed_at")
    experiment_context = {
        "experiment": {
            **measurement_context,
            "timestamp_basis": "explicit intake timestamp",
            "time_semantics": (
                "The source document records a date but not start/end time; "
                "the date is anchored as declared by the intake manifest"
            ),
            "source_text_encoding": "recorded by parser in normalized HDF5 and validation evidence",
        }
    }
    return _ingest_touchstone_repository(
        source,
        target,
        case_id=case_id,
        device_name=device_name,
        device_class="inductor",
        device_subtype=device_subtype,
        profile_extension={
            "manufacturer": manufacturer,
            "part_number": part_number,
            "construction": construction,
            "package_size": package_size,
            "datasheet_specifications": datasheet_specifications,
            "port_count": 2,
        },
        topology_family="two_terminal_lumped_inductor",
        metric_family="inductor",
        source_timezone=source_timezone,
        companion_artifacts=companion_artifacts,
        operator_id=operator_id,
        platform=platform,
        compute=compute,
        processed_at=processed_at,
        source_activity_type="experiment_run",
        source_timestamp=source_timestamp,
        source_run_suffix="vendor_vna_measurement",
        source_provenance_suffix="vendor_vna_measurement",
        source_artifact_role="measurement_data",
        source_value_origin="raw_experiment_output",
        source_provenance_type="experiment",
        source_context=experiment_context,
        source_software=[
            {
                "name": str(measurement_context.get("instrument", "not_recorded")),
                "version": str(measurement_context.get("instrument_version", "not_recorded")),
                "role": "vector network analyzer measurement",
            }
        ],
        source_operator_role="vendor measurement provider",
        source_citation=(
            f"Vendor-provided S-parameter measurement/model for {manufacturer} {part_number}; "
            "preserved byte-for-byte."
        ),
        device_tags=["real_vendor_data", "vna_measurement", "touchstone", "datasheet"],
        revision_label=f"vendor-{part_number}-measurement-model",
        study_objective="frequency_dependent_impedance_and_quality_factor",
        literature_context=literature_context,
        metric_reference_frequencies_hz=metric_reference_frequencies_hz,
    )
