"""Typed, allow-listed simulation backends. No shell or generated code execution."""

from __future__ import annotations

import json
import math
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from ..contracts import validate_contract
from ..ingestion.touchstone import parse_touchstone


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class EvidenceFile:
    path: Path
    role: str
    media_type: str
    value_origin: str


@dataclass(frozen=True)
class SimulationResult:
    status: str
    started_at: str
    ended_at: str
    metric: dict[str, Any] | None
    evidence: tuple[EvidenceFile, ...]
    error: str | None = None


class SimulationBackend(Protocol):
    backend_id: str
    backend_version: str
    is_physical_solver: bool
    evidence_level: str

    def preflight(self, plan: dict[str, Any]) -> dict[str, Any]: ...

    def evaluate(self, job: dict[str, Any], workdir: Path) -> SimulationResult: ...


class AnalyticFixtureBackend:
    """Deterministic paraboloid used only to prove the orchestration contract in CI."""

    backend_id = "analytic_fixture"
    backend_version = "1.0.0"
    is_physical_solver = False
    evidence_level = "ci_contract_only_non_physical"

    def __init__(self, options: dict[str, Any]):
        self.options = options

    def preflight(self, plan: dict[str, Any]) -> dict[str, Any]:
        names = [item["name"] for item in plan["parameters"]]
        centers = self.options.get("centers", {})
        weights = self.options.get("weights", {})
        missing = [name for name in names if name not in centers]
        fail_when = self.options.get("fail_when", [])
        failure_names = {
            key for condition in fail_when if isinstance(condition, dict) for key in condition
        }
        unknown = sorted((set(centers) | set(weights) | failure_names) - set(names))
        ready = not missing and not unknown
        return {
            "preflight_version": "1.0",
            "ready": ready,
            "backend_id": self.backend_id,
            "is_physical_solver": False,
            "parameter_names": names,
            "missing_centers": missing,
            "unknown_backend_parameters": unknown,
            "statement": "Contract fixture only; results are not physical simulation evidence.",
        }

    def evaluate(self, job: dict[str, Any], workdir: Path) -> SimulationResult:
        validate_contract(job, "simulation_job")
        workdir.mkdir(parents=True, exist_ok=False)
        started = _now()
        request_path = workdir / "simulation_request.json"
        _write_json(request_path, job)
        parameter_values = {
            item["name"]: float(item["value"]) for item in job["parameters"]
        }
        for condition in self.options.get("fail_when", []):
            if all(
                name in parameter_values
                and math.isclose(parameter_values[name], float(expected), rel_tol=0.0, abs_tol=0.0)
                for name, expected in condition.items()
            ):
                ended = _now()
                error = "declared analytic fixture failure for failed-run lineage testing"
                result_path = workdir / "simulation_result.json"
                _write_json(
                    result_path,
                    {
                        "simulation_result_version": "1.0",
                        "job_id": job["job_id"],
                        "status": "failed",
                        "backend": {
                            "backend_id": self.backend_id,
                            "backend_version": self.backend_version,
                            "is_physical_solver": False,
                        },
                        "started_at": started,
                        "ended_at": ended,
                        "error": error,
                        "warning": "Intentional CI failure; this is not physical simulation evidence.",
                    },
                )
                return SimulationResult(
                    status="failed",
                    started_at=started,
                    ended_at=ended,
                    metric=None,
                    error=error,
                    evidence=(
                        EvidenceFile(request_path, "raw_input", "application/json", "manual_entry"),
                        EvidenceFile(result_path, "report", "application/json", "calculated"),
                    ),
                )
        centers = self.options["centers"]
        weights = self.options.get("weights", {})
        value = float(self.options.get("offset", 0.0))
        terms: list[dict[str, float | str]] = []
        for parameter in job["parameters"]:
            name = parameter["name"]
            delta = float(parameter["value"]) - float(centers[name])
            weight = float(weights.get(name, 1.0))
            term = weight * delta * delta
            value += term
            terms.append({"parameter": name, "delta": delta, "weight": weight, "term": term})
        ended = _now()
        metric = {
            "name": job["objective"]["name"],
            "quantity": job["objective"]["quantity"],
            "value": value,
            "unit": job["objective"]["unit"],
        }
        result_path = workdir / "simulation_result.json"
        _write_json(
            result_path,
            {
                "simulation_result_version": "1.0",
                "job_id": job["job_id"],
                "status": "succeeded",
                "backend": {
                    "backend_id": self.backend_id,
                    "backend_version": self.backend_version,
                    "is_physical_solver": False,
                },
                "started_at": started,
                "ended_at": ended,
                "metric": metric,
                "calculation": {"kind": "paraboloid_contract_fixture", "terms": terms},
                "warning": "This result validates orchestration only and is not HFSS evidence.",
            },
        )
        return SimulationResult(
            status="succeeded",
            started_at=started,
            ended_at=ended,
            metric=metric,
            evidence=(
                EvidenceFile(request_path, "raw_input", "application/json", "manual_entry"),
                EvidenceFile(result_path, "report", "application/json", "calculated"),
            ),
        )


class PyAEDTPatchBackend:
    """Concrete local AEDT/PyAEDT runner using the repository's fixed patch recipe."""

    backend_id = "pyaedt_patch"
    backend_version = "1.0.0"
    is_physical_solver = True
    evidence_level = "local_aedt_solver_with_touchstone_and_native_project"
    _ALLOWED_PARAMETERS = {"patch_length_mm", "patch_width_mm", "probe_relative_x_offset"}

    def __init__(self, options: dict[str, Any]):
        self.options = options

    @staticmethod
    def _generator_path(options: dict[str, Any]) -> Path:
        configured = options.get("generator_script")
        if configured:
            return Path(str(configured)).resolve()
        return Path(__file__).resolve().parents[3] / "scripts" / "generate_patch_antenna.py"

    def preflight(self, plan: dict[str, Any]) -> dict[str, Any]:
        names = {item["name"] for item in plan["parameters"]}
        unknown = sorted(names - self._ALLOWED_PARAMETERS)
        generator = self._generator_path(self.options)
        missing_options = [
            key for key in ("aedt_version", "source_timezone") if not self.options.get(key)
        ]
        windows = os.name == "nt"
        ready = not unknown and generator.is_file() and not missing_options and windows
        return {
            "preflight_version": "1.0",
            "ready": ready,
            "backend_id": self.backend_id,
            "is_physical_solver": True,
            "platform": platform.platform(),
            "windows_required": True,
            "generator_script": str(generator),
            "generator_exists": generator.is_file(),
            "unknown_parameters": unknown,
            "missing_backend_options": missing_options,
            "next_action": (
                "run on a Windows host with AEDT/PyAEDT installed"
                if not windows
                else "correct missing options or unsupported parameters"
            ),
        }

    def evaluate(self, job: dict[str, Any], workdir: Path) -> SimulationResult:
        validate_contract(job, "simulation_job")
        names = {item["name"] for item in job["parameters"]}
        unknown = names - self._ALLOWED_PARAMETERS
        if unknown:
            raise ValueError(f"PyAEDT patch backend rejected parameters: {sorted(unknown)}")
        workdir.mkdir(parents=True, exist_ok=False)
        started = _now()
        request_path = workdir / "simulation_request.json"
        _write_json(request_path, job)
        values = {item["name"]: item["value"] for item in job["parameters"]}
        command = [
            str(self.options.get("python_executable", sys.executable)),
            str(self._generator_path(self.options)),
            "--output-dir",
            str(workdir / "solver_output"),
            "--aedt-version",
            str(self.options["aedt_version"]),
            "--source-timezone",
            str(self.options["source_timezone"]),
            "--operator-id",
            str(self.options.get("operator_id", "local_user")),
            "--case-id",
            job["job_id"],
            "--patch-length-mm",
            str(values.get("patch_length_mm", 9.57)),
            "--patch-width-mm",
            str(values.get("patch_width_mm", 9.25)),
            "--probe-relative-x-offset",
            str(values.get("probe_relative_x_offset", 0.485)),
            "--cores",
            str(int(self.options.get("cores", 2))),
        ]
        if self.options.get("non_graphical", True):
            command.append("--non-graphical")
        timeout = int(self.options.get("timeout_seconds", 7200))
        completed = subprocess.run(
            command,
            cwd=str(self._generator_path(self.options).parent),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        stdout_path = workdir / "runner_stdout.log"
        stderr_path = workdir / "runner_stderr.log"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        output = workdir / "solver_output"
        touchstones = sorted(output.glob("*.s1p"))
        ended = _now()
        if completed.returncode or len(touchstones) != 1:
            error = (
                f"PyAEDT generator exit={completed.returncode}; "
                f"touchstone_count={len(touchstones)}"
            )
            return SimulationResult(
                status="failed",
                started_at=started,
                ended_at=ended,
                metric=None,
                error=error,
                evidence=(
                    EvidenceFile(request_path, "raw_input", "application/json", "manual_entry"),
                    EvidenceFile(stdout_path, "solver_log", "text/plain", "raw_solver_output"),
                    EvidenceFile(stderr_path, "solver_log", "text/plain", "raw_solver_output"),
                ),
            )

        parsed = parse_touchstone(touchstones[0])
        s11_db = 20.0 * np.log10(np.maximum(np.abs(parsed.values[:, 0, 0]), 1.0e-300))
        minimum_index = int(np.argmin(s11_db))
        resonance_hz = float(parsed.frequencies_hz[minimum_index])
        objective = job["objective"]
        if objective["quantity"] == "target_frequency_error_hz":
            target = float(objective.get("target", self.options.get("target_frequency_hz", 1.0e10)))
            metric_value = abs(resonance_hz - target)
        elif objective["quantity"] == "resonance_frequency_hz":
            metric_value = resonance_hz
        elif objective["quantity"] == "minimum_return_loss_db":
            metric_value = float(s11_db[minimum_index])
        else:
            raise ValueError(f"Unsupported PyAEDT patch objective: {objective['quantity']!r}")
        metric = {
            "name": objective["name"],
            "quantity": objective["quantity"],
            "value": metric_value,
            "unit": objective["unit"],
        }
        result_path = workdir / "simulation_result.json"
        _write_json(
            result_path,
            {
                "simulation_result_version": "1.0",
                "job_id": job["job_id"],
                "status": "succeeded",
                "backend": {"backend_id": self.backend_id, "backend_version": self.backend_version},
                "started_at": started,
                "ended_at": ended,
                "metric": metric,
                "derived_values": {
                    "resonance_frequency_hz": resonance_hz,
                    "minimum_return_loss_db": float(s11_db[minimum_index]),
                    "full_sweep_point_count": int(parsed.frequencies_hz.size),
                },
                "command_argv": command,
            },
        )
        evidence: list[EvidenceFile] = [
            EvidenceFile(request_path, "raw_input", "application/json", "manual_entry"),
            EvidenceFile(result_path, "report", "application/json", "calculated"),
            EvidenceFile(stdout_path, "solver_log", "text/plain", "raw_solver_output"),
            EvidenceFile(stderr_path, "solver_log", "text/plain", "raw_solver_output"),
            EvidenceFile(touchstones[0], "raw_input", "application/vnd.touchstone", "raw_solver_output"),
        ]
        role_by_suffix = {
            ".aedt": ("native_project", "application/octet-stream", "raw_solver_output"),
            ".conv": ("validation_evidence", "text/plain", "raw_solver_output"),
            ".mstat": ("validation_evidence", "text/plain", "raw_solver_output"),
            ".prof": ("solver_log", "text/plain", "raw_solver_output"),
        }
        for path in sorted(output.iterdir()):
            if path == touchstones[0] or not path.is_file() or path.suffix not in role_by_suffix:
                continue
            role, media_type, origin = role_by_suffix[path.suffix]
            evidence.append(EvidenceFile(path, role, media_type, origin))
        return SimulationResult(
            status="succeeded",
            started_at=started,
            ended_at=ended,
            metric=metric,
            evidence=tuple(evidence),
        )


def create_backend(configuration: dict[str, Any]) -> SimulationBackend:
    backend_type = configuration["type"]
    options = dict(configuration.get("options", {}))
    if backend_type == "analytic_fixture":
        return AnalyticFixtureBackend(options)
    if backend_type == "pyaedt_patch":
        return PyAEDTPatchBackend(options)
    raise ValueError(f"Unknown simulation backend: {backend_type!r}")
