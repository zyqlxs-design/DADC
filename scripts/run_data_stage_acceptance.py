"""Produce an objective data-stage acceptance report without subjective scoring."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.automation import run_optimization  # noqa: E402
from dadc.contracts import validate_contract  # noqa: E402
from dadc.ingestion.registry import AdapterRegistry  # noqa: E402
from dadc.integrity import sha256_file  # noqa: E402
from dadc.knowledge import build_index, collect_corpus, search_index  # noqa: E402
from dadc.repository import DADCRepository  # noqa: E402
from dadc.warehouse import WarehouseManager, initialize_data_root  # noqa: E402


EXPECTED_CORE_SCHEMAS = {
    "artifact.schema.json",
    "design_revision.schema.json",
    "device.schema.json",
    "metric.schema.json",
    "observable.schema.json",
    "provenance.schema.json",
    "run.schema.json",
    "study.schema.json",
    "validation.schema.json",
}
EXPECTED_DEVICE_PROFILES = {
    "antenna.schema.json",
    "generic_component.schema.json",
    "inductor.schema.json",
    "multiphysics_component.schema.json",
    "power_resistor.schema.json",
    "rf_filter.schema.json",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _check(check_id: str, passed: bool, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": "passed" if passed else "failed",
        "evidence": evidence,
    }


def _source_ids(results: list[dict[str, Any]]) -> set[str]:
    return {str(item["evidence"]["source_id"]) for item in results}


def _physical_bundle_checks(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bundle = json.loads(path.read_text(encoding="utf-8"))
    validate_contract(bundle, "optimization_bundle")
    root = path.parent
    artifact_failures: list[dict[str, Any]] = []
    roles: set[str] = set()
    media_types: set[str] = set()
    for artifact in bundle["artifacts"]:
        artifact_path = root / artifact["relative_path"]
        roles.add(str(artifact["role"]))
        media_types.add(str(artifact["media_type"]))
        if not artifact_path.is_file():
            artifact_failures.append(
                {"relative_path": artifact["relative_path"], "reason": "missing"}
            )
            continue
        if artifact_path.stat().st_size != int(artifact["size_bytes"]):
            artifact_failures.append(
                {"relative_path": artifact["relative_path"], "reason": "size_mismatch"}
            )
            continue
        if sha256_file(artifact_path) != artifact["sha256"]:
            artifact_failures.append(
                {"relative_path": artifact["relative_path"], "reason": "sha256_mismatch"}
            )
    search_trials = bundle["search_trials"]
    verification_trials = bundle["verification_trials"]
    checks = [
        _check(
            "physical_backend_declared",
            bundle["backend"]["is_physical_solver"] is True,
            {"backend": bundle["backend"]},
        ),
        _check(
            "physical_trials_completed",
            bool(search_trials)
            and bool(verification_trials)
            and all(item["status"] == "succeeded" for item in search_trials + verification_trials),
            {
                "search_trials": len(search_trials),
                "verification_trials": len(verification_trials),
                "statuses": {
                    item["trial_id"]: item["status"]
                    for item in search_trials + verification_trials
                },
            },
        ),
        _check(
            "physical_artifact_integrity",
            not artifact_failures,
            {
                "artifact_count": len(bundle["artifacts"]),
                "failures": artifact_failures,
            },
        ),
        _check(
            "physical_native_and_touchstone_evidence",
            "native_project" in roles and "application/vnd.touchstone" in media_types,
            {"artifact_roles": sorted(roles), "media_types": sorted(media_types)},
        ),
    ]
    summary = {
        "bundle": str(path),
        "case_id": bundle["plan"]["case_id"],
        "best_search_trial_id": bundle["best_search_trial_id"],
        "search_metrics": {
            item["trial_id"]: item["metric"] for item in search_trials if item.get("metric")
        },
        "verification_metrics": {
            item["trial_id"]: item["metric"]
            for item in verification_trials
            if item.get("metric")
        },
    }
    return checks, summary


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# DADC data-stage acceptance report",
        "",
        f"- Executed at: `{report['executed_at']}`",
        f"- Overall status: `{report['status']}`",
        "- Evaluation policy: objective pass/fail checks only; no subjective score is used.",
        "",
        "## Accepted checks",
        "",
        "| Check | Status | Objective evidence |",
        "|---|---|---|",
    ]
    for item in report["checks"]:
        evidence = json.dumps(item["evidence"], ensure_ascii=False, sort_keys=True)
        lines.append(f"| `{item['check_id']}` | `{item['status']}` | `{evidence}` |")
    lines.extend(["", "## Checks not executed", ""])
    if report["not_executed"]:
        for item in report["not_executed"]:
            lines.append(f"- `{item['check_id']}`: {item['reason']}")
    else:
        lines.append("- None.")
    lines.extend(["", "## Remaining work", ""])
    for item in report["remaining_work"]:
        lines.append(f"- `{item['work_id']}` ({item['stage']}): {item['description']}")
    lines.extend(
        [
            "",
            "## Architecture decision",
            "",
            report["architecture_decision"],
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--physical-optimization-bundle")
    parser.add_argument("--physical-warehouse")
    args = parser.parse_args()

    output = Path(args.output_dir).resolve()
    if output.exists() and (output.is_file() or any(output.iterdir())):
        raise FileExistsError(f"Refusing to overwrite non-empty acceptance output: {output}")
    output.mkdir(parents=True, exist_ok=True)

    checks: list[dict[str, Any]] = []
    not_executed: list[dict[str, str]] = []

    schema_root = REPOSITORY_ROOT / "schemas" / "v1.0"
    core_found = {
        path.name
        for path in schema_root.glob("*.schema.json")
        if path.name in EXPECTED_CORE_SCHEMAS
    }
    checks.append(
        _check(
            "frozen_core_entity_schemas_present",
            core_found == EXPECTED_CORE_SCHEMAS,
            {"expected": sorted(EXPECTED_CORE_SCHEMAS), "found": sorted(core_found)},
        )
    )
    profile_found = {
        path.name for path in (schema_root / "device_profiles").glob("*.schema.json")
    }
    checks.append(
        _check(
            "device_profiles_present",
            EXPECTED_DEVICE_PROFILES.issubset(profile_found),
            {"required": sorted(EXPECTED_DEVICE_PROFILES), "found": sorted(profile_found)},
        )
    )

    corpus = output / "knowledge_corpus"
    manifest = REPOSITORY_ROOT / "examples" / "knowledge" / "device_partition_fixture_sources.json"
    corpus_result = collect_corpus(manifest, corpus)
    first_index = build_index(corpus, dimensions=128)
    first_vectors = (corpus / "index" / "embeddings.npy").read_bytes()
    second_index = build_index(corpus, dimensions=128)
    second_vectors = (corpus / "index" / "embeddings.npy").read_bytes()
    documents = _jsonl(corpus / "documents.jsonl")
    chunks = _jsonl(corpus / "chunks.jsonl")
    raw_failures: list[dict[str, str]] = []
    for document in documents:
        raw_path = corpus / document["raw_artifact"]["relative_path"]
        if not raw_path.is_file():
            raw_failures.append({"source_id": document["source_id"], "reason": "missing"})
        elif raw_path.stat().st_size != document["raw_artifact"]["size_bytes"]:
            raw_failures.append({"source_id": document["source_id"], "reason": "size_mismatch"})
        elif sha256_file(raw_path) != document["raw_artifact"]["sha256"]:
            raw_failures.append({"source_id": document["source_id"], "reason": "sha256_mismatch"})
    checks.extend(
        [
            _check(
                "knowledge_manifest_v11_and_content_counts",
                corpus_result["knowledge_manifest_version"] == "1.1"
                and corpus_result["document_count"] == len(documents) == 4
                and corpus_result["chunk_count"] == len(chunks)
                and len(chunks) > 0,
                {
                    "manifest_version": corpus_result["knowledge_manifest_version"],
                    "documents": len(documents),
                    "chunks": len(chunks),
                },
            ),
            _check(
                "knowledge_raw_artifact_integrity",
                not raw_failures,
                {"checked_documents": len(documents), "failures": raw_failures},
            ),
            _check(
                "knowledge_index_rebuildable",
                first_vectors == second_vectors
                and first_index["chunks_sha256"] == second_index["chunks_sha256"]
                and first_index["authoritative"] is False,
                {
                    "record_count": first_index["record_count"],
                    "dimensions": first_index["dimensions"],
                    "authoritative": first_index["authoritative"],
                    "byte_identical_rebuild": first_vectors == second_vectors,
                },
            ),
        ]
    )

    antenna_sources = _source_ids(
        search_index(
            corpus,
            "patch geometry probe excitation frequency sweep",
            top_k=100,
            device_class="antenna",
        )
    )
    filter_sources = _source_ids(
        search_index(
            corpus,
            "two port passband Touchstone",
            top_k=100,
            device_class="rf_filter",
        )
    )
    inductor_sources = _source_ids(
        search_index(
            corpus,
            "inductance quality factor self resonance",
            top_k=100,
            device_class="inductor",
        )
    )
    partition_ok = (
        antenna_sources == {"pyaedt_shared_api_fixture", "antenna_workflow_fixture"}
        and filter_sources == {"pyaedt_shared_api_fixture", "rf_filter_workflow_fixture"}
        and inductor_sources == {"pyaedt_shared_api_fixture", "inductor_workflow_fixture"}
    )
    checks.append(
        _check(
            "shared_plus_device_partition_retrieval",
            partition_ok,
            {
                "antenna_sources": sorted(antenna_sources),
                "rf_filter_sources": sorted(filter_sources),
                "inductor_sources": sorted(inductor_sources),
            },
        )
    )

    optimization = run_optimization(
        REPOSITORY_ROOT / "examples" / "automation" / "analytic_fixture_plan.json",
        output / "optimization_fixture",
    )
    data_paths = initialize_data_root(output / "data_root")
    ingestion = WarehouseManager(data_paths["warehouse"]).ingest(optimization["bundle"], {})
    repository = DADCRepository(data_paths["warehouse"])
    validation = repository.validate()
    metric_id = "metric_mvp_optimization_001_verified_objective"
    trace = repository.trace_metric(metric_id) if validation.valid else {"artifacts": [], "provenance": []}
    runs = repository.records("Run") if validation.valid else []
    checks.extend(
        [
            _check(
                "optimization_bundle_ingested",
                ingestion.status == "ingested",
                ingestion.to_dict(),
            ),
            _check(
                "warehouse_schema_and_integrity_validation",
                validation.valid,
                validation.to_dict(),
            ),
            _check(
                "failed_and_verified_runs_preserved",
                any(item["status"] == "failed" for item in runs)
                and any(item["status"] == "succeeded" for item in runs),
                {
                    "run_count": len(runs),
                    "failed_run_count": sum(item["status"] == "failed" for item in runs),
                    "succeeded_run_count": sum(item["status"] == "succeeded" for item in runs),
                },
            ),
            _check(
                "verified_metric_trace_reaches_artifacts_and_provenance",
                bool(trace["artifacts"]) and bool(trace["provenance"]),
                {
                    "metric_id": metric_id,
                    "artifact_count": len(trace["artifacts"]),
                    "provenance_count": len(trace["provenance"]),
                },
            ),
        ]
    )
    adapter_ids = {item["adapter_id"] for item in AdapterRegistry().catalog()}
    required_adapters = {
        "optimization_trace_bundle",
        "touchstone_antenna",
        "touchstone_rf_filter",
        "touchstone_inductor",
        "tabular_experiment_csv",
        "joule_thermal_field_bundle",
    }
    checks.append(
        _check(
            "required_ingestion_adapters_registered",
            required_adapters.issubset(adapter_ids),
            {"required": sorted(required_adapters), "registered": sorted(adapter_ids)},
        )
    )

    physical_summary: dict[str, Any] | None = None
    if args.physical_optimization_bundle:
        physical_path = Path(args.physical_optimization_bundle).resolve()
        physical_checks, physical_summary = _physical_bundle_checks(physical_path)
        checks.extend(physical_checks)
        if args.physical_warehouse:
            physical_repository = DADCRepository(Path(args.physical_warehouse).resolve())
            physical_validation = physical_repository.validate()
            physical_metric_id = (
                f"metric_{physical_summary['case_id']}_verified_objective"
            )
            physical_trace = (
                physical_repository.trace_metric(physical_metric_id)
                if physical_validation.valid
                else {"artifacts": [], "provenance": []}
            )
            checks.extend(
                [
                    _check(
                        "physical_warehouse_validation",
                        physical_validation.valid,
                        physical_validation.to_dict(),
                    ),
                    _check(
                        "physical_verified_metric_trace",
                        bool(physical_trace["artifacts"])
                        and bool(physical_trace["provenance"]),
                        {
                            "metric_id": physical_metric_id,
                            "artifact_count": len(physical_trace["artifacts"]),
                            "provenance_count": len(physical_trace["provenance"]),
                        },
                    ),
                ]
            )
        else:
            not_executed.append(
                {
                    "check_id": "physical_warehouse_validation_and_trace",
                    "reason": "--physical-warehouse was not supplied",
                }
            )
    else:
        not_executed.append(
            {
                "check_id": "physical_optimization_bundle_and_warehouse",
                "reason": "--physical-optimization-bundle was not supplied",
            }
        )

    remaining_work = [
        {
            "work_id": "official_content_expansion",
            "stage": "knowledge_content",
            "description": "Extend the 13-source official seed corpus with material assignment, detailed convergence, postprocessing APIs, troubleshooting, and additional device examples.",
        },
        {
            "work_id": "additional_document_formats",
            "stage": "knowledge_content",
            "description": "Add PDF, Markdown, plain-text, and source-code parsers with the same raw-byte evidence policy.",
        },
        {
            "work_id": "bilingual_hybrid_retrieval",
            "stage": "knowledge_retrieval",
            "description": "Add Chinese-English semantic embeddings, lexical retrieval, metadata filters, and deterministic evaluation cases.",
        },
        {
            "work_id": "verified_knowledge_cards",
            "stage": "data_to_knowledge",
            "description": "Generate bounded knowledge cards only from DADC metrics whose validation and provenance checks pass.",
        },
        {
            "work_id": "data_communication_agent",
            "stage": "agent",
            "description": "Connect DADC queries and knowledge retrieval to a typed planner; do not allow arbitrary solver code execution.",
        },
        {
            "work_id": "surrogate_and_bayesian_tuning",
            "stage": "optimization",
            "description": "Add a surrogate model and acquisition policy, retaining full-solver verification of selected candidates.",
        },
        {
            "work_id": "numerical_and_experimental_validation",
            "stage": "scientific_validation",
            "description": "Add mesh independence, cross-solver checks, and experimental comparison where available.",
        },
    ]
    report = {
        "data_stage_acceptance_version": "1.0",
        "executed_at": _now(),
        "status": "passed" if all(item["status"] == "passed" for item in checks) else "failed",
        "evaluation_policy": "objective pass/fail checks only; no subjective score",
        "checks": checks,
        "not_executed": not_executed,
        "physical_optimization_summary": physical_summary,
        "remaining_work": remaining_work,
        "architecture_decision": (
            "Use one knowledge platform with a shared scope and device-scoped partitions. "
            "Shared PyAEDT/HFSS knowledge is reused; antenna, RF-filter, inductor, and future "
            "device knowledge is selected by metadata rather than copied into isolated databases."
        ),
    }
    json_path = output / "data_stage_acceptance.json"
    markdown_path = output / "data_stage_acceptance.md"
    _write_json(json_path, report)
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "passed_checks": sum(item["status"] == "passed" for item in checks),
                "failed_checks": sum(item["status"] == "failed" for item in checks),
                "not_executed_checks": len(not_executed),
                "json_report": str(json_path),
                "markdown_report": str(markdown_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
