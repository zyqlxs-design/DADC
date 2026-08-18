"""Deterministic antenna, filter, and multiphysics example generator."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .constants import ENTITY_ID_FIELDS
from .integrity import artifact_file_record

STAMP = "2026-08-17T09:00:00Z"


def _write_text_once(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(content)


def _write_json_once(path: Path, value: Any) -> None:
    _write_text_once(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _record_path(root: Path, case_id: str, record: dict[str, Any]) -> Path:
    entity_type = record["entity_type"]
    identifier = record[ENTITY_ID_FIELDS[entity_type]]
    return root / "cases" / case_id / "metadata" / entity_type.lower() / f"{identifier}.json"


def _write_record(root: Path, case_id: str, record: dict[str, Any]) -> None:
    _write_json_once(_record_path(root, case_id, record), record)


def _h5_ref(root: Path, path: Path, object_path: str) -> str:
    return f"{path.relative_to(root).as_posix()}:{object_path}"


def _subject(entity_type: str, entity_id: str) -> dict[str, str]:
    return {"entity_type": entity_type, "entity_id": entity_id}


def _provenance(
    provenance_id: str,
    subjects: list[dict[str, str]],
    scripts: list[str],
    *,
    source_type: str = "generated",
) -> dict[str, Any]:
    return {
        "entity_type": "Provenance",
        "schema_version": "1.0",
        "provenance_id": provenance_id,
        "subject_refs": subjects,
        "source_type": source_type,
        "sources": [
            {
                "source_id": "dadc-v1-fixture-spec",
                "source_type": "report",
                "title": "DADC V1.0 deterministic acceptance fixture specification",
                "license": "MIT",
            }
        ],
        "software": [
            {"name": "DADC fixture generator", "version": "1.0.0", "role": "synthetic data generation"}
        ],
        "scripts": scripts,
        "people": [{"person_id": "dadc_automation", "role": "fixture maintainer"}],
        "generated_at": STAMP,
    }


def _artifact(
    root: Path,
    case_id: str,
    path: Path,
    artifact_id: str,
    subject_refs: list[dict[str, str]],
    role: str,
    media_type: str,
    origin: str,
    provenance_id: str,
    *,
    immutable: bool = True,
) -> dict[str, Any]:
    record = artifact_file_record(
        root,
        path,
        artifact_id=artifact_id,
        subject_refs=subject_refs,
        artifact_role=role,
        media_type=media_type,
        immutable=immutable,
        value_origin=origin,
        provenance_id=provenance_id,
        created_at=STAMP,
    )
    _write_record(root, case_id, record)
    return record


def _write_complex_group(handle: h5py.File, path: str, values: np.ndarray, components: list[str]) -> None:
    group = handle.create_group(path)
    group.create_dataset("real", data=np.real(values), compression="gzip")
    group.create_dataset("imaginary", data=np.imag(values), compression="gzip")
    group.attrs["complex_representation"] = "real_imaginary"
    group.attrs["components"] = json.dumps(components)


def _create_antenna(root: Path) -> None:
    case_id = "antenna_patch_case"
    case = root / "cases" / case_id
    data_path = case / "data" / "results.h5"
    native_path = case / "raw" / "antenna_model.py"
    script_path = case / "scripts" / "extract_metrics.py"
    log_path = case / "logs" / "solver.log"
    schema_evidence = case / "evidence" / "schema_validation.txt"
    physics_evidence = case / "evidence" / "passivity_check.txt"
    for directory in (data_path.parent, native_path.parent, script_path.parent, log_path.parent, schema_evidence.parent):
        directory.mkdir(parents=True, exist_ok=True)

    _write_text_once(native_path, "# Synthetic parametric patch antenna fixture\npatch_width_mm = 37.2\npatch_length_mm = 29.1\n")
    _write_text_once(script_path, "# Metric rule: resonance = frequency at minimum |S11|\n")
    _write_text_once(log_path, "Synthetic fixture solver converged in 8 adaptive passes; delta_S=0.006.\n")
    _write_text_once(schema_evidence, "JSON Schema error count: 0\n")
    _write_text_once(physics_evidence, "Maximum accepted power ratio: 0.998; passivity threshold: 1.001\n")

    frequency = np.linspace(2.0e9, 3.0e9, 101)
    x = (frequency - 2.45e9) / 8.0e7
    s11 = (0.08 + 0.18j * x) / (1.0 + 1j * x)
    s11_db = 20.0 * np.log10(np.maximum(np.abs(s11), 1.0e-12))
    theta = np.linspace(0.0, np.pi, 37)
    phi = np.linspace(0.0, 2.0 * np.pi, 73)
    theta_grid, phi_grid = np.meshgrid(theta, phi, indexing="ij")
    e_theta = np.sin(theta_grid) * np.exp(1j * 0.1 * np.cos(phi_grid))
    e_phi = 0.05 * np.sin(theta_grid) * np.exp(-1j * phi_grid)
    far_field = np.stack([e_theta, e_phi], axis=-1)
    with h5py.File(data_path, "x") as handle:
        axes = handle.create_group("axes")
        axes.create_dataset("frequency", data=frequency)
        axes.create_dataset("theta", data=theta)
        axes.create_dataset("phi", data=phi)
        _write_complex_group(handle, "observables/s_parameters", s11[:, None], ["S11"])
        _write_complex_group(handle, "observables/far_field", far_field, ["E_theta", "E_phi"])
        handle.create_group("derived").create_dataset("s11_db", data=s11_db)

    prov_id = "prov_ant_fixture"
    manual_prov_id = "prov_ant_manual"
    native_id = "art_ant_native"
    script_id = "art_ant_extract_script"
    log_id = "art_ant_log"
    data_id = "art_ant_results_h5"
    schema_ev_id = "art_ant_schema_evidence"
    physics_ev_id = "art_ant_physics_evidence"

    _write_record(root, case_id, _provenance(prov_id, [_subject("Run", "run_ant_sim_001")], [script_id]))
    _write_record(
        root,
        case_id,
        _provenance(
            manual_prov_id,
            [_subject("Metric", "metric_ant_visual_review")],
            [],
            source_type="manual_entry",
        ),
    )

    _artifact(root, case_id, native_path, native_id, [_subject("DesignRevision", "rev_ant_001")], "native_project", "text/x-python", "manual_entry", prov_id)
    _artifact(root, case_id, script_path, script_id, [_subject("Run", "run_ant_sim_001")], "script", "text/x-python", "manual_entry", prov_id)
    _artifact(root, case_id, log_path, log_id, [_subject("Run", "run_ant_sim_001")], "solver_log", "text/plain", "raw_solver_output", prov_id)
    _artifact(root, case_id, data_path, data_id, [_subject("Run", "run_ant_sim_001")], "result_hdf5", "application/x-hdf5", "raw_solver_output", prov_id)
    _artifact(root, case_id, schema_evidence, schema_ev_id, [_subject("Validation", "val_ant_schema")], "validation_evidence", "text/plain", "calculated", prov_id)
    _artifact(root, case_id, physics_evidence, physics_ev_id, [_subject("Validation", "val_ant_passivity")], "validation_evidence", "text/plain", "calculated", prov_id)

    device = {
        "entity_type": "Device",
        "schema_version": "1.0",
        "device_id": "device_ant_001",
        "name": "Synthetic 2.45 GHz patch antenna",
        "device_class": "antenna",
        "device_subtype": "microstrip_patch_antenna",
        "physics_domains": ["electromagnetics"],
        "profile_schema": "device_profiles/antenna.schema.json",
        "extensions": {"antenna": {"feed_type": "inset_microstrip", "radiation_mode": "broadside", "port_count": 1}},
        "tags": ["synthetic_fixture", "rf"],
        "created_at": STAMP,
    }
    revision = {
        "entity_type": "DesignRevision",
        "schema_version": "1.0",
        "design_revision_id": "rev_ant_001",
        "device_id": "device_ant_001",
        "revision_label": "R1",
        "geometry": {
            "representation": "parametric",
            "coordinate_system_ref": "cs_ant_cartesian",
            "parameters": [
                {"name": "patch_width", "value": 37.2, "unit": "mm", "value_origin": "manual_entry"},
                {"name": "patch_length", "value": 29.1, "unit": "mm", "value_origin": "manual_entry"},
            ],
        },
        "materials": [
            {
                "material_id": "copper_fixture",
                "role": "conductor",
                "properties": {"conductivity_S_per_m": 58000000.0},
                "source_provenance_id": prov_id,
            }
        ],
        "topology": {"radiator": "rectangular_patch", "ground_plane": "finite"},
        "artifact_ids": [native_id],
        "created_at": STAMP,
    }
    study = {
        "entity_type": "Study",
        "schema_version": "1.0",
        "study_id": "study_ant_sweep",
        "device_id": "device_ant_001",
        "design_revision_ids": ["rev_ant_001"],
        "study_type": "parameter_sweep",
        "physics_domains": ["electromagnetics"],
        "objectives": [{"metric": "minimum_S11", "operator": "minimize"}],
        "parameter_space": [{"name": "frequency", "start": 2.0e9, "stop": 3.0e9, "unit": "Hz"}],
        "run_ids": ["run_ant_sim_001"],
        "created_at": STAMP,
    }
    run = {
        "entity_type": "Run",
        "schema_version": "1.0",
        "run_id": "run_ant_sim_001",
        "study_id": "study_ant_sweep",
        "design_revision_id": "rev_ant_001",
        "activity_type": "simulation_run",
        "status": "succeeded",
        "physics_domains": ["electromagnetics"],
        "started_at": "2026-08-17T09:00:00Z",
        "ended_at": "2026-08-17T09:04:00Z",
        "input_artifact_ids": [native_id],
        "output_artifact_ids": [data_id, log_id],
        "provenance_id": prov_id,
        "environment": {"platform": "linux-x86_64", "compute": "deterministic synthetic fixture", "random_seed": 1001},
        "source_context": {"solver": {"name": "DADC synthetic EM solver", "version": "1.0"}, "boundary": "open"},
    }
    freq_ref = _h5_ref(root, data_path, "/axes/frequency")
    raw_s = {
        "entity_type": "Observable",
        "schema_version": "1.0",
        "observable_id": "obs_ant_s_complex",
        "run_id": "run_ant_sim_001",
        "observable_type": "s_parameters",
        "quantity": "complex_scattering_parameter",
        "axes": [{"name": "frequency", "unit": "Hz", "data_ref": freq_ref}],
        "components": ["S11"],
        "complex_representation": "real_imaginary",
        "data_ref": _h5_ref(root, data_path, "/observables/s_parameters"),
        "artifact_id": data_id,
        "coordinate_system_ref": None,
        "value_origin": "raw_solver_output",
        "provenance_id": prov_id,
    }
    derived_db = {
        "entity_type": "Observable",
        "schema_version": "1.0",
        "observable_id": "obs_ant_s11_db",
        "run_id": "run_ant_sim_001",
        "observable_type": "curve",
        "quantity": "scattering_parameter_magnitude_db",
        "axes": [{"name": "frequency", "unit": "Hz", "data_ref": freq_ref}],
        "components": ["S11_dB"],
        "complex_representation": "not_applicable",
        "data_ref": _h5_ref(root, data_path, "/derived/s11_db"),
        "artifact_id": data_id,
        "coordinate_system_ref": None,
        "value_origin": "calculated",
        "provenance_id": prov_id,
        "derived_from_observable_ids": ["obs_ant_s_complex"],
        "derivation": {"method": "20*log10(abs(S11))", "script_artifact_id": script_id},
    }
    far_field_observable = {
        "entity_type": "Observable",
        "schema_version": "1.0",
        "observable_id": "obs_ant_far_field",
        "run_id": "run_ant_sim_001",
        "observable_type": "field",
        "quantity": "complex_electric_far_field",
        "axes": [
            {"name": "theta", "unit": "rad", "data_ref": _h5_ref(root, data_path, "/axes/theta")},
            {"name": "phi", "unit": "rad", "data_ref": _h5_ref(root, data_path, "/axes/phi")},
        ],
        "components": ["E_theta", "E_phi"],
        "complex_representation": "real_imaginary",
        "data_ref": _h5_ref(root, data_path, "/observables/far_field"),
        "artifact_id": data_id,
        "coordinate_system_ref": "cs_ant_spherical_far_field",
        "value_origin": "raw_solver_output",
        "provenance_id": prov_id,
        "field_metadata": {
            "coordinate_system_ref": "cs_ant_spherical_far_field",
            "coordinate_unit": "rad",
            "mesh_type": "structured",
            "components": ["E_theta", "E_phi"],
            "condition": {"kind": "frequency", "value": 2450000000.0, "unit": "Hz"},
            "data_ref": _h5_ref(root, data_path, "/observables/far_field"),
            "normalization": "accepted_power_1_W",
        },
    }
    resonance_index = int(np.argmin(np.abs(s11)))
    metric = {
        "entity_type": "Metric",
        "schema_version": "1.0",
        "metric_id": "metric_ant_resonance",
        "run_id": "run_ant_sim_001",
        "name": "Resonant frequency",
        "quantity": "frequency_at_minimum_S11",
        "value": float(frequency[resonance_index]),
        "unit": "Hz",
        "value_origin": "calculated",
        "source_observable_ids": ["obs_ant_s_complex"],
        "extraction_method": "argmin(abs(S11))",
        "calculation": {"algorithm": "numpy.argmin", "script_artifact_id": script_id},
        "provenance_id": prov_id,
    }
    manual_metric = {
        "entity_type": "Metric",
        "schema_version": "1.0",
        "metric_id": "metric_ant_visual_review",
        "run_id": "run_ant_sim_001",
        "name": "Pattern visual review score",
        "quantity": "operator_review_score",
        "value": 4,
        "unit": "score_1_to_5",
        "value_origin": "manual_entry",
        "source_observable_ids": ["obs_ant_far_field"],
        "extraction_method": "human review of far-field pattern",
        "manual_entry_context": {"person_id": "dadc_automation", "entry_time": STAMP, "reason": "test provenance distinction"},
        "provenance_id": manual_prov_id,
    }
    for record in (device, revision, study, run, raw_s, derived_db, far_field_observable, metric, manual_metric):
        _write_record(root, case_id, record)

    validations = [
        {
            "entity_type": "Validation",
            "schema_version": "1.0",
            "validation_id": "val_ant_schema",
            "subject_refs": [_subject("Device", "device_ant_001")],
            "validation_type": "schema_validation",
            "method": {"name": "Draft 2020-12 validation", "description": "Validate all antenna case records against V1.0 schemas."},
            "threshold": {"name": "schema_error_count", "operator": "==", "value": 0, "unit": "count"},
            "result": {"status": "passed", "summary": "No schema errors in generated fixture.", "measured_values": [{"schema_error_count": 0}]},
            "evidence_artifact_ids": [schema_ev_id],
            "executed_at": STAMP,
            "provenance_id": prov_id,
        },
        {
            "entity_type": "Validation",
            "schema_version": "1.0",
            "validation_id": "val_ant_passivity",
            "subject_refs": [_subject("Run", "run_ant_sim_001")],
            "validation_type": "physical_rule_check",
            "method": {"name": "passivity bound", "description": "Check accepted-power ratio is no greater than threshold."},
            "threshold": {"name": "accepted_power_ratio", "operator": "<=", "value": 1.001, "unit": "1"},
            "result": {"status": "passed", "summary": "Synthetic response satisfies the configured passivity bound.", "measured_values": [{"accepted_power_ratio": 0.998}]},
            "evidence_artifact_ids": [physics_ev_id],
            "executed_at": STAMP,
            "provenance_id": prov_id,
        },
    ]
    _write_json_once(case / "validation.json", {"schema_version": "1.0", "validations": validations})


def _create_filter(root: Path) -> None:
    case_id = "microstrip_filter_case"
    case = root / "cases" / case_id
    data_path = case / "data" / "results.h5"
    native_path = case / "raw" / "filter_model.py"
    script_path = case / "scripts" / "extract_metrics.py"
    failed_log = case / "logs" / "run_failed.log"
    success_log = case / "logs" / "run_success.log"
    convergence_evidence = case / "evidence" / "convergence_check.txt"
    mesh_evidence = case / "evidence" / "mesh_independence.txt"
    for directory in (data_path.parent, native_path.parent, script_path.parent, failed_log.parent, convergence_evidence.parent):
        directory.mkdir(parents=True, exist_ok=True)

    _write_text_once(native_path, "# Synthetic third-order microstrip bandpass filter fixture\nresonator_length_mm = 28.4\n")
    _write_text_once(script_path, "# Metric rule: insertion loss = -max(S21_dB) in passband\n")
    _write_text_once(failed_log, "Mesh generation failed: local element limit exceeded (E_MESH_042).\n")
    _write_text_once(success_log, "Retry with bounded refinement converged in 6 passes; delta_S=0.008.\n")
    _write_text_once(convergence_evidence, "Final adaptive delta_S=0.008; threshold=0.01\n")
    _write_text_once(mesh_evidence, "Insertion-loss delta coarse-to-fine=0.031 dB; threshold=0.05 dB\n")

    frequency = np.linspace(1.0e9, 2.0e9, 121)
    phase = -2.0 * np.pi * (frequency - 1.0e9) / 1.0e9
    transmission = 0.9 * np.exp(-((frequency - 1.5e9) / 2.3e8) ** 8)
    s21 = transmission * np.exp(1j * phase)
    reflection_mag = np.sqrt(np.maximum(1.0 - transmission**2, 0.0)) * 0.95
    s11 = reflection_mag * np.exp(-0.4j * phase)
    s_matrix = np.empty((frequency.size, 2, 2), dtype=np.complex128)
    s_matrix[:, 0, 0] = s11
    s_matrix[:, 1, 1] = s11
    s_matrix[:, 0, 1] = s21
    s_matrix[:, 1, 0] = s21
    s21_db = 20.0 * np.log10(np.maximum(np.abs(s21), 1.0e-12))
    with h5py.File(data_path, "x") as handle:
        handle.create_group("axes").create_dataset("frequency", data=frequency)
        _write_complex_group(handle, "observables/s_parameters", s_matrix, ["S11", "S12", "S21", "S22"])
        handle.create_group("derived").create_dataset("s21_db", data=s21_db)

    prov_id = "prov_filter_fixture"
    native_id = "art_filter_native"
    script_id = "art_filter_extract_script"
    failed_log_id = "art_filter_failed_log"
    success_log_id = "art_filter_success_log"
    data_id = "art_filter_results_h5"
    convergence_id = "art_filter_convergence_evidence"
    mesh_id = "art_filter_mesh_evidence"
    _write_record(root, case_id, _provenance(prov_id, [_subject("Run", "run_filter_success")], [script_id]))
    _artifact(root, case_id, native_path, native_id, [_subject("DesignRevision", "rev_filter_001")], "native_project", "text/x-python", "manual_entry", prov_id)
    _artifact(root, case_id, script_path, script_id, [_subject("Run", "run_filter_success")], "script", "text/x-python", "manual_entry", prov_id)
    _artifact(root, case_id, failed_log, failed_log_id, [_subject("Run", "run_filter_failed")], "solver_log", "text/plain", "raw_solver_output", prov_id)
    _artifact(root, case_id, success_log, success_log_id, [_subject("Run", "run_filter_success")], "solver_log", "text/plain", "raw_solver_output", prov_id)
    _artifact(root, case_id, data_path, data_id, [_subject("Run", "run_filter_success")], "result_hdf5", "application/x-hdf5", "raw_solver_output", prov_id)
    _artifact(root, case_id, convergence_evidence, convergence_id, [_subject("Validation", "val_filter_convergence")], "validation_evidence", "text/plain", "calculated", prov_id)
    _artifact(root, case_id, mesh_evidence, mesh_id, [_subject("Validation", "val_filter_mesh")], "validation_evidence", "text/plain", "calculated", prov_id)

    records = [
        {
            "entity_type": "Device", "schema_version": "1.0", "device_id": "device_filter_001",
            "name": "Synthetic third-order microstrip bandpass filter", "device_class": "rf_filter",
            "device_subtype": "microstrip_bandpass_filter", "physics_domains": ["electromagnetics"],
            "profile_schema": "device_profiles/rf_filter.schema.json",
            "extensions": {"rf_filter": {"filter_response": "bandpass", "order": 3, "port_count": 2}},
            "tags": ["synthetic_fixture", "rf"], "created_at": STAMP,
        },
        {
            "entity_type": "DesignRevision", "schema_version": "1.0", "design_revision_id": "rev_filter_001",
            "device_id": "device_filter_001", "revision_label": "R1",
            "geometry": {"representation": "parametric", "coordinate_system_ref": "cs_filter_cartesian", "parameters": [
                {"name": "resonator_length", "value": 28.4, "unit": "mm", "value_origin": "manual_entry"},
                {"name": "coupling_gap", "value": 0.35, "unit": "mm", "value_origin": "manual_entry"}
            ]},
            "materials": [{"material_id": "substrate_fixture", "role": "substrate", "properties": {"relative_permittivity": 3.48, "loss_tangent": 0.0037}, "source_provenance_id": prov_id}],
            "topology": {"resonators": 3, "coupling": "edge"}, "artifact_ids": [native_id], "created_at": STAMP,
        },
        {
            "entity_type": "Study", "schema_version": "1.0", "study_id": "study_filter_retry",
            "device_id": "device_filter_001", "design_revision_ids": ["rev_filter_001"], "study_type": "validation",
            "physics_domains": ["electromagnetics"], "objectives": [{"metric": "insertion_loss", "operator": "minimize"}],
            "run_ids": ["run_filter_failed", "run_filter_success"], "created_at": STAMP,
        },
        {
            "entity_type": "Run", "schema_version": "1.0", "run_id": "run_filter_failed",
            "study_id": "study_filter_retry", "design_revision_id": "rev_filter_001", "activity_type": "simulation_run",
            "status": "failed", "physics_domains": ["electromagnetics"], "started_at": "2026-08-17T09:10:00Z",
            "ended_at": "2026-08-17T09:10:30Z", "input_artifact_ids": [native_id], "output_artifact_ids": [failed_log_id],
            "provenance_id": prov_id, "environment": {"platform": "linux-x86_64", "compute": "deterministic synthetic fixture", "random_seed": 2001},
            "source_context": {"solver": {"name": "DADC synthetic EM solver", "version": "1.0"}, "mesh_policy": "unbounded_local_refinement"},
            "failure": {"stage": "mesh_generation", "error_code": "E_MESH_042", "message": "Local element limit exceeded", "log_artifact_id": failed_log_id, "recoverable": True},
        },
        {
            "entity_type": "Run", "schema_version": "1.0", "run_id": "run_filter_success", "parent_run_id": "run_filter_failed",
            "study_id": "study_filter_retry", "design_revision_id": "rev_filter_001", "activity_type": "simulation_run",
            "status": "succeeded", "physics_domains": ["electromagnetics"], "started_at": "2026-08-17T09:12:00Z",
            "ended_at": "2026-08-17T09:16:00Z", "input_artifact_ids": [native_id], "output_artifact_ids": [data_id, success_log_id],
            "provenance_id": prov_id, "environment": {"platform": "linux-x86_64", "compute": "deterministic synthetic fixture", "random_seed": 2001},
            "source_context": {"solver": {"name": "DADC synthetic EM solver", "version": "1.0"}, "mesh_policy": "bounded_refinement"},
        },
    ]
    freq_ref = _h5_ref(root, data_path, "/axes/frequency")
    records.extend([
        {
            "entity_type": "Observable", "schema_version": "1.0", "observable_id": "obs_filter_s_complex",
            "run_id": "run_filter_success", "observable_type": "s_parameters", "quantity": "complex_scattering_parameter",
            "axes": [{"name": "frequency", "unit": "Hz", "data_ref": freq_ref}], "components": ["S11", "S12", "S21", "S22"],
            "complex_representation": "real_imaginary", "data_ref": _h5_ref(root, data_path, "/observables/s_parameters"),
            "artifact_id": data_id, "coordinate_system_ref": None, "value_origin": "raw_solver_output", "provenance_id": prov_id,
        },
        {
            "entity_type": "Observable", "schema_version": "1.0", "observable_id": "obs_filter_s21_db",
            "run_id": "run_filter_success", "observable_type": "curve", "quantity": "scattering_parameter_magnitude_db",
            "axes": [{"name": "frequency", "unit": "Hz", "data_ref": freq_ref}], "components": ["S21_dB"],
            "complex_representation": "not_applicable", "data_ref": _h5_ref(root, data_path, "/derived/s21_db"),
            "artifact_id": data_id, "coordinate_system_ref": None, "value_origin": "calculated", "provenance_id": prov_id,
            "derived_from_observable_ids": ["obs_filter_s_complex"],
            "derivation": {"method": "20*log10(abs(S21))", "script_artifact_id": script_id},
        },
        {
            "entity_type": "Metric", "schema_version": "1.0", "metric_id": "metric_filter_insertion_loss",
            "run_id": "run_filter_success", "name": "Minimum passband insertion loss", "quantity": "insertion_loss",
            "value": float(-np.max(s21_db)), "unit": "dB", "value_origin": "calculated",
            "source_observable_ids": ["obs_filter_s_complex"], "extraction_method": "-max(20*log10(abs(S21)))",
            "calculation": {"passband_Hz": [1.3e9, 1.7e9], "script_artifact_id": script_id}, "provenance_id": prov_id,
        },
    ])
    for record in records:
        _write_record(root, case_id, record)

    validations = [
        {
            "entity_type": "Validation", "schema_version": "1.0", "validation_id": "val_filter_convergence",
            "subject_refs": [_subject("Run", "run_filter_success")], "validation_type": "solver_convergence",
            "method": {"name": "adaptive S-parameter delta", "description": "Compare consecutive adaptive passes."},
            "threshold": {"name": "delta_S", "operator": "<=", "value": 0.01, "unit": "1"},
            "result": {"status": "passed", "summary": "Final adaptive delta is below threshold.", "measured_values": [{"delta_S": 0.008}]},
            "evidence_artifact_ids": [convergence_id], "executed_at": STAMP, "provenance_id": prov_id,
        },
        {
            "entity_type": "Validation", "schema_version": "1.0", "validation_id": "val_filter_mesh",
            "subject_refs": [_subject("Run", "run_filter_success")], "validation_type": "mesh_independence",
            "method": {"name": "two-level mesh comparison", "description": "Compare insertion loss at two mesh densities."},
            "threshold": {"name": "insertion_loss_delta", "operator": "<=", "value": 0.05, "unit": "dB"},
            "result": {"status": "passed", "summary": "Insertion-loss change is below threshold.", "measured_values": [{"insertion_loss_delta_dB": 0.031}]},
            "evidence_artifact_ids": [mesh_id], "executed_at": STAMP, "provenance_id": prov_id,
        },
    ]
    _write_json_once(case / "validation.json", {"schema_version": "1.0", "validations": validations})


def _create_multiphysics(root: Path) -> None:
    case_id = "multiphysics_connector_case"
    case = root / "cases" / case_id
    data_path = case / "data" / "results.h5"
    native_path = case / "raw" / "connector_model.json"
    coupling_script = case / "scripts" / "coupling_map.py"
    log_path = case / "logs" / "coupled_run.log"
    evidence_path = case / "evidence" / "cross_solver_energy.txt"
    for directory in (data_path.parent, native_path.parent, coupling_script.parent, log_path.parent, evidence_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
    _write_text_once(native_path, json.dumps({"model": "synthetic coax connector", "units": "mm"}) + "\n")
    _write_text_once(coupling_script, "# Map EM loss density -> thermal volumetric heat source -> structural temperature load\n")
    _write_text_once(log_path, "EM, thermal, and structural synthetic stages completed.\n")
    _write_text_once(evidence_path, "Mapped EM loss=1.000 W; integrated thermal source=0.999 W; relative error=0.001\n")

    x = np.linspace(-5.0, 5.0, 11)
    y = np.linspace(-5.0, 5.0, 11)
    z = np.linspace(0.0, 20.0, 21)
    xx, yy, zz = np.meshgrid(x, y, z, indexing="ij")
    loss = np.exp(-(xx**2 + yy**2) / 8.0) * (1.0 + 0.02 * zz)
    temperature = 293.15 + 18.0 * loss / np.max(loss)
    displacement = np.stack([np.zeros_like(temperature), np.zeros_like(temperature), 1.2e-6 * (temperature - 293.15)], axis=-1)
    with h5py.File(data_path, "x") as handle:
        axes_group = handle.create_group("axes")
        axes_group.create_dataset("x", data=x)
        axes_group.create_dataset("y", data=y)
        axes_group.create_dataset("z", data=z)
        observable_group = handle.create_group("observables")
        observable_group.create_dataset("em_loss_density", data=loss, compression="gzip")
        observable_group.create_dataset("temperature", data=temperature, compression="gzip")
        observable_group.create_dataset("displacement", data=displacement, compression="gzip")

    prov_id = "prov_multi_fixture"
    native_id = "art_multi_native"
    coupling_id = "art_multi_coupling_script"
    log_id = "art_multi_log"
    data_id = "art_multi_results_h5"
    evidence_id = "art_multi_energy_evidence"
    _write_record(root, case_id, _provenance(prov_id, [_subject("Study", "study_multi_coupled")], [coupling_id]))
    _artifact(root, case_id, native_path, native_id, [_subject("DesignRevision", "rev_multi_001")], "native_project", "application/json", "manual_entry", prov_id)
    _artifact(root, case_id, coupling_script, coupling_id, [_subject("Study", "study_multi_coupled")], "script", "text/x-python", "calculated", prov_id)
    _artifact(root, case_id, log_path, log_id, [_subject("Study", "study_multi_coupled")], "solver_log", "text/plain", "raw_solver_output", prov_id)
    _artifact(root, case_id, data_path, data_id, [_subject("Study", "study_multi_coupled")], "result_hdf5", "application/x-hdf5", "raw_solver_output", prov_id)
    _artifact(root, case_id, evidence_path, evidence_id, [_subject("Validation", "val_multi_energy")], "validation_evidence", "text/plain", "calculated", prov_id)

    run_ids = ["run_multi_em", "run_multi_thermal", "run_multi_structural"]
    records: list[dict[str, Any]] = [
        {
            "entity_type": "Device", "schema_version": "1.0", "device_id": "device_multi_001",
            "name": "Synthetic high-power coax connector", "device_class": "electrical_connector",
            "device_subtype": "coaxial_power_connector", "physics_domains": ["electromagnetics", "thermal", "structural"],
            "profile_schema": "device_profiles/multiphysics_component.schema.json",
            "extensions": {"multiphysics_component": {"coupling_strategy": "one_way", "primary_function": "RF power transfer"}},
            "tags": ["synthetic_fixture", "multiphysics"], "created_at": STAMP,
        },
        {
            "entity_type": "DesignRevision", "schema_version": "1.0", "design_revision_id": "rev_multi_001",
            "device_id": "device_multi_001", "revision_label": "R1",
            "geometry": {"representation": "parametric", "coordinate_system_ref": "cs_multi_cartesian", "parameters": [
                {"name": "length", "value": 20.0, "unit": "mm", "value_origin": "manual_entry"},
                {"name": "outer_radius", "value": 5.0, "unit": "mm", "value_origin": "manual_entry"}
            ]},
            "materials": [{"material_id": "copper_fixture", "role": "conductor_and_structure", "properties": {"conductivity_S_per_m": 58000000.0, "thermal_conductivity_W_per_mK": 400.0, "youngs_modulus_Pa": 110000000000.0}, "source_provenance_id": prov_id}],
            "topology": {"conductors": 2, "dielectric_regions": 1}, "artifact_ids": [native_id], "created_at": STAMP,
        },
        {
            "entity_type": "Study", "schema_version": "1.0", "study_id": "study_multi_coupled",
            "device_id": "device_multi_001", "design_revision_ids": ["rev_multi_001"], "study_type": "validation",
            "physics_domains": ["electromagnetics", "thermal", "structural"],
            "coupling_edges": [
                {"source_run_id": "run_multi_em", "target_run_id": "run_multi_thermal", "coupling_type": "one_way", "transferred_observable_ids": ["obs_multi_em_loss"], "mapping_artifact_id": coupling_id},
                {"source_run_id": "run_multi_thermal", "target_run_id": "run_multi_structural", "coupling_type": "one_way", "transferred_observable_ids": ["obs_multi_temperature"], "mapping_artifact_id": coupling_id}
            ],
            "run_ids": run_ids, "created_at": STAMP,
        },
    ]
    for index, (run_id, domain, solver_name) in enumerate([
        ("run_multi_em", "electromagnetics", "DADC synthetic EM solver"),
        ("run_multi_thermal", "thermal", "DADC synthetic thermal solver"),
        ("run_multi_structural", "structural", "DADC synthetic structural solver"),
    ]):
        records.append({
            "entity_type": "Run", "schema_version": "1.0", "run_id": run_id,
            "study_id": "study_multi_coupled", "design_revision_id": "rev_multi_001", "activity_type": "simulation_run",
            "status": "succeeded", "physics_domains": [domain], "started_at": f"2026-08-17T09:{20 + index * 5:02d}:00Z",
            "ended_at": f"2026-08-17T09:{24 + index * 5:02d}:00Z", "input_artifact_ids": [native_id, coupling_id] if index else [native_id],
            "output_artifact_ids": [data_id, log_id], "provenance_id": prov_id,
            "environment": {"platform": "linux-x86_64", "compute": "deterministic synthetic fixture", "random_seed": 3001 + index},
            "source_context": {"solver": {"name": solver_name, "version": "1.0"}, "stage": domain},
        })

    axes = [
        {"name": "x", "unit": "mm", "data_ref": _h5_ref(root, data_path, "/axes/x")},
        {"name": "y", "unit": "mm", "data_ref": _h5_ref(root, data_path, "/axes/y")},
        {"name": "z", "unit": "mm", "data_ref": _h5_ref(root, data_path, "/axes/z")},
    ]
    field_specs = [
        ("obs_multi_em_loss", "run_multi_em", "volumetric_electromagnetic_loss_density", ["loss_density"], "/observables/em_loss_density", "frequency", 2.0e9, "Hz", "input_power_1_W"),
        ("obs_multi_temperature", "run_multi_thermal", "temperature", ["T"], "/observables/temperature", "steady_state", "steady", "1", "absolute_temperature"),
        ("obs_multi_displacement", "run_multi_structural", "displacement", ["Ux", "Uy", "Uz"], "/observables/displacement", "steady_state", "steady", "1", "not_normalized"),
    ]
    for observable_id, run_id, quantity, components, object_path, condition_kind, condition_value, condition_unit, normalization in field_specs:
        data_ref = _h5_ref(root, data_path, object_path)
        records.append({
            "entity_type": "Observable", "schema_version": "1.0", "observable_id": observable_id,
            "run_id": run_id, "observable_type": "field", "quantity": quantity, "axes": axes,
            "components": components, "complex_representation": "not_applicable", "data_ref": data_ref,
            "artifact_id": data_id, "coordinate_system_ref": "cs_multi_cartesian", "value_origin": "raw_solver_output",
            "provenance_id": prov_id,
            "field_metadata": {"coordinate_system_ref": "cs_multi_cartesian", "coordinate_unit": "mm", "mesh_type": "structured",
                               "components": components, "condition": {"kind": condition_kind, "value": condition_value, "unit": condition_unit},
                               "data_ref": data_ref, "normalization": normalization},
        })
    records.append({
        "entity_type": "Metric", "schema_version": "1.0", "metric_id": "metric_multi_peak_temperature",
        "run_id": "run_multi_thermal", "name": "Peak steady-state temperature", "quantity": "maximum_temperature",
        "value": float(np.max(temperature)), "unit": "K", "value_origin": "calculated",
        "source_observable_ids": ["obs_multi_temperature"], "extraction_method": "max(T)",
        "calculation": {"algorithm": "numpy.max", "script_artifact_id": coupling_id}, "provenance_id": prov_id,
    })
    for record in records:
        _write_record(root, case_id, record)
    validation = {
        "entity_type": "Validation", "schema_version": "1.0", "validation_id": "val_multi_energy",
        "subject_refs": [_subject("Study", "study_multi_coupled")], "validation_type": "cross_solver_comparison",
        "method": {"name": "coupling energy conservation", "description": "Compare integrated EM loss with mapped thermal heat source.", "script_artifact_id": coupling_id},
        "threshold": {"name": "relative_energy_mapping_error", "operator": "<=", "value": 0.005, "unit": "1"},
        "result": {"status": "passed", "summary": "Cross-solver energy mapping is within threshold.", "measured_values": [{"relative_error": 0.001}]},
        "evidence_artifact_ids": [evidence_id], "executed_at": STAMP, "provenance_id": prov_id,
    }
    _write_json_once(case / "validation.json", {"schema_version": "1.0", "validations": [validation]})


def _scan_records(root: Path) -> list[tuple[str, str, dict[str, Any], Path]]:
    rows: list[tuple[str, str, dict[str, Any], Path]] = []
    for path in sorted((root / "cases").glob("*/metadata/*/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        entity_type = record["entity_type"]
        rows.append((entity_type, record[ENTITY_ID_FIELDS[entity_type]], record, path))
    for path in sorted((root / "cases").glob("*/validation.json")):
        for record in json.loads(path.read_text(encoding="utf-8"))["validations"]:
            rows.append(("Validation", record["validation_id"], record, path))
    return rows


def _create_indexes(root: Path) -> None:
    index_dir = root / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    records = _scan_records(root)
    catalog_rows = []
    metric_rows = []
    for entity_type, identifier, record, path in records:
        case_id = path.relative_to(root).parts[1]
        catalog_rows.append({
            "case_id": case_id,
            "entity_type": entity_type,
            "entity_id": identifier,
            "schema_version": record["schema_version"],
            "json_path": path.relative_to(root).as_posix(),
        })
        if entity_type == "Metric":
            metric_rows.append({
                "case_id": case_id,
                "metric_id": record["metric_id"],
                "run_id": record["run_id"],
                "name": record["name"],
                "quantity": record["quantity"],
                "value": float(record["value"]),
                "unit": record["unit"],
                "value_origin": record["value_origin"],
                "source_observable_ids_json": json.dumps(record["source_observable_ids"]),
            })
    pq.write_table(pa.Table.from_pylist(catalog_rows), index_dir / "catalog.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(metric_rows), index_dir / "metrics.parquet", compression="zstd")

    case_id = "antenna_patch_case"
    prov_id = "prov_repository_index"
    _write_record(root, case_id, _provenance(prov_id, [_subject("Device", "device_ant_001"), _subject("Device", "device_filter_001"), _subject("Device", "device_multi_001")], []))
    _artifact(root, case_id, index_dir / "catalog.parquet", "art_index_catalog", [_subject("Device", "device_ant_001")], "index_parquet", "application/vnd.apache.parquet", "calculated", prov_id)
    _artifact(root, case_id, index_dir / "metrics.parquet", "art_index_metrics", [_subject("Metric", "metric_ant_resonance")], "index_parquet", "application/vnd.apache.parquet", "calculated", prov_id)


def create_demo_repository(target: str | Path, *, replace: bool = False) -> Path:
    """Create a complete repository; never overwrite a non-empty directory implicitly."""

    root = Path(target).resolve()
    if root.exists() and any(root.iterdir()):
        if not replace:
            raise FileExistsError(f"Refusing to overwrite non-empty directory: {root}")
        manifest = root / "repository.json"
        if not manifest.is_file():
            raise ValueError(f"Refusing to replace a directory that is not a DADC repository: {root}")
        if root in {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}:
            raise ValueError(f"Unsafe replacement target: {root}")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    source_schemas = Path(__file__).resolve().parents[2] / "schemas"
    if not source_schemas.is_dir():
        source_schemas = Path(sys.prefix) / "share" / "dadc" / "schemas"
    if not source_schemas.is_dir():
        raise FileNotFoundError("Bundled DADC schemas were not found")
    shutil.copytree(source_schemas, root / "schemas")
    manifest = {
        "repository_id": "dadc_v1_demo_repository",
        "schema_version": "1.0",
        "created_at": STAMP,
        "cases": [
            {"case_id": "antenna_patch_case", "path": "cases/antenna_patch_case"},
            {"case_id": "microstrip_filter_case", "path": "cases/microstrip_filter_case"},
            {"case_id": "multiphysics_connector_case", "path": "cases/multiphysics_connector_case"},
        ],
        "indexes": [
            {"name": "global_catalog", "path": "index/catalog.parquet", "format": "parquet"},
            {"name": "metric_query", "path": "index/metrics.parquet", "format": "parquet"},
        ],
    }
    _write_json_once(root / "repository.json", manifest)
    _create_antenna(root)
    _create_filter(root)
    _create_multiphysics(root)
    _create_indexes(root)
    return root
