"""Ingest an immutable Joule-heating/thermal field bundle into DADC V1.0."""

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
_ALLOWED_ROLES = {
    "native_project",
    "raw_input",
    "mesh",
    "solver_log",
    "script",
    "result_hdf5",
    "index_parquet",
    "validation_evidence",
    "report",
    "measurement_data",
    "literature_source",
}
_ALLOWED_ORIGINS = {
    "raw_solver_output",
    "raw_experiment_output",
    "literature_extracted",
    "calculated",
    "manual_entry",
}


@dataclass(frozen=True)
class FieldBundleIngestionResult:
    repository: Path
    case_id: str
    source_sha256: str
    node_count: int
    cell_count: int
    metrics: dict[str, float]
    validation: dict[str, Any]


def _safe_companion(root: Path, relative: str) -> Path:
    candidate_relative = Path(relative)
    if candidate_relative.is_absolute() or ".." in candidate_relative.parts:
        raise ValueError(f"Bundle companion path must be relative and cannot escape: {relative!r}")
    candidate = (root / candidate_relative).resolve()
    if root.resolve() not in candidate.parents:
        raise ValueError(f"Bundle companion path escapes source folder: {relative!r}")
    if not candidate.is_file():
        raise FileNotFoundError(f"Bundle companion is missing: {candidate}")
    return candidate


def _load_bundle(source: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bundle = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(bundle, dict):
        raise ValueError("Field bundle root must be a JSON object")
    if bundle.get("bundle_schema_version") != "1.0":
        raise ValueError("bundle_schema_version must be '1.0'")
    if bundle.get("bundle_type") != "joule_thermal_field_bundle":
        raise ValueError("bundle_type must be 'joule_thermal_field_bundle'")
    required = {"generated_at", "operator_id", "case", "coordinate_system", "mesh", "parameters", "outputs", "references", "files"}
    missing = sorted(required.difference(bundle))
    if missing:
        raise ValueError(f"Field bundle is missing keys: {', '.join(missing)}")
    _require_aware_datetime(str(bundle["generated_at"]), "bundle.generated_at")
    files = bundle["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("bundle.files must be a non-empty array")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Each bundle.files item must be an object")
        item_required = {"file_id", "path", "sha256", "size_bytes", "stage", "artifact_role", "media_type", "value_origin"}
        item_missing = sorted(item_required.difference(item))
        if item_missing:
            raise ValueError(f"Bundle file is missing keys: {', '.join(item_missing)}")
        file_id = str(item["file_id"])
        relative = str(item["path"])
        if file_id in seen_ids or relative in seen_paths:
            raise ValueError(f"Duplicate bundle file id or path: {file_id!r}, {relative!r}")
        seen_ids.add(file_id)
        seen_paths.add(relative)
        if item["artifact_role"] not in _ALLOWED_ROLES:
            raise ValueError(f"Unsupported artifact_role in bundle: {item['artifact_role']!r}")
        if item["value_origin"] not in _ALLOWED_ORIGINS:
            raise ValueError(f"Unsupported value_origin in bundle: {item['value_origin']!r}")
        path = _safe_companion(source.parent, relative)
        actual_hash = sha256_file(path)
        if actual_hash != item["sha256"]:
            raise ValueError(f"Companion SHA-256 mismatch: {relative}")
        if path.stat().st_size != int(item["size_bytes"]):
            raise ValueError(f"Companion size mismatch: {relative}")
        normalized.append({**item, "source_path": path})
    mandatory = {
        "mesh_nodes",
        "mesh_cells",
        "electrical_fields",
        "thermal_fields",
        "coupling_map",
        "electrical_solver_log",
        "thermal_solver_log",
        "reference_solver_checks",
        "generation_recipe",
    }
    if missing_ids := mandatory.difference(seen_ids):
        raise ValueError(f"Field bundle is missing mandatory files: {', '.join(sorted(missing_ids))}")
    return bundle, normalized


def _read_csv(path: Path, required_columns: list[str]) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != required_columns:
            raise ValueError(
                f"Unexpected columns in {path.name}: {reader.fieldnames!r}; expected {required_columns!r}"
            )
        rows = list(reader)
    if not rows:
        raise ValueError(f"CSV has no data rows: {path.name}")
    return rows


def _arrays(bundle: dict[str, Any], files: dict[str, Path]) -> dict[str, np.ndarray]:
    ny, nx = [int(value) for value in bundle["mesh"]["shape"]]
    node_count = nx * ny
    node_rows = _read_csv(files["mesh_nodes"], ["node_id", "i", "j", "x_m", "y_m", "z_m"])
    cell_rows = _read_csv(files["mesh_cells"], ["cell_id", "n0", "n1", "n2", "n3"])
    electrical_rows = _read_csv(
        files["electrical_fields"],
        ["node_id", "potential_v", "electric_field_x_v_m", "electric_field_y_v_m", "joule_loss_w_m3"],
    )
    thermal_rows = _read_csv(
        files["thermal_fields"],
        ["node_id", "temperature_k", "heat_flux_x_w_m2", "heat_flux_y_w_m2"],
    )
    if len(node_rows) != node_count or len(electrical_rows) != node_count or len(thermal_rows) != node_count:
        raise ValueError("Mesh and field row counts do not match bundle.mesh.shape")
    expected_cells = (nx - 1) * (ny - 1)
    if len(cell_rows) != expected_cells or int(bundle["mesh"]["cell_count"]) != expected_cells:
        raise ValueError("Cell row count does not match structured mesh shape")
    ids = np.arange(node_count, dtype=np.int64)
    for label, rows in (("mesh", node_rows), ("electrical", electrical_rows), ("thermal", thermal_rows)):
        row_ids = np.array([int(row["node_id"]) for row in rows], dtype=np.int64)
        if not np.array_equal(row_ids, ids):
            raise ValueError(f"{label} node_id values must be contiguous and in canonical order")

    x = np.array([float(node_rows[i]["x_m"]) for i in range(nx)], dtype=np.float64)
    y = np.array([float(node_rows[j * nx]["y_m"]) for j in range(ny)], dtype=np.float64)
    coordinates = np.array(
        [[float(row["x_m"]), float(row["y_m"]), float(row["z_m"])] for row in node_rows],
        dtype=np.float64,
    )
    connectivity = np.array(
        [[int(row[key]) for key in ("n0", "n1", "n2", "n3")] for row in cell_rows],
        dtype=np.int64,
    )
    result = {
        "x": x,
        "y": y,
        "coordinates": coordinates,
        "connectivity": connectivity,
        "potential": np.array([float(row["potential_v"]) for row in electrical_rows]).reshape(ny, nx),
        "electric_field": np.array(
            [[float(row["electric_field_x_v_m"]), float(row["electric_field_y_v_m"])] for row in electrical_rows]
        ).reshape(ny, nx, 2),
        "joule_loss": np.array([float(row["joule_loss_w_m3"]) for row in electrical_rows]).reshape(ny, nx),
        "temperature": np.array([float(row["temperature_k"]) for row in thermal_rows]).reshape(ny, nx),
        "heat_flux": np.array(
            [[float(row["heat_flux_x_w_m2"]), float(row["heat_flux_y_w_m2"])] for row in thermal_rows]
        ).reshape(ny, nx, 2),
    }
    if not all(np.all(np.isfinite(value)) for value in result.values()):
        raise ValueError("Field bundle contains NaN or infinity")
    if not np.all(np.diff(x) > 0.0) or not np.all(np.diff(y) > 0.0):
        raise ValueError("Structured mesh axes must be strictly increasing")
    return result


def _write_field_h5(
    path: Path,
    arrays: dict[str, np.ndarray],
    fields: list[tuple[str, str, list[str], str]],
    source_hashes: dict[str, str],
) -> None:
    with h5py.File(path, "x") as handle:
        axes = handle.create_group("axes")
        x = axes.create_dataset("x", data=arrays["x"])
        y = axes.create_dataset("y", data=arrays["y"])
        x.attrs["unit"] = "m"
        y.attrs["unit"] = "m"
        mesh = handle.create_group("mesh")
        coordinates = mesh.create_dataset("coordinates", data=arrays["coordinates"], compression="gzip")
        connectivity = mesh.create_dataset("connectivity", data=arrays["connectivity"], compression="gzip")
        coordinates.attrs["unit"] = "m"
        connectivity.attrs["cell_type"] = "quadrilateral"
        for key, dataset_name, components, unit in fields:
            dataset = handle.create_dataset(f"fields/{dataset_name}", data=arrays[key], compression="gzip")
            dataset.attrs["unit"] = unit
            dataset.attrs["components"] = json.dumps(components)
            dataset.attrs["mesh_coordinates_ref"] = "/mesh/coordinates"
            dataset.attrs["mesh_connectivity_ref"] = "/mesh/connectivity"
        handle.attrs["source_sha256_json"] = json.dumps(source_hashes, sort_keys=True)
        handle.attrs["canonical_coordinate_unit"] = "m"


def ingest_joule_thermal_field_bundle(
    source: str | Path,
    target: str | Path,
    *,
    case_id: str,
    device_name: str,
    operator_id: str = "local_user",
    platform: str = "not_recorded",
    compute: str = "not_recorded",
    processed_at: str | None = None,
) -> FieldBundleIngestionResult:
    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if not _CASE_ID.fullmatch(case_id):
        raise ValueError("case_id must match ^[a-z][a-z0-9_]{2,63}$")
    bundle, file_items = _load_bundle(source_path)
    if bundle["case"].get("device_class") != "power_resistor":
        raise ValueError("Joule-thermal bundle requires case.device_class='power_resistor'")
    if bundle["case"].get("physics_domains") != ["electromagnetics", "thermal"]:
        raise ValueError("Joule-thermal bundle requires electromagnetics and thermal domains")
    generated_at = str(bundle["generated_at"])
    processed = _require_aware_datetime(processed_at or _utc_now(), "processed_at")
    root = Path(target).resolve()
    if root.exists():
        if root.is_file() or any(root.iterdir()):
            raise FileExistsError(f"Refusing to overwrite non-empty target: {root}")
    else:
        root.mkdir(parents=True)
    shutil.copytree(_source_schemas(), root / "schemas")

    case = root / "cases" / case_id
    raw_dir = case / "raw"
    data_dir = case / "data"
    scripts_dir = case / "scripts"
    raw_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    scripts_dir.mkdir(parents=True)
    bundle_copy = raw_dir / source_path.name
    shutil.copyfile(source_path, bundle_copy)
    source_hash = sha256_file(source_path)
    if sha256_file(bundle_copy) != source_hash:
        raise IOError("Bundle copy failed byte-integrity verification")

    copied: dict[str, dict[str, Any]] = {}
    for item in file_items:
        target_path = raw_dir / str(item["path"])
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item["source_path"], target_path)
        if sha256_file(target_path) != item["sha256"]:
            raise IOError(f"Companion copy failed byte-integrity verification: {item['path']}")
        copied[str(item["file_id"])] = {**item, "target_path": target_path}
    adapter_script_path = scripts_dir / "field_bundle_adapter.py"
    shutil.copyfile(Path(__file__).resolve(), adapter_script_path)

    source_files = {file_id: item["target_path"] for file_id, item in copied.items()}
    arrays = _arrays(bundle, source_files)
    electrical_h5 = data_dir / "electrical_fields.h5"
    thermal_h5 = data_dir / "thermal_fields.h5"
    source_hashes = {file_id: str(item["sha256"]) for file_id, item in copied.items()}
    _write_field_h5(
        electrical_h5,
        arrays,
        [
            ("potential", "electric_potential", ["V"], "V"),
            ("electric_field", "electric_field", ["E_x", "E_y"], "V/m"),
            ("joule_loss", "joule_loss_density", ["q_joule"], "W/m^3"),
        ],
        source_hashes,
    )
    _write_field_h5(
        thermal_h5,
        arrays,
        [
            ("temperature", "temperature", ["T"], "K"),
            ("heat_flux", "heat_flux", ["q_x", "q_y"], "W/m^2"),
        ],
        source_hashes,
    )

    slug = case_id
    device_id = f"device_{slug}"
    revision_id = f"rev_{slug}_001"
    study_id = f"study_{slug}_one_way_joule_heating"
    electrical_run_id = f"run_{slug}_electrical_fd"
    electrical_import_run_id = f"run_{slug}_electrical_import"
    thermal_run_id = f"run_{slug}_thermal_fd"
    thermal_import_run_id = f"run_{slug}_thermal_import"
    electrical_prov_id = f"prov_{slug}_electrical_fd"
    thermal_prov_id = f"prov_{slug}_thermal_fd"
    processing_prov_id = f"prov_{slug}_field_import"
    bundle_artifact_id = f"art_{slug}_bundle"
    adapter_artifact_id = f"art_{slug}_adapter_script"
    electrical_h5_artifact_id = f"art_{slug}_electrical_h5"
    thermal_h5_artifact_id = f"art_{slug}_thermal_h5"
    source_artifact_ids = {
        file_id: f"art_{slug}_{file_id}" for file_id in copied
    }
    potential_obs_id = f"obs_{slug}_electric_potential"
    electric_field_obs_id = f"obs_{slug}_electric_field"
    loss_obs_id = f"obs_{slug}_joule_loss_density"
    temperature_obs_id = f"obs_{slug}_temperature"
    heat_flux_obs_id = f"obs_{slug}_heat_flux"

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

    electrical_subject_artifacts = [
        bundle_artifact_id,
        source_artifact_ids["mesh_nodes"],
        source_artifact_ids["mesh_cells"],
        source_artifact_ids["electrical_fields"],
        source_artifact_ids["electrical_solver_log"],
        source_artifact_ids["generation_recipe"],
    ]
    thermal_subject_artifacts = [
        source_artifact_ids["thermal_fields"],
        source_artifact_ids["thermal_solver_log"],
        source_artifact_ids["coupling_map"],
    ]
    processing_subject_artifacts = [
        electrical_h5_artifact_id,
        thermal_h5_artifact_id,
        adapter_artifact_id,
        source_artifact_ids["reference_solver_checks"],
    ]
    references = [dict(item) for item in bundle["references"]]
    provenance_records = [
        {
            "entity_type": "Provenance",
            "schema_version": "1.0",
            "provenance_id": electrical_prov_id,
            "subject_refs": [
                _subject("Device", device_id),
                _subject("DesignRevision", revision_id),
                _subject("Run", electrical_run_id),
                *[_subject("Artifact", artifact_id) for artifact_id in electrical_subject_artifacts],
            ],
            "source_type": "simulation",
            "sources": references,
            "software": [{"name": "DADC reference finite-difference electrical solver", "version": "1.0", "role": "electrical potential and Joule-loss solve"}],
            "scripts": [source_artifact_ids["generation_recipe"]],
            "people": [{"person_id": operator_id, "role": "simulation operator"}],
            "generated_at": generated_at,
        },
        {
            "entity_type": "Provenance",
            "schema_version": "1.0",
            "provenance_id": thermal_prov_id,
            "subject_refs": [
                _subject("Run", thermal_run_id),
                *[_subject("Artifact", artifact_id) for artifact_id in thermal_subject_artifacts],
            ],
            "source_type": "simulation",
            "sources": references,
            "software": [{"name": "DADC reference finite-difference thermal solver", "version": "1.0", "role": "steady thermal solve"}],
            "scripts": [source_artifact_ids["generation_recipe"]],
            "people": [{"person_id": operator_id, "role": "simulation operator"}],
            "generated_at": generated_at,
        },
        {
            "entity_type": "Provenance",
            "schema_version": "1.0",
            "provenance_id": processing_prov_id,
            "subject_refs": [
                _subject("Run", electrical_import_run_id),
                _subject("Run", thermal_import_run_id),
                *[_subject("Observable", observable_id) for observable_id in (potential_obs_id, electric_field_obs_id, loss_obs_id, temperature_obs_id, heat_flux_obs_id)],
                *[_subject("Artifact", artifact_id) for artifact_id in processing_subject_artifacts],
            ],
            "source_type": "data_processing",
            "sources": [{"source_id": source_hash, "source_type": "file", "title": source_path.name, "citation": "Immutable source-bundle manifest containing SHA-256 for every companion file."}],
            "software": [{"name": "DADC Joule-thermal field adapter", "version": ADAPTER_VERSION, "role": "CSV and mesh normalization to HDF5"}],
            "scripts": [adapter_artifact_id],
            "people": [{"person_id": operator_id, "role": "data importer"}],
            "generated_at": processed,
        },
    ]
    for record in provenance_records:
        _write_record(root, case_id, record)

    _artifact(
        root,
        case_id,
        bundle_copy,
        bundle_artifact_id,
        [_subject("Run", electrical_run_id), _subject("DesignRevision", revision_id)],
        "raw_input",
        "application/json",
        "manual_entry",
        electrical_prov_id,
        generated_at,
    )
    for file_id, item in copied.items():
        stage = str(item["stage"])
        if stage in {"electrical", "common"}:
            provenance_id = electrical_prov_id
            run_id = electrical_run_id
        elif stage in {"thermal", "coupling"}:
            provenance_id = thermal_prov_id
            run_id = thermal_run_id
        else:
            provenance_id = processing_prov_id
            run_id = thermal_import_run_id
        _artifact(
            root,
            case_id,
            item["target_path"],
            source_artifact_ids[file_id],
            [_subject("Run", run_id), _subject("DesignRevision", revision_id)],
            str(item["artifact_role"]),
            str(item["media_type"]),
            str(item["value_origin"]),
            provenance_id,
            generated_at,
        )
    _artifact(
        root,
        case_id,
        adapter_script_path,
        adapter_artifact_id,
        [_subject("Run", electrical_import_run_id), _subject("Run", thermal_import_run_id)],
        "script",
        "text/x-python",
        "manual_entry",
        processing_prov_id,
        processed,
    )
    _artifact(
        root,
        case_id,
        electrical_h5,
        electrical_h5_artifact_id,
        [_subject("Run", electrical_import_run_id), *[_subject("Observable", item) for item in (potential_obs_id, electric_field_obs_id, loss_obs_id)]],
        "result_hdf5",
        "application/x-hdf5",
        "calculated",
        processing_prov_id,
        processed,
    )
    _artifact(
        root,
        case_id,
        thermal_h5,
        thermal_h5_artifact_id,
        [_subject("Run", thermal_import_run_id), *[_subject("Observable", item) for item in (temperature_obs_id, heat_flux_obs_id)]],
        "result_hdf5",
        "application/x-hdf5",
        "calculated",
        processing_prov_id,
        processed,
    )

    parameters = bundle["parameters"]
    outputs = {key: float(value) for key, value in bundle["outputs"].items()}
    device = {
        "entity_type": "Device",
        "schema_version": "1.0",
        "device_id": device_id,
        "name": device_name,
        "device_class": "power_resistor",
        "device_subtype": "thin_film_power_resistor",
        "physics_domains": ["electromagnetics", "thermal"],
        "profile_schema": "device_profiles/power_resistor.schema.json",
        "extensions": {
            "power_resistor": {
                "resistor_type": "thin_film",
                "terminal_count": 2,
                "primary_function": "electrical_power_dissipation",
                "coupling_strategy": "one_way",
                "field_bundle_schema_version": "1.0",
            }
        },
        "tags": ["multiphysics", "joule_heating", "reference_solver", "real_numerical_solution"],
        "created_at": generated_at,
    }
    revision = {
        "entity_type": "DesignRevision",
        "schema_version": "1.0",
        "design_revision_id": revision_id,
        "device_id": device_id,
        "revision_label": "reference-fd-001",
        "geometry": {
            "representation": "mesh_only",
            "coordinate_system_ref": str(bundle["coordinate_system"]["coordinate_system_ref"]),
            "parameters": [
                {"name": key, "value": float(value), "unit": {"length_m": "m", "width_m": "m", "thickness_m": "m", "applied_voltage_v": "V", "ambient_temperature_k": "K"}.get(key, "1"), "value_origin": "manual_entry"}
                for key, value in parameters.items()
                if key in {"length_m", "width_m", "thickness_m", "applied_voltage_v", "ambient_temperature_k"}
            ],
        },
        "materials": [
            {
                "material_id": "resistive_film",
                "role": "electrically_resistive_and_thermally_conductive_domain",
                "properties": {
                    "electrical_conductivity": {"value": float(parameters["electrical_conductivity_s_m"]), "unit": "S/m", "value_origin": "manual_entry"},
                    "thermal_conductivity": {"value": float(parameters["thermal_conductivity_w_mk"]), "unit": "W/(m*K)", "value_origin": "manual_entry"},
                },
                "source_provenance_id": electrical_prov_id,
            }
        ],
        "topology": {
            "family": "rectangular_thin_film_resistor",
            "mesh_type": "structured_quadrilateral",
            "mesh_shape": list(bundle["mesh"]["shape"]),
            "coupled_physics_order": ["electromagnetics", "thermal"],
        },
        "artifact_ids": [bundle_artifact_id, source_artifact_ids["mesh_nodes"], source_artifact_ids["mesh_cells"], source_artifact_ids["generation_recipe"]],
        "created_at": generated_at,
    }
    study = {
        "entity_type": "Study",
        "schema_version": "1.0",
        "study_id": study_id,
        "device_id": device_id,
        "design_revision_ids": [revision_id],
        "study_type": "validation",
        "physics_domains": ["electromagnetics", "thermal"],
        "objectives": [{"metric": "maximum_temperature_and_thermal_resistance", "operator": "characterize"}],
        "parameter_space": [{"name": "applied_voltage", "value": float(parameters["applied_voltage_v"]), "unit": "V"}],
        "coupling_edges": [
            {
                "source_run_id": electrical_import_run_id,
                "target_run_id": thermal_run_id,
                "coupling_type": "one_way",
                "transferred_observable_ids": [loss_obs_id],
                "mapping_artifact_id": source_artifact_ids["coupling_map"],
            }
        ],
        "run_ids": [electrical_run_id, electrical_import_run_id, thermal_run_id, thermal_import_run_id],
        "created_at": generated_at,
    }
    runs = [
        {
            "entity_type": "Run",
            "schema_version": "1.0",
            "run_id": electrical_run_id,
            "study_id": study_id,
            "design_revision_id": revision_id,
            "activity_type": "simulation_run",
            "status": "succeeded",
            "physics_domains": ["electromagnetics"],
            "started_at": generated_at,
            "ended_at": generated_at,
            "input_artifact_ids": [bundle_artifact_id, source_artifact_ids["generation_recipe"]],
            "output_artifact_ids": [source_artifact_ids[key] for key in ("mesh_nodes", "mesh_cells", "electrical_fields", "electrical_solver_log")],
            "provenance_id": electrical_prov_id,
            "environment": {"platform": platform, "compute": compute},
            "source_context": {"solver": {"name": "DADC reference finite-difference electrical solver", "version": "1.0", "method": "Jacobi iteration", "equation": "div(sigma*grad(V))=0"}},
        },
        {
            "entity_type": "Run",
            "schema_version": "1.0",
            "run_id": electrical_import_run_id,
            "parent_run_id": electrical_run_id,
            "study_id": study_id,
            "design_revision_id": revision_id,
            "activity_type": "data_processing",
            "status": "succeeded",
            "physics_domains": ["electromagnetics"],
            "started_at": processed,
            "ended_at": processed,
            "input_artifact_ids": [source_artifact_ids[key] for key in ("mesh_nodes", "mesh_cells", "electrical_fields")],
            "output_artifact_ids": [electrical_h5_artifact_id],
            "provenance_id": processing_prov_id,
            "environment": {"platform": platform, "compute": "local deterministic adapter"},
            "source_context": {"processing": {"adapter": "joule_thermal_field_bundle", "adapter_version": ADAPTER_VERSION, "normalization": "CSV SI values to HDF5 without numeric unit conversion"}},
        },
        {
            "entity_type": "Run",
            "schema_version": "1.0",
            "run_id": thermal_run_id,
            "study_id": study_id,
            "design_revision_id": revision_id,
            "activity_type": "simulation_run",
            "status": "succeeded",
            "physics_domains": ["thermal"],
            "started_at": generated_at,
            "ended_at": generated_at,
            "input_artifact_ids": [source_artifact_ids[key] for key in ("mesh_nodes", "mesh_cells", "electrical_fields", "coupling_map", "generation_recipe")],
            "output_artifact_ids": [source_artifact_ids[key] for key in ("thermal_fields", "thermal_solver_log")],
            "provenance_id": thermal_prov_id,
            "environment": {"platform": platform, "compute": compute},
            "source_context": {"solver": {"name": "DADC reference finite-difference thermal solver", "version": "1.0", "method": "Jacobi iteration", "equation": "-div(k*grad(T))=joule_loss_density"}},
        },
        {
            "entity_type": "Run",
            "schema_version": "1.0",
            "run_id": thermal_import_run_id,
            "parent_run_id": thermal_run_id,
            "study_id": study_id,
            "design_revision_id": revision_id,
            "activity_type": "data_processing",
            "status": "succeeded",
            "physics_domains": ["thermal"],
            "started_at": processed,
            "ended_at": processed,
            "input_artifact_ids": [source_artifact_ids[key] for key in ("mesh_nodes", "mesh_cells", "thermal_fields", "reference_solver_checks")],
            "output_artifact_ids": [thermal_h5_artifact_id],
            "provenance_id": processing_prov_id,
            "environment": {"platform": platform, "compute": "local deterministic adapter"},
            "source_context": {"processing": {"adapter": "joule_thermal_field_bundle", "adapter_version": ADAPTER_VERSION, "normalization": "CSV SI values to HDF5 without numeric unit conversion"}},
        },
    ]

    coordinate_ref = str(bundle["coordinate_system"]["coordinate_system_ref"])
    def field_observable(observable_id: str, run_id: str, quantity: str, components: list[str], h5_path: Path, artifact_id: str, object_path: str) -> dict[str, Any]:
        data_ref = _h5_ref(root, h5_path, object_path)
        return {
            "entity_type": "Observable",
            "schema_version": "1.0",
            "observable_id": observable_id,
            "run_id": run_id,
            "observable_type": "field",
            "quantity": quantity,
            "axes": [
                {"name": "y", "unit": "m", "data_ref": _h5_ref(root, h5_path, "/axes/y")},
                {"name": "x", "unit": "m", "data_ref": _h5_ref(root, h5_path, "/axes/x")},
            ],
            "components": components,
            "complex_representation": "not_applicable",
            "data_ref": data_ref,
            "artifact_id": artifact_id,
            "coordinate_system_ref": coordinate_ref,
            "value_origin": "raw_solver_output",
            "provenance_id": processing_prov_id,
            "field_metadata": {
                "coordinate_system_ref": coordinate_ref,
                "coordinate_unit": "m",
                "mesh_type": "structured",
                "components": components,
                "condition": {"kind": "steady_state", "value": "converged", "unit": "1"},
                "data_ref": data_ref,
                "normalization": "not_normalized; absolute SI solver values",
            },
        }

    observables = [
        field_observable(potential_obs_id, electrical_import_run_id, "electric_potential", ["V"], electrical_h5, electrical_h5_artifact_id, "/fields/electric_potential"),
        field_observable(electric_field_obs_id, electrical_import_run_id, "electric_field_strength", ["E_x", "E_y"], electrical_h5, electrical_h5_artifact_id, "/fields/electric_field"),
        field_observable(loss_obs_id, electrical_import_run_id, "joule_loss_density", ["q_joule"], electrical_h5, electrical_h5_artifact_id, "/fields/joule_loss_density"),
        field_observable(temperature_obs_id, thermal_import_run_id, "temperature", ["T"], thermal_h5, thermal_h5_artifact_id, "/fields/temperature"),
        field_observable(heat_flux_obs_id, thermal_import_run_id, "heat_flux", ["q_x", "q_y"], thermal_h5, thermal_h5_artifact_id, "/fields/heat_flux"),
    ]
    metrics = [
        {
            "entity_type": "Metric", "schema_version": "1.0", "metric_id": f"metric_{slug}_total_electrical_power", "run_id": electrical_import_run_id,
            "name": "total_electrical_power", "quantity": "electrical_power", "value": outputs["total_electrical_power_w"], "unit": "W", "value_origin": "calculated",
            "source_observable_ids": [loss_obs_id], "extraction_method": "trapezoidal area integration of Joule-loss density times film thickness",
            "calculation": {"algorithm": "integral(q_joule dV)", "script_artifact_id": source_artifact_ids["generation_recipe"]}, "provenance_id": processing_prov_id,
        },
        {
            "entity_type": "Metric", "schema_version": "1.0", "metric_id": f"metric_{slug}_maximum_temperature", "run_id": thermal_import_run_id,
            "name": "maximum_temperature", "quantity": "temperature", "value": outputs["maximum_temperature_k"], "unit": "K", "value_origin": "calculated",
            "source_observable_ids": [temperature_obs_id], "extraction_method": "maximum over all mesh nodes",
            "calculation": {"algorithm": "max(T)", "script_artifact_id": source_artifact_ids["generation_recipe"]}, "provenance_id": processing_prov_id,
        },
        {
            "entity_type": "Metric", "schema_version": "1.0", "metric_id": f"metric_{slug}_thermal_resistance", "run_id": thermal_import_run_id,
            "name": "thermal_resistance", "quantity": "thermal_resistance", "value": outputs["thermal_resistance_k_w"], "unit": "K/W", "value_origin": "calculated",
            "source_observable_ids": [temperature_obs_id, loss_obs_id], "extraction_method": "(max(T)-ambient_temperature)/integral(q_joule dV)",
            "calculation": {"algorithm": "temperature rise divided by electrical power", "script_artifact_id": source_artifact_ids["generation_recipe"]}, "provenance_id": processing_prov_id,
        },
    ]
    for record in [device, revision, study, *runs, *observables, *metrics]:
        _write_record(root, case_id, record)

    electrical_log = json.loads(copied["electrical_solver_log"]["target_path"].read_text(encoding="utf-8"))
    thermal_log = json.loads(copied["thermal_solver_log"]["target_path"].read_text(encoding="utf-8"))
    checks = json.loads(copied["reference_solver_checks"]["target_path"].read_text(encoding="utf-8"))
    validation_specs = [
        (
            f"val_{slug}_electrical_convergence", "solver_convergence", electrical_run_id,
            "electrical maximum update convergence", "Maximum Jacobi update in electrical potential solve.",
            "residual_max_delta_v", "<=", float(electrical_log["convergence_threshold_v"]), "V", float(electrical_log["residual_max_delta_v"]),
            [source_artifact_ids["electrical_solver_log"]],
        ),
        (
            f"val_{slug}_thermal_convergence", "solver_convergence", thermal_run_id,
            "thermal maximum update convergence", "Maximum Jacobi update in steady thermal solve.",
            "residual_max_delta_k", "<=", float(thermal_log["convergence_threshold_k"]), "K", float(thermal_log["residual_max_delta_k"]),
            [source_artifact_ids["thermal_solver_log"]],
        ),
        (
            f"val_{slug}_coupling_power", "physical_rule_check", thermal_run_id,
            "coupling integrated power conservation", "Relative difference between electrical Joule power and mapped thermal source power.",
            "relative_integrated_power_error", "<=", 1.0e-12, "1", float(checks["coupling_relative_power_error"]),
            [source_artifact_ids["coupling_map"], source_artifact_ids["reference_solver_checks"]],
        ),
        (
            f"val_{slug}_mesh_independence", "mesh_independence", thermal_import_run_id,
            "coarse/fine peak-temperature comparison", "Relative peak-temperature difference between 31x17 and 61x33 structured grids.",
            "relative_peak_temperature_rise_difference", "<=", 0.01, "1", float(checks["mesh_relative_peak_temperature_rise_difference"]),
            [source_artifact_ids["reference_solver_checks"]],
        ),
    ]
    validations = []
    for validation_id, validation_type, subject_id, method_name, description, threshold_name, operator, threshold_value, unit, measured, evidence_ids in validation_specs:
        validations.append(
            {
                "entity_type": "Validation",
                "schema_version": "1.0",
                "validation_id": validation_id,
                "subject_refs": [_subject("Run", subject_id)],
                "validation_type": validation_type,
                "method": {"name": method_name, "description": description, "script_artifact_id": source_artifact_ids["generation_recipe"]},
                "threshold": {"name": threshold_name, "operator": operator, "value": threshold_value, "unit": unit},
                "result": {
                    "status": "passed" if measured <= threshold_value else "failed",
                    "summary": f"Measured {threshold_name}={measured:.17g} {unit} against {operator} {threshold_value:.17g} {unit}.",
                    "measured_values": [{threshold_name: measured}],
                },
                "evidence_artifact_ids": evidence_ids,
                "executed_at": processed,
                "provenance_id": processing_prov_id,
            }
        )
    _write_json(case / "validation.json", {"schema_version": "1.0", "validations": validations})
    rebuild_indexes(root)
    validation = DADCRepository(root).validate()
    if not validation.valid:
        raise RuntimeError(json.dumps(validation.to_dict(), ensure_ascii=False, indent=2))
    return FieldBundleIngestionResult(
        repository=root,
        case_id=case_id,
        source_sha256=source_hash,
        node_count=int(bundle["mesh"]["node_count"]),
        cell_count=int(bundle["mesh"]["cell_count"]),
        metrics=outputs,
        validation=validation.to_dict(),
    )
