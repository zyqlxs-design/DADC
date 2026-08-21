# DADC V1.7.0 data-stage acceptance

Executed: 2026-08-21

Policy: objective pass/fail evidence only. No subjective score or percentage is used.

## Accepted in the portable environment

| Check | Result | Objective evidence |
|---|---|---|
| Frozen core data model | Passed | All 9 DADC V1.0 entity schemas are present; no new core entity was introduced |
| Device extensions | Passed | 6 sparse profiles are present: antenna, RF filter, inductor, multiphysics component, power resistor, and generic component |
| Ingestion boundaries | Passed | 6 registered adapters cover antenna, RF filter, inductor, tabular experiment, Joule-thermal field bundle, and optimization trace bundle |
| Regression | Passed | 64 tests executed; 64 passed; 0 failed |
| Knowledge manifest V1.1 | Passed | 4 test-only source documents produced 13 canonical chunks |
| Raw knowledge integrity | Passed | 4 raw artifacts checked by size and SHA-256; 0 failures |
| Rebuildable index | Passed | 13 records, 128 dimensions, two builds were byte-identical, index marked non-authoritative |
| Shared/device partition | Passed | Antenna, RF-filter, and inductor queries each returned shared knowledge plus only their own device fixture |
| Optimization ingestion | Passed | 57 DADC records, 16 artifacts, 11 data references, 0 validation issues |
| Failed-run preservation | Passed | 7 Runs retained: 1 intentional failure and 6 successful calls/processing steps |
| Verified metric trace | Passed | Verified metric reached 16 artifacts and 7 provenance records |
| Minimal vertical slice | Passed | Corpus, retrieval, bounded tuning, independent verification, ingestion, validation, and trace checks all passed |
| Wheel build | Passed | `dadc-1.7.0.dev0-py3-none-any.whl`, SHA-256 `2a5fa137bff7ff2764bc771db1ad6f61b541513fd78d5cfe27b609042634a5d1` |

## Not executed in this portable environment

| Check | Status | Reason / next evidence |
|---|---|---|
| Physical PyAEDT optimization artifact hashing | Not executed | The Linux acceptance host has no access to the Windows optimization bundle |
| Physical DADC warehouse validation and metric trace | Not executed | Run `scripts/run_data_stage_acceptance.py` on Windows with `--physical-optimization-bundle` and `--physical-warehouse` |

The user-run Windows trial reported two successful HFSS search calls with target-frequency errors
of 110 MHz and 20 MHz, followed by a successful independent verification at 20 MHz. Those values
are not marked as independently accepted here until the Windows bundle and warehouse are supplied
to the objective acceptance command.

## Architecture decision

Use one knowledge platform with a shared scope and device-scoped partitions. Shared PyAEDT/HFSS
knowledge is stored once. Antenna, RF-filter, inductor, and future device knowledge is selected by
`device_classes`, `topics`, and `knowledge_type`; separate duplicated knowledge systems are not
created per device.

## Remaining work

| Work | Stage | Completion condition |
|---|---|---|
| Expand official content | Knowledge content | Extend the 13-source seed corpus with material assignment, detailed convergence, postprocessing APIs, troubleshooting, and additional device examples |
| Add document formats | Knowledge content | PDF, Markdown, text, and code inputs preserve raw bytes and produce traceable chunks |
| Add bilingual hybrid retrieval | Knowledge retrieval | Chinese-English semantic and lexical retrieval pass fixed expected-source test cases |
| Generate verified knowledge cards | Data-to-knowledge | Only validation-passed DADC metrics produce bounded findings linked to Metric/Artifact/Provenance IDs |
| Build the data-communication agent | Agent | A typed planner can query DADC and the knowledge corpus without executing arbitrary generated code |
| Add surrogate/Bayesian tuning | Optimization | Candidate selection uses a surrogate while the selected result is independently rerun in the physical solver |
| Add scientific validation | Scientific validation | Mesh-independence, cross-solver, and experimental checks are attached where available |

The data-stage structure and portable evidence are accepted. Content growth is ongoing by design;
the next software boundary is the data-communication agent, not a change to the frozen DADC core.
