# DADC data-stage acceptance report

- Executed at: `2026-08-21T06:52:53Z`
- Overall status: `passed`
- Evaluation policy: objective pass/fail checks only; no subjective score is used.

## Accepted checks

| Check | Status | Objective evidence |
|---|---|---|
| `frozen_core_entity_schemas_present` | `passed` | `{"expected": ["artifact.schema.json", "design_revision.schema.json", "device.schema.json", "metric.schema.json", "observable.schema.json", "provenance.schema.json", "run.schema.json", "study.schema.json", "validation.schema.json"], "found": ["artifact.schema.json", "design_revision.schema.json", "device.schema.json", "metric.schema.json", "observable.schema.json", "provenance.schema.json", "run.schema.json", "study.schema.json", "validation.schema.json"]}` |
| `device_profiles_present` | `passed` | `{"found": ["antenna.schema.json", "generic_component.schema.json", "inductor.schema.json", "multiphysics_component.schema.json", "power_resistor.schema.json", "rf_filter.schema.json"], "required": ["antenna.schema.json", "generic_component.schema.json", "inductor.schema.json", "multiphysics_component.schema.json", "power_resistor.schema.json", "rf_filter.schema.json"]}` |
| `knowledge_manifest_v11_and_content_counts` | `passed` | `{"chunks": 13, "documents": 4, "manifest_version": "1.1"}` |
| `knowledge_raw_artifact_integrity` | `passed` | `{"checked_documents": 4, "failures": []}` |
| `knowledge_index_rebuildable` | `passed` | `{"authoritative": false, "byte_identical_rebuild": true, "dimensions": 128, "record_count": 13}` |
| `shared_plus_device_partition_retrieval` | `passed` | `{"antenna_sources": ["antenna_workflow_fixture", "pyaedt_shared_api_fixture"], "inductor_sources": ["inductor_workflow_fixture", "pyaedt_shared_api_fixture"], "rf_filter_sources": ["pyaedt_shared_api_fixture", "rf_filter_workflow_fixture"]}` |
| `optimization_bundle_ingested` | `passed` | `{"adapter_id": "optimization_trace_bundle", "adapter_version": "1.0.0", "case_id": "mvp_optimization_001", "duplicate_of_case_id": null, "message": "Case committed; raw source preserved and global indexes rebuilt.", "quarantine_path": null, "source": "D:\\DADC_TEST\\data_acceptance_physical_20260821_145246\\optimization_fixture\\optimization_bundle.json", "source_sha256": "b18dbcd79a2aa164b306f1553b24d8d80cd1967b1a3555f4378e0e6ea634734c", "status": "ingested", "validation": {"checked_artifacts": 16, "checked_data_refs": 11, "checked_records": 57, "issues": [], "repository": "D:\\DADC_TEST\\data_acceptance_physical_20260821_145246\\data_root\\warehouse", "valid": true}, "warehouse": "D:\\DADC_TEST\\data_acceptance_physical_20260821_145246\\data_root\\warehouse"}` |
| `warehouse_schema_and_integrity_validation` | `passed` | `{"checked_artifacts": 16, "checked_data_refs": 11, "checked_records": 57, "issues": [], "repository": "D:\\DADC_TEST\\data_acceptance_physical_20260821_145246\\data_root\\warehouse", "valid": true}` |
| `failed_and_verified_runs_preserved` | `passed` | `{"failed_run_count": 1, "run_count": 7, "succeeded_run_count": 6}` |
| `verified_metric_trace_reaches_artifacts_and_provenance` | `passed` | `{"artifact_count": 16, "metric_id": "metric_mvp_optimization_001_verified_objective", "provenance_count": 7}` |
| `required_ingestion_adapters_registered` | `passed` | `{"registered": ["joule_thermal_field_bundle", "optimization_trace_bundle", "tabular_experiment_csv", "touchstone_antenna", "touchstone_inductor", "touchstone_rf_filter"], "required": ["joule_thermal_field_bundle", "optimization_trace_bundle", "tabular_experiment_csv", "touchstone_antenna", "touchstone_inductor", "touchstone_rf_filter"]}` |
| `physical_backend_declared` | `passed` | `{"backend": {"backend_id": "pyaedt_patch", "backend_version": "1.0.0", "evidence_level": "local_aedt_solver_with_touchstone_and_native_project", "is_physical_solver": true}}` |
| `physical_trials_completed` | `passed` | `{"search_trials": 2, "statuses": {"search_001": "succeeded", "search_002": "succeeded", "verify_001": "succeeded"}, "verification_trials": 1}` |
| `physical_artifact_integrity` | `passed` | `{"artifact_count": 27, "failures": []}` |
| `physical_native_and_touchstone_evidence` | `passed` | `{"artifact_roles": ["native_project", "raw_input", "report", "solver_log", "validation_evidence"], "media_types": ["application/json", "application/octet-stream", "application/vnd.touchstone", "text/plain"]}` |
| `physical_warehouse_validation` | `passed` | `{"checked_artifacts": 31, "checked_data_refs": 13, "checked_records": 60, "issues": [], "repository": "D:\\DADC_TEST\\real_data_pyaedt_patch_smoke_20260821_113206\\warehouse", "valid": true}` |
| `physical_verified_metric_trace` | `passed` | `{"artifact_count": 31, "metric_id": "metric_pyaedt_patch_smoke_20260821_113206_verified_objective", "provenance_count": 4}` |

## Checks not executed

- None.

## Remaining work

- `official_content_expansion` (knowledge_content): Extend the 13-source official seed corpus with material assignment, detailed convergence, postprocessing APIs, troubleshooting, and additional device examples.
- `additional_document_formats` (knowledge_content): Add PDF, Markdown, plain-text, and source-code parsers with the same raw-byte evidence policy.
- `bilingual_hybrid_retrieval` (knowledge_retrieval): Add Chinese-English semantic embeddings, lexical retrieval, metadata filters, and deterministic evaluation cases.
- `verified_knowledge_cards` (data_to_knowledge): Generate bounded knowledge cards only from DADC metrics whose validation and provenance checks pass.
- `data_communication_agent` (agent): Connect DADC queries and knowledge retrieval to a typed planner; do not allow arbitrary solver code execution.
- `surrogate_and_bayesian_tuning` (optimization): Add a surrogate model and acquisition policy, retaining full-solver verification of selected candidates.
- `numerical_and_experimental_validation` (scientific_validation): Add mesh independence, cross-solver checks, and experimental comparison where available.

## Architecture decision

Use one knowledge platform with a shared scope and device-scoped partitions. Shared PyAEDT/HFSS knowledge is reused; antenna, RF-filter, inductor, and future device knowledge is selected by metadata rather than copied into isolated databases.
