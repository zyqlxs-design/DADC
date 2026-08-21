"""Build and optionally execute an evidence-grounded, bounded tuning plan."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import operator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..automation import run_optimization
from ..contracts import validate_contract
from ..knowledge import search_index
from ..repository import DADCRepository
from ..warehouse import WarehouseManager
from .providers import create_provider

AGENT_TUNER_VERSION = "1.0.0"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _canonical_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _read_request(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_contract(value, "agent_tuning_request")
    names = [item["name"] for item in value["parameter_policy"]]
    if len(names) != len(set(names)):
        raise ValueError("agent tuning parameter_policy names must be unique")
    return value


def _resolve_template(request_path: Path, request: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    candidate = Path(request["plan_template"])
    if not candidate.is_absolute():
        candidate = request_path.parent / candidate
    path = candidate.resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_contract(value, "optimization_plan")
    if value["device"]["device_class"] != request["device_class"]:
        raise ValueError("agent request device_class does not match the locked plan template")
    policies = {item["name"]: item for item in request["parameter_policy"]}
    template_parameters = {item["name"]: item for item in value["parameters"]}
    if set(policies) != set(template_parameters):
        raise ValueError("parameter_policy names must exactly match the locked plan template")
    for name, policy in policies.items():
        if policy["unit"] != template_parameters[name]["unit"]:
            raise ValueError(f"parameter unit mismatch for {name!r}")
        if int(policy["max_selected_values"]) > len(policy["allowed_values"]):
            raise ValueError(f"max_selected_values exceeds allowed_values for {name!r}")
    acceptance = request["acceptance"]
    objective = value["objective"]
    if acceptance["quantity"] != objective["quantity"] or acceptance["unit"] != objective["unit"]:
        raise ValueError("acceptance quantity/unit must match the locked objective")
    return path, value


def _warehouse_context(warehouse: str | Path | None, device_class: str) -> dict[str, Any]:
    if warehouse is None:
        return {"supplied": False, "repository_valid": None, "devices": [], "optimization_trials": []}
    repository = DADCRepository(warehouse)
    report = repository.validate()
    if not report.valid:
        raise ValueError("Agent refuses to read an invalid DADC warehouse")
    devices = [
        {
            "device_id": item["device_id"],
            "name": item["name"],
            "device_class": item["device_class"],
            "device_subtype": item["device_subtype"],
        }
        for item in repository.records("Device")
        if item["device_class"] == device_class
    ]
    matching_device_ids = {item["device_id"] for item in devices}
    revisions = {
        item["design_revision_id"]: item
        for item in repository.records("DesignRevision")
        if item["device_id"] in matching_device_ids
    }
    metrics_by_run: dict[str, list[dict[str, Any]]] = {}
    for metric in repository.records("Metric"):
        metrics_by_run.setdefault(metric["run_id"], []).append(metric)
    trials: list[dict[str, Any]] = []
    for run in repository.records("Run"):
        if run["design_revision_id"] not in revisions:
            continue
        optimization = run.get("source_context", {}).get("optimization")
        if not isinstance(optimization, dict):
            continue
        metrics = metrics_by_run.get(run["run_id"], [])
        trials.append(
            {
                "run_id": run["run_id"],
                "status": run["status"],
                "trial_kind": optimization.get("trial_kind"),
                "backend": optimization.get("backend"),
                "parameters": optimization.get("parameters", []),
                "metrics": [
                    {
                        "metric_id": metric["metric_id"],
                        "quantity": metric["quantity"],
                        "value": metric["value"],
                        "unit": metric["unit"],
                        "provenance_id": metric["provenance_id"],
                    }
                    for metric in metrics
                ],
            }
        )
    return {
        "supplied": True,
        "repository": str(repository.root),
        "repository_valid": True,
        "validation_counts": {
            "checked_records": report.checked_records,
            "checked_artifacts": report.checked_artifacts,
            "checked_data_refs": report.checked_data_refs,
        },
        "devices": devices,
        "optimization_trials": trials[-50:],
    }


def _context(
    request: dict[str, Any],
    corpus: str | Path,
    warehouse: str | Path | None,
) -> dict[str, Any]:
    evidence = search_index(
        corpus,
        request["knowledge_query"],
        top_k=int(request["knowledge_top_k"]),
        device_class=request["device_class"],
    )
    if not evidence:
        raise ValueError("No eligible knowledge evidence was found for the agent request")
    compact_evidence = []
    for item in evidence:
        compact_evidence.append(
            {
                "chunk_id": item["chunk_id"],
                "heading": item["heading"],
                "text": item["text"][:2000],
                "authority": item["authority"],
                "validation_status": item["validation_status"],
                "evidence": item["evidence"],
            }
        )
    return {
        "agent_context_version": "1.0",
        "request": request,
        "knowledge_evidence": compact_evidence,
        "dadc_history": _warehouse_context(warehouse, request["device_class"]),
    }


def _same_number(left: float, right: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=1.0e-12, abs_tol=1.0e-12)


def _validate_selection(
    selection: dict[str, Any],
    request: dict[str, Any],
    context: dict[str, Any],
    template: dict[str, Any],
) -> dict[str, list[float]]:
    validate_contract(selection, "agent_parameter_selection")
    policies = {item["name"]: item for item in request["parameter_policy"]}
    selected_items = selection["selected_values"]
    selected_names = [item["name"] for item in selected_items]
    if len(selected_names) != len(set(selected_names)) or set(selected_names) != set(policies):
        raise ValueError("LLM selection must contain each allow-listed parameter exactly once")
    selected: dict[str, list[float]] = {}
    grid_size = 1
    for item in selected_items:
        name = item["name"]
        policy = policies[name]
        values = [float(value) for value in item["values"]]
        if len(values) > int(policy["max_selected_values"]):
            raise ValueError(f"LLM selected too many values for {name!r}")
        if any(
            not any(_same_number(value, allowed) for allowed in policy["allowed_values"])
            for value in values
        ):
            raise ValueError(f"LLM selected a value outside allowed_values for {name!r}")
        selected[name] = values
        grid_size *= len(values)
    if grid_size > int(template["budget"]["max_search_trials"]):
        raise ValueError(
            f"LLM grid has {grid_size} points, exceeding max_search_trials="
            f"{template['budget']['max_search_trials']}"
        )
    allowed_chunks = {item["chunk_id"] for item in context["knowledge_evidence"]}
    if not selection["knowledge_chunk_ids"]:
        raise ValueError("LLM selection must cite at least one supplied knowledge chunk")
    if not set(selection["knowledge_chunk_ids"]).issubset(allowed_chunks):
        raise ValueError("LLM cited a knowledge chunk that was not supplied")
    return selected


def _build_plan(
    request: dict[str, Any],
    template: dict[str, Any],
    selected: dict[str, list[float]],
    selection: dict[str, Any],
    exchange: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    plan = copy.deepcopy(template)
    plan["optimization_id"] = request["request_id"]
    plan["case_id"] = request["request_id"]
    plan["created_at"] = _now()
    knowledge_by_id = {item["chunk_id"]: item for item in context["knowledge_evidence"]}
    cited_knowledge = []
    for chunk_id in selection["knowledge_chunk_ids"]:
        item = knowledge_by_id[chunk_id]
        cited_knowledge.append(
            {
                "chunk_id": chunk_id,
                "source_id": item["evidence"]["source_id"],
                "source_url": item["evidence"]["source_url"],
                "locator": item["evidence"]["locator"],
                "content_sha256": item["evidence"]["content_sha256"],
                "authority": item["authority"],
                "validation_status": item["validation_status"],
            }
        )
    history_run_ids = [
        item["run_id"] for item in context["dadc_history"]["optimization_trials"]
    ]
    plan["device"]["attributes"]["agent_planning"] = {
        "agent_tuner_version": AGENT_TUNER_VERSION,
        "request_id": request["request_id"],
        "provider_id": exchange["provider_id"],
        "model": exchange["model"],
        "selection_sha256": _canonical_hash(selection),
        "knowledge_evidence": cited_knowledge,
        "dadc_history_run_ids": history_run_ids,
        "constraint_policy": "allow_list_and_cartesian_budget_checked_before_execution",
    }
    for parameter in plan["parameters"]:
        parameter["values"] = selected[parameter["name"]]
    validate_contract(plan, "optimization_plan")
    return plan


def prepare_agent_plan(
    request_path: str | Path,
    corpus: str | Path,
    target: str | Path,
    *,
    provider: str = "deterministic_fixture",
    model: str | None = None,
    warehouse: str | Path | None = None,
) -> dict[str, Any]:
    """Retrieve evidence, query one provider, and emit a validated locked plan."""

    source = Path(request_path).resolve()
    request = _read_request(source)
    template_path, template = _resolve_template(source, request)
    root = Path(target).resolve()
    if root.exists() and (root.is_file() or any(root.iterdir())):
        raise FileExistsError(f"Refusing to overwrite non-empty agent output: {root}")
    root.mkdir(parents=True, exist_ok=True)
    context = _context(request, corpus, warehouse)
    selection_provider = create_provider(provider, model=model)
    selection, exchange = selection_provider.select(context)
    selected = _validate_selection(selection, request, context, template)
    plan = _build_plan(request, template, selected, selection, exchange, context)
    request_snapshot = root / "agent_tuning_request.json"
    context_path = root / "agent_context.json"
    proposal_path = root / "parameter_proposal.json"
    plan_path = root / "optimization_plan.json"
    exchange_path = root / "planner_exchange.json"
    _write_json(request_snapshot, request)
    _write_json(context_path, context)
    _write_json(
        proposal_path,
        {
            "proposal_record_version": "1.0",
            "created_at": _now(),
            "provider_id": exchange["provider_id"],
            "model": exchange["model"],
            "selection": selection,
            "constraints_checked": True,
            "request_sha256": _canonical_hash(request),
            "context_sha256": _canonical_hash(context),
            "locked_template_sha256": _canonical_hash(template),
        },
    )
    _write_json(plan_path, plan)
    _write_json(exchange_path, exchange)
    return {
        "status": "planned",
        "agent_tuner_version": AGENT_TUNER_VERSION,
        "request_id": request["request_id"],
        "provider_id": exchange["provider_id"],
        "model": exchange["model"],
        "knowledge_chunks_supplied": len(context["knowledge_evidence"]),
        "dadc_history_trials_supplied": len(context["dadc_history"]["optimization_trials"]),
        "locked_plan_template": str(template_path),
        "selected_values": selected,
        "output": str(root),
        "plan": str(plan_path),
        "proposal": str(proposal_path),
    }


_OPERATORS = {
    "<=": operator.le,
    "<": operator.lt,
    ">=": operator.ge,
    ">": operator.gt,
    "==": lambda left, right: math.isclose(left, right, rel_tol=1.0e-12, abs_tol=1.0e-12),
}


def _evaluate_acceptance(request: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    rule = request["acceptance"]
    verification = bundle["verification_trials"]
    successful = [item for item in verification if item["status"] == "succeeded"]
    values = [float(item["metric"]["value"]) for item in successful]
    verified_value = float(sum(values) / len(values)) if values else None
    checks = {
        "quantity_matches": bundle["plan"]["objective"]["quantity"] == rule["quantity"],
        "unit_matches": bundle["plan"]["objective"]["unit"] == rule["unit"],
        "physical_solver": (
            bool(bundle["backend"]["is_physical_solver"])
            if rule["require_physical_solver"]
            else True
        ),
        "independent_verification": (
            bool(successful) and len(successful) == len(verification)
            if rule["require_independent_verification"]
            else True
        ),
        "threshold": (
            False
            if verified_value is None
            else bool(_OPERATORS[rule["operator"]](verified_value, float(rule["value"])))
        ),
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "verified_value": verified_value,
        "rule": rule,
    }


def run_agent_tuning(
    request_path: str | Path,
    corpus: str | Path,
    target: str | Path,
    *,
    provider: str = "deterministic_fixture",
    model: str | None = None,
    warehouse: str | Path | None = None,
    approve_execution: bool = False,
) -> dict[str, Any]:
    """Plan, execute through an allow-listed backend, verify, and optionally ingest."""

    if not approve_execution:
        raise ValueError("Execution requires approve_execution=True")
    root = Path(target).resolve()
    planning = prepare_agent_plan(
        request_path,
        corpus,
        root / "planning",
        provider=provider,
        model=model,
        warehouse=warehouse,
    )
    planned_value = json.loads(Path(planning["plan"]).read_text(encoding="utf-8"))
    if planned_value["backend"]["type"] != "analytic_fixture":
        citations = planned_value["device"]["attributes"]["agent_planning"][
            "knowledge_evidence"
        ]
        test_only = [
            item["chunk_id"]
            for item in citations
            if item["authority"] == "test_fixture"
            or item["validation_status"] == "test_only"
        ]
        if test_only:
            raise ValueError(
                "Physical execution rejects test-only knowledge citations: " + ", ".join(test_only)
            )
    result = run_optimization(planning["plan"], root / "optimization")
    bundle_path = Path(result["bundle"])
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    request = _read_request(Path(request_path).resolve())
    acceptance = _evaluate_acceptance(request, bundle)
    ingestion = None
    if warehouse is not None:
        ingestion = WarehouseManager(warehouse).ingest(bundle_path, {}).to_dict()
    final = {
        "agent_tuning_result_version": "1.0",
        "created_at": _now(),
        "status": "accepted" if acceptance["status"] == "passed" else "target_not_met",
        "planning": planning,
        "optimization": result,
        "acceptance": acceptance,
        "ingestion": ingestion,
        "claim_boundary": (
            "Acceptance is computed from independent solver verification. "
            "An LLM parameter proposal is not itself scientific validation."
        ),
    }
    result_path = root / "agent_tuning_result.json"
    _write_json(result_path, final)
    return {**final, "result": str(result_path)}
