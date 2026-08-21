# DADC data and knowledge stage

## Scope

This stage establishes trustworthy storage and retrieval before an agent is allowed to plan or
execute tuning work. It covers:

- the frozen DADC V1.0 entity model;
- sparse device profiles and source adapters;
- immutable raw artifacts, SHA-256 integrity, quarantine, validation, and metric traceability;
- a controlled documentation corpus with source-addressable chunks;
- shared knowledge and device-scoped knowledge partitions;
- deterministic, rebuildable local retrieval;
- an objective, machine-readable acceptance report.

It does not claim that an LLM, surrogate model, Bayesian optimizer, mesh-independence workflow,
or experimental-validation workflow is complete.

## Data foundation versus knowledge platform

The DADC warehouse records what happened: devices, revisions, studies, runs, observables,
metrics, artifacts, validations, and provenance. The knowledge platform records how to act or
interpret evidence: API references, procedures, design rules, troubleshooting guidance, and
bounded findings derived from validated DADC records.

The retrieval index is not authoritative. Raw documents, semantic documents, and chunks are the
canonical knowledge evidence; the index is a derived projection that can be rebuilt.

## Device knowledge architecture

DADC uses one knowledge platform, not one duplicated implementation per device. Every knowledge
document declares `device_classes`:

- `shared` for PyAEDT/HFSS APIs, units, solver operations, and other reusable knowledge;
- `antenna` for antenna geometry, feeds, radiation, and antenna-specific metrics;
- `rf_filter` for ports, passbands, filter-order semantics, and multi-port metrics;
- `inductor` for inductance, quality factor, self-resonance, and vendor evidence;
- additional device classes can be appended without changing the retrieval engine.

A device-filtered query returns shared knowledge plus knowledge for the requested device class.
It excludes documents scoped to other device classes. Topic and knowledge-type filters can narrow
the same corpus further.

## Knowledge manifest V1.1

The V1.1 source contract adds mandatory metadata to each document:

- `knowledge_type`;
- `device_classes`;
- `topics`;
- `language`;
- `authority`;
- `validation_status`;
- optional `evidence_refs` for validated findings.

V1.0 manifests remain readable. V1.1 is required for device-aware knowledge partitioning.

## Objective acceptance

Run the portable acceptance without physical AEDT evidence:

```powershell
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
python .\scripts\run_data_stage_acceptance.py `
  --output-dir "D:\DADC_TEST\data_acceptance_$Stamp"
```

Include an existing physical PyAEDT optimization bundle and its ingested warehouse:

```powershell
$CaseId = "pyaedt_patch_smoke_20260821_113206"
$Bundle = "D:\DADC_TEST\$CaseId\optimization_bundle.json"
$Warehouse = "D:\DADC_TEST\real_data_$CaseId\warehouse"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"

python .\scripts\run_data_stage_acceptance.py `
  --output-dir "D:\DADC_TEST\data_acceptance_physical_$Stamp" `
  --physical-optimization-bundle $Bundle `
  --physical-warehouse $Warehouse
```

The command writes `data_stage_acceptance.json` and `data_stage_acceptance.md`. Checks use only
pass/fail conditions and objective evidence such as counts, file sizes, SHA-256 values, schema
validation, artifact roles, run statuses, and trace reachability. No subjective score is used.

## Remaining work after this stage

The generated report lists remaining work explicitly. The next implementation boundary is the
data-communication agent: typed access to DADC queries and knowledge retrieval, followed by a
constrained planner. Surrogate-assisted or Bayesian tuning is a later numerical optimization layer
and must retain independent full-solver verification.
