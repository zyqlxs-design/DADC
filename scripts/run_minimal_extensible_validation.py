"""Run the offline contract proof from documentation corpus through DADC traceability."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.automation.backends import create_backend  # noqa: E402
from dadc.automation.tuning import run_optimization  # noqa: E402
from dadc.knowledge import build_index, collect_corpus, search_index  # noqa: E402
from dadc.repository import DADCRepository  # noqa: E402
from dadc.warehouse import WarehouseManager, initialize_data_root  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    output = Path(args.output_dir).resolve()
    if output.exists() and (output.is_file() or any(output.iterdir())):
        raise FileExistsError(f"Refusing to overwrite non-empty validation output: {output}")
    output.mkdir(parents=True, exist_ok=True)

    knowledge_manifest = REPOSITORY_ROOT / "examples" / "knowledge" / "local_fixture_sources.json"
    corpus = output / "knowledge_corpus"
    corpus_result = collect_corpus(knowledge_manifest, corpus)
    index_result = build_index(corpus, dimensions=128)
    search_results = search_index(corpus, "Hfss create_setup frequency sweep", top_k=2)

    fixture_plan_path = REPOSITORY_ROOT / "examples" / "automation" / "analytic_fixture_plan.json"
    optimization_result = run_optimization(fixture_plan_path, output / "optimization")
    data_paths = initialize_data_root(output / "data_root")
    ingestion = WarehouseManager(data_paths["warehouse"]).ingest(
        optimization_result["bundle"], {}
    )
    if ingestion.status != "ingested":
        raise RuntimeError(json.dumps(ingestion.to_dict(), ensure_ascii=False, indent=2))
    repository = DADCRepository(data_paths["warehouse"])
    validation = repository.validate()
    metric_id = "metric_mvp_optimization_001_verified_objective"
    trace = repository.trace_metric(metric_id)

    pyaedt_plan = json.loads(
        (REPOSITORY_ROOT / "examples" / "automation" / "pyaedt_patch_plan.json").read_text(
            encoding="utf-8"
        )
    )
    pyaedt_preflight = create_backend(pyaedt_plan["backend"]).preflight(pyaedt_plan)
    checks = {
        "corpus_raw_document_preserved": corpus_result["document_count"] == 1,
        "semantic_index_rebuildable": index_result["authoritative"] is False,
        "search_returns_source_evidence": bool(search_results and search_results[0]["evidence"]),
        "bounded_search_executed": optimization_result["search_trials"] == 5,
        "independent_verification_executed": optimization_result["verification_trials"] == 1,
        "failed_trial_preserved": any(
            item["status"] == "failed" for item in repository.records("Run")
        ),
        "optimization_ingested": ingestion.status == "ingested",
        "warehouse_valid": validation.valid,
        "verified_metric_traceable": bool(trace["artifacts"] and trace["provenance"]),
        "fixture_is_not_physical_solver": optimization_result["backend"]["is_physical_solver"] is False,
        "real_pyaedt_backend_is_explicit": pyaedt_preflight["backend_id"] == "pyaedt_patch",
    }
    report = {
        "minimal_extensible_validation_version": "1.0",
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "counts": {
            "knowledge_documents": corpus_result["document_count"],
            "knowledge_chunks": corpus_result["chunk_count"],
            "search_trials": optimization_result["search_trials"],
            "verification_trials": optimization_result["verification_trials"],
            "dadc_records": sum(len(repository.records(entity)) for entity in (
                "Device", "DesignRevision", "Study", "Run", "Observable", "Metric",
                "Artifact", "Validation", "Provenance"
            )),
            "trace_artifacts": len(trace["artifacts"]),
        },
        "best_metric": optimization_result["best_metric"],
        "pyaedt_backend_preflight": pyaedt_preflight,
        "claim_boundary": {
            "hfss_executed_in_this_validation": False,
            "proven": "crawler, rebuildable retrieval, typed calls, tuning, verification, quarantine, and DADC evidence lineage",
            "not_proven": "AEDT installation, license availability, HFSS numerical validity, mesh convergence, or experiment agreement",
        },
    }
    report_path = output / "acceptance.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "report": str(report_path)}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
