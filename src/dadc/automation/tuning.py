"""Budgeted deterministic grid search with an independent best-point rerun."""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..contracts import validate_contract
from ..integrity import sha256_file
from .backends import EvidenceFile, SimulationResult, create_backend

TUNER_VERSION = "1.0.0"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_sha256(value: Any) -> str:
    rendered = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _job(
    plan: dict[str, Any],
    trial_id: str,
    trial_kind: str,
    values: tuple[float, ...],
) -> dict[str, Any]:
    job = {
        "simulation_job_version": "1.0",
        "job_id": f"{plan['case_id']}_{trial_id}",
        "trial_kind": trial_kind,
        "parameters": [
            {"name": spec["name"], "value": value, "unit": spec["unit"]}
            for spec, value in zip(plan["parameters"], values, strict=True)
        ],
        "objective": dict(plan["objective"]),
        "backend": dict(plan["backend"]),
    }
    validate_contract(job, "simulation_job")
    return job


def _failure_result(job: dict[str, Any], workdir: Path, exc: Exception) -> SimulationResult:
    workdir.mkdir(parents=True, exist_ok=True)
    timestamp = _now()
    path = workdir / "orchestrator_failure.json"
    _write_json(
        path,
        {
            "simulation_result_version": "1.0",
            "job_id": job["job_id"],
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "recorded_at": timestamp,
        },
    )
    return SimulationResult(
        status="failed",
        started_at=timestamp,
        ended_at=timestamp,
        metric=None,
        evidence=(EvidenceFile(path, "report", "application/json", "calculated"),),
        error=f"{type(exc).__name__}: {exc}",
    )


def _trial_record(
    root: Path,
    trial_id: str,
    kind: str,
    job: dict[str, Any],
    result: SimulationResult,
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    artifact_paths: list[str] = []
    for evidence in result.evidence:
        path = evidence.path.resolve()
        relative = path.relative_to(root).as_posix()
        artifact_paths.append(relative)
        artifacts.append(
            {
                "relative_path": relative,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "role": evidence.role,
                "media_type": evidence.media_type,
                "value_origin": evidence.value_origin,
                "trial_id": trial_id,
            }
        )
    return {
        "trial_id": trial_id,
        "trial_kind": kind,
        "job": job,
        "status": result.status,
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "metric": result.metric,
        **({"error": result.error} if result.error else {}),
        "artifact_paths": artifact_paths,
    }


def _score(trial: dict[str, Any], objective: dict[str, Any]) -> float:
    value = float(trial["metric"]["value"])
    if objective["goal"] == "minimize":
        return value
    if objective["goal"] == "maximize":
        return -value
    return abs(value - float(objective["target"]))


def run_optimization(plan_path: str | Path, target: str | Path) -> dict[str, Any]:
    """Execute a bounded grid and write an ingestible, hash-pinned evidence bundle."""

    source = Path(plan_path).resolve()
    plan = json.loads(source.read_text(encoding="utf-8"))
    validate_contract(plan, "optimization_plan")
    names = [item["name"] for item in plan["parameters"]]
    if len(names) != len(set(names)):
        raise ValueError("Optimization parameter names must be unique")
    root = Path(target).resolve()
    if root.exists() and (root.is_file() or any(root.iterdir())):
        raise FileExistsError(f"Refusing to overwrite non-empty optimization output: {root}")
    root.mkdir(parents=True, exist_ok=True)
    plan_snapshot = root / "optimization_plan.json"
    _write_json(plan_snapshot, plan)
    backend = create_backend(plan["backend"])
    preflight = backend.preflight(plan)
    if not preflight.get("ready"):
        _write_json(root / "preflight.json", preflight)
        raise RuntimeError(f"Simulation backend preflight failed: {preflight}")

    started = _now()
    artifacts: list[dict[str, Any]] = []
    search_trials: list[dict[str, Any]] = []
    value_grid = itertools.product(*(item["values"] for item in plan["parameters"]))
    limit = int(plan["budget"]["max_search_trials"])
    for index, raw_values in enumerate(itertools.islice(value_grid, limit), start=1):
        trial_id = f"search_{index:03d}"
        values = tuple(float(item) for item in raw_values)
        job = _job(plan, trial_id, "search", values)
        workdir = root / "trials" / trial_id
        try:
            result = backend.evaluate(job, workdir)
        except Exception as exc:
            result = _failure_result(job, workdir, exc)
        search_trials.append(_trial_record(root, trial_id, "search", job, result, artifacts))

    succeeded = [item for item in search_trials if item["status"] == "succeeded"]
    if not succeeded:
        raise RuntimeError("Every search trial failed; evidence remains in the output directory")
    best = min(succeeded, key=lambda item: (_score(item, plan["objective"]), item["trial_id"]))
    best_values = tuple(float(item["value"]) for item in best["job"]["parameters"])

    verification_trials: list[dict[str, Any]] = []
    verification_count = int(plan["budget"]["independent_verification_runs"])
    for index in range(1, verification_count + 1):
        trial_id = f"verify_{index:03d}"
        job = _job(plan, trial_id, "independent_verification", best_values)
        workdir = root / "trials" / trial_id
        try:
            result = backend.evaluate(job, workdir)
        except Exception as exc:
            result = _failure_result(job, workdir, exc)
        verification_trials.append(
            _trial_record(root, trial_id, "independent_verification", job, result, artifacts)
        )
    if any(item["status"] != "succeeded" for item in verification_trials):
        raise RuntimeError("Independent best-point verification failed; evidence remains in output")

    backend_record = {
        "backend_id": backend.backend_id,
        "backend_version": backend.backend_version,
        "is_physical_solver": backend.is_physical_solver,
        "evidence_level": backend.evidence_level,
    }
    bundle = {
        "bundle_type": "dadc_optimization_trace",
        "optimization_bundle_version": "1.0",
        "created_at": started,
        "finished_at": _now(),
        "plan": plan,
        "plan_sha256": _canonical_sha256(plan),
        "backend": backend_record,
        "preflight": preflight,
        "search_trials": search_trials,
        "verification_trials": verification_trials,
        "best_search_trial_id": best["trial_id"],
        "budget": {
            "max_search_trials": limit,
            "search_trials_executed": len(search_trials),
            "independent_verification_runs": verification_count,
            "verification_runs_executed": len(verification_trials),
        },
        "artifacts": artifacts,
    }
    validate_contract(bundle, "optimization_bundle")
    bundle_path = root / "optimization_bundle.json"
    _write_json(bundle_path, bundle)
    return {
        "optimization_id": plan["optimization_id"],
        "bundle": str(bundle_path),
        "backend": backend_record,
        "best_search_trial_id": best["trial_id"],
        "best_metric": best["metric"],
        "search_trials": len(search_trials),
        "verification_trials": len(verification_trials),
    }
