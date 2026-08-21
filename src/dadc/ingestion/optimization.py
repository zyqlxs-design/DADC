"""Normalize a typed optimization evidence bundle into frozen DADC V1.0 entities."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from ..contracts import validate_contract
from ..indexing import rebuild_indexes
from ..integrity import sha256_file
from ..repository import DADCRepository
from .importer import (
    _artifact,
    _h5_ref,
    _require_aware_datetime,
    _source_schemas,
    _subject,
    _write_json,
    _write_record,
)
from .touchstone import parse_touchstone

ADAPTER_VERSION = "1.0.0"
_SAFE_ID = re.compile(r"[^a-z0-9_]+")


@dataclass(frozen=True)
class OptimizationIngestionResult:
    repository: Path
    case_id: str
    source_sha256: str
    trial_count: int
    best_metric: dict[str, Any]
    validation: dict[str, Any]


def _canonical_sha256(value: Any) -> str:
    rendered = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _slug(value: str) -> str:
    rendered = _SAFE_ID.sub("_", value.lower()).strip("_")
    if not rendered or not rendered[0].isalpha():
        rendered = f"item_{rendered}"
    return rendered


def _safe_source_artifact(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"Unsafe optimization artifact path: {relative!r}")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Optimization artifact escapes its bundle directory: {relative!r}") from exc
    if not resolved.is_file():
        raise FileNotFoundError(f"Optimization artifact is missing: {resolved}")
    return resolved


def _load_and_check(source_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bundle = json.loads(source_path.read_text(encoding="utf-8"))
    validate_contract(bundle, "optimization_bundle")
    validate_contract(bundle["plan"], "optimization_plan")
    if bundle["plan_sha256"] != _canonical_sha256(bundle["plan"]):
        raise ValueError("optimization bundle plan_sha256 does not match the embedded plan")
    trials = [*bundle["search_trials"], *bundle["verification_trials"]]
    trial_ids = [item["trial_id"] for item in trials]
    if len(trial_ids) != len(set(trial_ids)):
        raise ValueError("Optimization trial IDs must be unique")
    for trial in trials:
        validate_contract(trial["job"], "simulation_job")
        expected_kind = (
            "search" if trial in bundle["search_trials"] else "independent_verification"
        )
        if trial["trial_kind"] != expected_kind or trial["job"]["trial_kind"] != expected_kind:
            raise ValueError(f"Trial kind mismatch: {trial['trial_id']}")
        if trial["status"] == "succeeded" and trial["metric"] is None:
            raise ValueError(f"Succeeded trial has no metric: {trial['trial_id']}")
    search_ids = {item["trial_id"] for item in bundle["search_trials"]}
    if bundle["best_search_trial_id"] not in search_ids:
        raise ValueError("best_search_trial_id does not identify a search trial")
    if bundle["budget"]["search_trials_executed"] != len(bundle["search_trials"]):
        raise ValueError("Search budget accounting does not match trial records")
    if bundle["budget"]["verification_runs_executed"] != len(bundle["verification_trials"]):
        raise ValueError("Verification budget accounting does not match trial records")
    artifact_paths = [item["relative_path"] for item in bundle["artifacts"]]
    if len(artifact_paths) != len(set(artifact_paths)):
        raise ValueError("Optimization artifact relative paths must be unique")
    declared_by_trial: dict[str, set[str]] = {identifier: set() for identifier in trial_ids}
    for item in bundle["artifacts"]:
        if item["trial_id"] not in declared_by_trial:
            raise ValueError(f"Artifact refers to unknown trial: {item['trial_id']}")
        path = _safe_source_artifact(source_path.parent, item["relative_path"])
        if path.stat().st_size != item["size_bytes"]:
            raise ValueError(f"Artifact size mismatch: {item['relative_path']}")
        if sha256_file(path) != item["sha256"]:
            raise ValueError(f"Artifact SHA-256 mismatch: {item['relative_path']}")
        declared_by_trial[item["trial_id"]].add(item["relative_path"])
    for trial in trials:
        if set(trial["artifact_paths"]) != declared_by_trial[trial["trial_id"]]:
            raise ValueError(f"Trial artifact list mismatch: {trial['trial_id']}")
    best = next(item for item in bundle["search_trials"] if item["trial_id"] == bundle["best_search_trial_id"])
    best_parameters = best["job"]["parameters"]
    for verification in bundle["verification_trials"]:
        if verification["job"]["parameters"] != best_parameters:
            raise ValueError("Independent verification must rerun the selected parameter point")
    return bundle, trials


def _fixture_objective(plan: dict[str, Any], trial: dict[str, Any]) -> float:
    options = plan["backend"]["options"]
    centers = options["centers"]
    weights = options.get("weights", {})
    value = float(options.get("offset", 0.0))
    for parameter in trial["job"]["parameters"]:
        delta = float(parameter["value"]) - float(centers[parameter["name"]])
        value += float(weights.get(parameter["name"], 1.0)) * delta * delta
    return value


def _touchstone_objective(
    plan: dict[str, Any],
    parsed: Any,
) -> float:
    values = 20.0 * np.log10(np.maximum(np.abs(parsed.values[:, 0, 0]), 1.0e-300))
    index = int(np.argmin(values))
    objective = plan["objective"]
    if objective["quantity"] == "target_frequency_error_hz":
        target = float(objective.get("target", plan["backend"]["options"].get("target_frequency_hz")))
        return abs(float(parsed.frequencies_hz[index]) - target)
    if objective["quantity"] == "resonance_frequency_hz":
        return float(parsed.frequencies_hz[index])
    if objective["quantity"] == "minimum_return_loss_db":
        return float(values[index])
    raise ValueError(f"Unsupported PyAEDT objective: {objective['quantity']!r}")


def ingest_optimization_bundle_repository(
    source: str | Path,
    target: str | Path,
    *,
    intake: dict[str, Any] | None = None,
) -> OptimizationIngestionResult:
    """Create one validated DADC case from an optimization trace and pinned evidence."""

    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Optimization bundle does not exist: {source_path}")
    bundle, trials = _load_and_check(source_path)
    plan = bundle["plan"]
    if intake and intake.get("case_id") not in (None, plan["case_id"]):
        raise ValueError("Intake case_id conflicts with the signed optimization plan")
    case_id = plan["case_id"]
    root = Path(target).resolve()
    if root.exists() and (root.is_file() or any(root.iterdir())):
        raise FileExistsError(f"Refusing to overwrite non-empty target: {root}")
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_source_schemas(), root / "schemas")
    case = root / "cases" / case_id
    evidence_root = case / "evidence"
    raw_root = case / "raw" / "automation"
    data_root = case / "data"
    scripts_root = case / "scripts"
    for directory in (evidence_root, raw_root, data_root, scripts_root):
        directory.mkdir(parents=True, exist_ok=True)

    processed = _require_aware_datetime(str(bundle["finished_at"]), "finished_at")
    source_copy = evidence_root / "optimization_bundle.json"
    shutil.copyfile(source_path, source_copy)
    if sha256_file(source_copy) != sha256_file(source_path):
        raise IOError("Optimization bundle copy failed byte-integrity verification")
    adapter_script = scripts_root / "optimization_adapter.py"
    shutil.copyfile(Path(__file__), adapter_script)

    artifact_items: dict[str, dict[str, Any]] = {}
    copied_paths: dict[str, Path] = {}
    for item in bundle["artifacts"]:
        original = _safe_source_artifact(source_path.parent, item["relative_path"])
        category = "raw" if item["role"] in {
            "native_project", "raw_input", "mesh", "solver_log", "measurement_data"
        } else "evidence"
        base = raw_root if category == "raw" else evidence_root / "automation"
        destination = base / Path(item["relative_path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
        if sha256_file(destination) != item["sha256"]:
            raise IOError(f"Optimization companion copy failed: {item['relative_path']}")
        artifact_items[item["relative_path"]] = item
        copied_paths[item["relative_path"]] = destination

    trial_by_id = {item["trial_id"]: item for item in trials}
    best_trial = trial_by_id[bundle["best_search_trial_id"]]
    backend_is_physical = bool(bundle["backend"]["is_physical_solver"])
    checks: dict[str, Any] = {
        "optimization_check_version": "1.0",
        "bundle_sha256": sha256_file(source_path),
        "plan_sha256_verified": True,
        "artifact_count": len(bundle["artifacts"]),
        "artifact_hashes_verified": True,
        "budget_accounting_verified": True,
        "best_point_independently_rerun": True,
        "backend_is_physical_solver": backend_is_physical,
        "trial_objective_checks": [],
    }

    h5_path = data_root / "optimization_trace.h5"
    parsed_by_trial: dict[str, Any] = {}
    objective_values: dict[str, float] = {}
    with h5py.File(h5_path, "x") as handle:
        trial_group = handle.create_group("trials")
        for trial in trials:
            if trial["status"] != "succeeded":
                continue
            group = trial_group.create_group(trial["trial_id"])
            parameters = trial["job"]["parameters"]
            parameter_data = np.array([float(item["value"]) for item in parameters], dtype=float)
            parameter_dataset = group.create_dataset("parameters", data=parameter_data)
            parameter_dataset.attrs["names"] = json.dumps([item["name"] for item in parameters])
            parameter_dataset.attrs["units"] = json.dumps([item["unit"] for item in parameters])
            declared = float(trial["metric"]["value"])
            if bundle["backend"]["backend_id"] == "analytic_fixture":
                recomputed = _fixture_objective(plan, trial)
            elif bundle["backend"]["backend_id"] == "pyaedt_patch":
                candidates = [
                    item
                    for item in bundle["artifacts"]
                    if item["trial_id"] == trial["trial_id"]
                    and item["media_type"] == "application/vnd.touchstone"
                ]
                if len(candidates) != 1:
                    raise ValueError(
                        f"Physical PyAEDT trial requires exactly one Touchstone artifact: {trial['trial_id']}"
                    )
                parsed = parse_touchstone(copied_paths[candidates[0]["relative_path"]])
                parsed_by_trial[trial["trial_id"]] = parsed
                frequency = group.create_dataset("frequency_hz", data=parsed.frequencies_hz)
                frequency.attrs["unit"] = "Hz"
                s_group = group.create_group("s_parameters")
                s_group.create_dataset("real", data=parsed.values.real, compression="gzip")
                s_group.create_dataset("imaginary", data=parsed.values.imag, compression="gzip")
                s_group.attrs["components"] = json.dumps(list(parsed.components))
                s_group.attrs["complex_representation"] = "real_imaginary"
                recomputed = _touchstone_objective(plan, parsed)
            else:
                raise ValueError(f"Unsupported optimization backend in adapter: {bundle['backend']['backend_id']}")
            if not math.isclose(declared, recomputed, rel_tol=1.0e-12, abs_tol=1.0e-9):
                raise ValueError(
                    f"Objective recomputation mismatch for {trial['trial_id']}: "
                    f"declared={declared}, recomputed={recomputed}"
                )
            group.create_dataset("objective", data=np.array(recomputed, dtype=float))
            objective_values[trial["trial_id"]] = recomputed
            checks["trial_objective_checks"].append(
                {"trial_id": trial["trial_id"], "declared": declared, "recomputed": recomputed}
            )
        verification_values = [
            objective_values[item["trial_id"]] for item in bundle["verification_trials"]
        ]
        summary = handle.create_group("summary")
        summary.create_dataset(
            "objective",
            data=np.array(
                [objective_values[best_trial["trial_id"]], float(np.mean(verification_values))],
                dtype=float,
            ),
        )
        summary["objective"].attrs["components"] = json.dumps(
            ["best_search", "independent_verification_mean"]
        )
        handle.attrs["adapter_id"] = "optimization_trace_bundle"
        handle.attrs["adapter_version"] = ADAPTER_VERSION
        handle.attrs["source_sha256"] = sha256_file(source_path)

    checks_path = evidence_root / "optimization_checks.json"
    _write_json(checks_path, checks)

    device_id = f"device_{case_id}"
    base_revision_id = f"rev_{case_id}_base"
    study_id = f"study_{case_id}_optimization"
    processing_run_id = f"run_{case_id}_optimization_summary"
    processing_prov_id = f"prov_{case_id}_optimization_summary"
    bundle_artifact_id = f"art_{case_id}_optimization_bundle"
    h5_artifact_id = f"art_{case_id}_optimization_trace_h5"
    script_artifact_id = f"art_{case_id}_optimization_adapter"
    checks_artifact_id = f"art_{case_id}_optimization_checks"
    summary_observable_id = f"obs_{case_id}_optimization_summary"
    best_metric_id = f"metric_{case_id}_best_objective"
    verified_metric_id = f"metric_{case_id}_verified_objective"

    search_revision_ids: dict[str, str] = {
        trial["trial_id"]: f"rev_{case_id}_{trial['trial_id']}"
        for trial in bundle["search_trials"]
    }
    revision_for_trial = dict(search_revision_ids)
    for trial in bundle["verification_trials"]:
        revision_for_trial[trial["trial_id"]] = search_revision_ids[best_trial["trial_id"]]
    run_ids = {trial["trial_id"]: f"run_{case_id}_{trial['trial_id']}" for trial in trials}
    provenance_ids = {
        trial["trial_id"]: f"prov_{case_id}_{trial['trial_id']}" for trial in trials
    }
    parameter_observable_ids = {
        trial["trial_id"]: f"obs_{case_id}_{trial['trial_id']}_parameters"
        for trial in trials
        if trial["status"] == "succeeded"
    }
    raw_observable_ids = {
        trial["trial_id"]: f"obs_{case_id}_{trial['trial_id']}_s_parameters"
        for trial in trials
        if trial["status"] == "succeeded" and backend_is_physical
    }
    objective_observable_ids = {
        trial["trial_id"]: f"obs_{case_id}_{trial['trial_id']}_objective"
        for trial in trials
        if trial["status"] == "succeeded"
    }
    trial_metric_ids = {
        trial["trial_id"]: f"metric_{case_id}_{trial['trial_id']}_objective"
        for trial in trials
        if trial["status"] == "succeeded"
    }

    companion_artifact_ids: dict[str, str] = {}
    per_trial_artifact_ids: dict[str, list[str]] = {trial["trial_id"]: [] for trial in trials}
    for index, item in enumerate(bundle["artifacts"], start=1):
        artifact_id = f"art_{case_id}_{_slug(item['trial_id'])}_{index:03d}"
        companion_artifact_ids[item["relative_path"]] = artifact_id
        per_trial_artifact_ids[item["trial_id"]].append(artifact_id)

    summary_subjects = [
        _subject("Run", processing_run_id),
        _subject("Observable", summary_observable_id),
        _subject("Metric", best_metric_id),
        _subject("Metric", verified_metric_id),
        _subject("Artifact", bundle_artifact_id),
        _subject("Artifact", h5_artifact_id),
        _subject("Artifact", script_artifact_id),
        _subject("Artifact", checks_artifact_id),
    ]
    processing_provenance = {
        "entity_type": "Provenance",
        "schema_version": "1.0",
        "provenance_id": processing_prov_id,
        "subject_refs": summary_subjects,
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
                "name": "DADC Optimization Trace Adapter",
                "version": ADAPTER_VERSION,
                "role": "contract validation, objective recomputation, and DADC normalization",
            }
        ],
        "scripts": [script_artifact_id],
        "people": [
            {
                "person_id": str(plan["backend"]["options"].get("operator_id", "local_user")),
                "role": "optimization operator",
            }
        ],
        "generated_at": processed,
    }
    _write_record(root, case_id, processing_provenance)

    for trial in trials:
        trial_id = trial["trial_id"]
        provenance = {
            "entity_type": "Provenance",
            "schema_version": "1.0",
            "provenance_id": provenance_ids[trial_id],
            "subject_refs": [
                _subject("Run", run_ids[trial_id]),
                _subject("DesignRevision", revision_for_trial[trial_id]),
                *[
                    _subject("Artifact", companion_artifact_ids[path])
                    for path in trial["artifact_paths"]
                ],
                *(
                    [_subject("Observable", parameter_observable_ids[trial_id])]
                    if trial_id in parameter_observable_ids
                    else []
                ),
                *(
                    [_subject("Observable", raw_observable_ids[trial_id])]
                    if trial_id in raw_observable_ids
                    else []
                ),
                *(
                    [
                        _subject("Observable", objective_observable_ids[trial_id]),
                        _subject("Metric", trial_metric_ids[trial_id]),
                    ]
                    if trial_id in objective_observable_ids
                    else []
                ),
            ],
            "source_type": "simulation" if backend_is_physical else "generated",
            "sources": [
                {
                    "source_id": bundle["plan_sha256"],
                    "source_type": "file",
                    "title": "embedded optimization plan",
                }
            ],
            "software": [
                {
                    "name": bundle["backend"]["backend_id"],
                    "version": bundle["backend"]["backend_version"],
                    "role": "physical simulation" if backend_is_physical else "non-physical contract fixture",
                }
            ],
            "scripts": [],
            "people": [
                {
                    "person_id": str(plan["backend"]["options"].get("operator_id", "local_user")),
                    "role": "optimization operator",
                }
            ],
            "generated_at": trial["ended_at"],
        }
        _write_record(root, case_id, provenance)

    _artifact(
        root,
        case_id,
        source_copy,
        bundle_artifact_id,
        [_subject("Study", study_id), _subject("Run", processing_run_id)],
        "report",
        "application/json",
        "calculated",
        processing_prov_id,
        processed,
    )
    _artifact(
        root,
        case_id,
        h5_path,
        h5_artifact_id,
        [
            _subject("Run", processing_run_id),
            _subject("Observable", summary_observable_id),
            *[
                _subject("Observable", identifier)
                for identifier in parameter_observable_ids.values()
            ],
            *[_subject("Observable", identifier) for identifier in raw_observable_ids.values()],
            *[
                _subject("Observable", identifier)
                for identifier in objective_observable_ids.values()
            ],
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
        adapter_script,
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
        checks_path,
        checks_artifact_id,
        [
            _subject("Run", processing_run_id),
            _subject("Validation", f"val_{case_id}_optimization_contract"),
        ],
        "validation_evidence",
        "application/json",
        "calculated",
        processing_prov_id,
        processed,
    )

    artifact_record_by_path = {item["relative_path"]: item for item in bundle["artifacts"]}
    for relative, artifact_id in companion_artifact_ids.items():
        item = artifact_record_by_path[relative]
        trial_id = item["trial_id"]
        _artifact(
            root,
            case_id,
            copied_paths[relative],
            artifact_id,
            [_subject("Run", run_ids[trial_id]), _subject("Study", study_id)],
            item["role"],
            item["media_type"],
            item["value_origin"],
            provenance_ids[trial_id],
            trial_by_id[trial_id]["ended_at"],
        )

    device = {
        "entity_type": "Device",
        "schema_version": "1.0",
        "device_id": device_id,
        "name": plan["device"]["name"],
        "device_class": plan["device"]["device_class"],
        "device_subtype": plan["device"]["device_subtype"],
        "physics_domains": plan["physics_domains"],
        "profile_schema": "device_profiles/generic_component.schema.json",
        "extensions": {
            "generic_component": {
                "identity_basis": "explicit_intake_manifest",
                "attributes": {
                    **plan["device"]["attributes"],
                    "optimization_backend": bundle["backend"]["backend_id"],
                    "is_physical_solver": backend_is_physical,
                    "evidence_level": bundle["backend"]["evidence_level"],
                },
            }
        },
        "tags": [
            "optimization_trace",
            "physical_solver" if backend_is_physical else "non_physical_contract_fixture",
        ],
        "created_at": processed,
    }
    base_revision = {
        "entity_type": "DesignRevision",
        "schema_version": "1.0",
        "design_revision_id": base_revision_id,
        "device_id": device_id,
        "revision_label": "optimization_base",
        "geometry": {"representation": "parametric", "parameters": []},
        "materials": [],
        "topology": {
            "source": "optimization_plan",
            "reconstruction_status": "backend_recipe_required",
        },
        "artifact_ids": [bundle_artifact_id],
        "created_at": plan["created_at"],
    }
    revisions: list[dict[str, Any]] = [base_revision]
    for trial in bundle["search_trials"]:
        trial_id = trial["trial_id"]
        revisions.append(
            {
                "entity_type": "DesignRevision",
                "schema_version": "1.0",
                "design_revision_id": search_revision_ids[trial_id],
                "device_id": device_id,
                "revision_label": trial_id,
                "parent_revision_id": base_revision_id,
                "change_summary": "Explicit grid-search parameter point",
                "geometry": {
                    "representation": "parametric",
                    "parameters": [
                        {
                            "name": item["name"],
                            "value": item["value"],
                            "unit": item["unit"],
                            "value_origin": "manual_entry",
                        }
                        for item in trial["job"]["parameters"]
                    ],
                },
                "materials": [],
                "topology": {
                    "source": "fixed_backend_recipe",
                    "backend_id": bundle["backend"]["backend_id"],
                },
                "artifact_ids": [bundle_artifact_id, *per_trial_artifact_ids[trial_id]],
                "created_at": trial["started_at"],
            }
        )
    study = {
        "entity_type": "Study",
        "schema_version": "1.0",
        "study_id": study_id,
        "device_id": device_id,
        "design_revision_ids": [
            base_revision_id,
            *[search_revision_ids[item["trial_id"]] for item in bundle["search_trials"]],
        ],
        "study_type": "optimization",
        "physics_domains": plan["physics_domains"],
        "objectives": [
            {
                **plan["objective"],
                "best_search_trial_id": best_trial["trial_id"],
                "independent_verification_runs": len(bundle["verification_trials"]),
            }
        ],
        "parameter_space": plan["parameters"],
        "run_ids": [*[run_ids[item["trial_id"]] for item in trials], processing_run_id],
        "created_at": plan["created_at"],
    }
    for record in (device, *revisions, study):
        _write_record(root, case_id, record)

    for trial in trials:
        trial_id = trial["trial_id"]
        inputs = [
            companion_artifact_ids[path]
            for path in trial["artifact_paths"]
            if artifact_record_by_path[path]["role"] == "raw_input"
            and artifact_record_by_path[path]["value_origin"] == "manual_entry"
        ]
        outputs = [identifier for identifier in per_trial_artifact_ids[trial_id] if identifier not in inputs]
        run: dict[str, Any] = {
            "entity_type": "Run",
            "schema_version": "1.0",
            "run_id": run_ids[trial_id],
            **(
                {"parent_run_id": run_ids[best_trial["trial_id"]]}
                if trial["trial_kind"] == "independent_verification"
                else {}
            ),
            "study_id": study_id,
            "design_revision_id": revision_for_trial[trial_id],
            "activity_type": "optimization_step",
            "status": trial["status"],
            "physics_domains": plan["physics_domains"],
            "started_at": trial["started_at"],
            "ended_at": trial["ended_at"],
            "input_artifact_ids": inputs,
            "output_artifact_ids": outputs,
            "provenance_id": provenance_ids[trial_id],
            "environment": {
                "platform": plan["execution"]["platform"],
                "compute": plan["execution"]["compute"],
            },
            "source_context": {
                "optimization": {
                    "optimization_id": plan["optimization_id"],
                    "strategy": plan["strategy"],
                    "trial_id": trial_id,
                    "trial_kind": trial["trial_kind"],
                    "backend": bundle["backend"],
                    "parameters": trial["job"]["parameters"],
                    "objective": plan["objective"],
                }
            },
        }
        if trial["status"] == "failed":
            if not outputs:
                raise ValueError(f"Failed trial has no failure evidence artifact: {trial_id}")
            run["failure"] = {
                "stage": "backend_evaluation",
                "error_code": "simulation_backend_failed",
                "message": trial.get("error", "backend reported failure"),
                "log_artifact_id": outputs[0],
                "recoverable": True,
            }
        _write_record(root, case_id, run)

    processing_run = {
        "entity_type": "Run",
        "schema_version": "1.0",
        "run_id": processing_run_id,
        "study_id": study_id,
        "design_revision_id": search_revision_ids[best_trial["trial_id"]],
        "activity_type": "data_processing",
        "status": "succeeded",
        "physics_domains": plan["physics_domains"],
        "started_at": processed,
        "ended_at": processed,
        "input_artifact_ids": [bundle_artifact_id, script_artifact_id, *companion_artifact_ids.values()],
        "output_artifact_ids": [h5_artifact_id, checks_artifact_id],
        "provenance_id": processing_prov_id,
        "environment": {
            "platform": plan["execution"]["platform"],
            "compute": plan["execution"]["compute"],
        },
        "source_context": {
            "processing": {
                "adapter": "optimization_trace_bundle",
                "adapter_version": ADAPTER_VERSION,
                "selection_rule": plan["objective"]["goal"],
                "verification_policy": "best search point rerun independently and excluded from selection",
            }
        },
    }
    _write_record(root, case_id, processing_run)

    for trial in trials:
        if trial["status"] != "succeeded":
            continue
        trial_id = trial["trial_id"]
        parameter_observable = {
            "entity_type": "Observable",
            "schema_version": "1.0",
            "observable_id": parameter_observable_ids[trial_id],
            "run_id": run_ids[trial_id],
            "observable_type": "table",
            "quantity": "optimization_parameters",
            "axes": [],
            "components": [item["name"] for item in trial["job"]["parameters"]],
            "complex_representation": "not_applicable",
            "data_ref": _h5_ref(root, h5_path, f"/trials/{trial_id}/parameters"),
            "artifact_id": h5_artifact_id,
            "coordinate_system_ref": None,
            "value_origin": "manual_entry",
            "provenance_id": provenance_ids[trial_id],
            "manual_entry_context": {
                "source": "versioned optimization plan",
                "plan_sha256": bundle["plan_sha256"],
            },
        }
        _write_record(root, case_id, parameter_observable)
        derived_from = [parameter_observable_ids[trial_id]]
        if backend_is_physical:
            parsed = parsed_by_trial[trial_id]
            raw_observable = {
                "entity_type": "Observable",
                "schema_version": "1.0",
                "observable_id": raw_observable_ids[trial_id],
                "run_id": run_ids[trial_id],
                "observable_type": "s_parameters",
                "quantity": "complex_s_parameters",
                "axes": [
                    {
                        "name": "frequency",
                        "unit": "Hz",
                        "data_ref": _h5_ref(root, h5_path, f"/trials/{trial_id}/frequency_hz"),
                    }
                ],
                "components": list(parsed.components),
                "complex_representation": "real_imaginary",
                "data_ref": _h5_ref(root, h5_path, f"/trials/{trial_id}/s_parameters"),
                "artifact_id": h5_artifact_id,
                "coordinate_system_ref": None,
                "value_origin": "raw_solver_output",
                "provenance_id": provenance_ids[trial_id],
            }
            _write_record(root, case_id, raw_observable)
            derived_from.append(raw_observable_ids[trial_id])
        objective_observable = {
            "entity_type": "Observable",
            "schema_version": "1.0",
            "observable_id": objective_observable_ids[trial_id],
            "run_id": run_ids[trial_id],
            "observable_type": "scalar",
            "quantity": plan["objective"]["quantity"],
            "axes": [],
            "components": ["value"],
            "complex_representation": "not_applicable",
            "data_ref": _h5_ref(root, h5_path, f"/trials/{trial_id}/objective"),
            "artifact_id": h5_artifact_id,
            "coordinate_system_ref": None,
            "value_origin": "calculated",
            "provenance_id": provenance_ids[trial_id],
            "derived_from_observable_ids": derived_from,
            "derivation": {
                "method": (
                    "recompute objective from the full Touchstone sweep"
                    if backend_is_physical
                    else "recompute the declared analytic contract fixture"
                ),
                "script_artifact_id": script_artifact_id,
            },
        }
        metric = {
            "entity_type": "Metric",
            "schema_version": "1.0",
            "metric_id": trial_metric_ids[trial_id],
            "run_id": run_ids[trial_id],
            "name": plan["objective"]["name"],
            "quantity": plan["objective"]["quantity"],
            "value": objective_values[trial_id],
            "unit": plan["objective"]["unit"],
            "value_origin": "calculated",
            "source_observable_ids": [objective_observable_ids[trial_id]],
            "extraction_method": "deterministic optimization objective recomputation",
            "calculation": {
                "goal": plan["objective"]["goal"],
                "script_artifact_id": script_artifact_id,
                "backend_is_physical_solver": backend_is_physical,
            },
            "provenance_id": provenance_ids[trial_id],
        }
        _write_record(root, case_id, objective_observable)
        _write_record(root, case_id, metric)

    all_objective_ids = list(objective_observable_ids.values())
    summary_observable = {
        "entity_type": "Observable",
        "schema_version": "1.0",
        "observable_id": summary_observable_id,
        "run_id": processing_run_id,
        "observable_type": "response",
        "quantity": plan["objective"]["quantity"],
        "axes": [],
        "components": ["best_search", "independent_verification_mean"],
        "complex_representation": "not_applicable",
        "data_ref": _h5_ref(root, h5_path, "/summary/objective"),
        "artifact_id": h5_artifact_id,
        "coordinate_system_ref": None,
        "value_origin": "calculated",
        "provenance_id": processing_prov_id,
        "derived_from_observable_ids": all_objective_ids,
        "derivation": {
            "method": "budgeted search selection followed by an independent best-point mean",
            "script_artifact_id": script_artifact_id,
        },
    }
    verification_mean = float(
        np.mean([objective_values[item["trial_id"]] for item in bundle["verification_trials"]])
    )
    summary_metrics = [
        {
            "entity_type": "Metric",
            "schema_version": "1.0",
            "metric_id": best_metric_id,
            "run_id": processing_run_id,
            "name": f"best search {plan['objective']['name']}",
            "quantity": plan["objective"]["quantity"],
            "value": objective_values[best_trial["trial_id"]],
            "unit": plan["objective"]["unit"],
            "value_origin": "calculated",
            "source_observable_ids": [summary_observable_id],
            "extraction_method": f"deterministic {plan['objective']['goal']} selection over search trials only",
            "calculation": {
                "best_search_trial_id": best_trial["trial_id"],
                "script_artifact_id": script_artifact_id,
            },
            "provenance_id": processing_prov_id,
        },
        {
            "entity_type": "Metric",
            "schema_version": "1.0",
            "metric_id": verified_metric_id,
            "run_id": processing_run_id,
            "name": f"independently verified {plan['objective']['name']}",
            "quantity": plan["objective"]["quantity"],
            "value": verification_mean,
            "unit": plan["objective"]["unit"],
            "value_origin": "calculated",
            "source_observable_ids": [summary_observable_id],
            "extraction_method": "mean of independent reruns at the selected best parameter point",
            "calculation": {
                "verification_trial_ids": [item["trial_id"] for item in bundle["verification_trials"]],
                "script_artifact_id": script_artifact_id,
            },
            "provenance_id": processing_prov_id,
        },
    ]
    _write_record(root, case_id, summary_observable)
    for metric in summary_metrics:
        _write_record(root, case_id, metric)

    validations = [
        {
            "entity_type": "Validation",
            "schema_version": "1.0",
            "validation_id": f"val_{case_id}_optimization_contract",
            "subject_refs": [
                _subject("Study", study_id),
                _subject("Run", processing_run_id),
                _subject("Metric", verified_metric_id),
            ],
            "validation_type": "schema_validation",
            "method": {
                "name": "optimization evidence contract and objective replay",
                "description": (
                    "Verify plan and job schemas, budget accounting, companion hashes, "
                    "best-point rerun, and deterministic objective recomputation."
                ),
                "script_artifact_id": script_artifact_id,
            },
            "threshold": {
                "name": "contract_error_count",
                "operator": "==",
                "value": 0,
                "unit": "count",
            },
            "result": {
                "status": "passed",
                "summary": (
                    "Optimization evidence is internally consistent. "
                    + (
                        "Physical claims remain limited to the preserved solver evidence."
                        if backend_is_physical
                        else "The backend is explicitly marked as a non-physical CI fixture."
                    )
                ),
                "measured_values": [
                    {
                        "contract_error_count": 0,
                        "search_trials": len(bundle["search_trials"]),
                        "verification_trials": len(bundle["verification_trials"]),
                        "backend_is_physical_solver": backend_is_physical,
                    }
                ],
            },
            "evidence_artifact_ids": [bundle_artifact_id, checks_artifact_id],
            "executed_at": processed,
            "provenance_id": processing_prov_id,
        }
    ]
    _write_json(case / "validation.json", {"schema_version": "1.0", "validations": validations})
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
    return OptimizationIngestionResult(
        repository=root,
        case_id=case_id,
        source_sha256=sha256_file(source_path),
        trial_count=len(trials),
        best_metric={
            "metric_id": best_metric_id,
            "value": objective_values[best_trial["trial_id"]],
            "unit": plan["objective"]["unit"],
        },
        validation=validation.to_dict(),
    )
