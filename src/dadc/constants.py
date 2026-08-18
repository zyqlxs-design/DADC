"""Frozen DADC V1.0 contract constants."""

SCHEMA_VERSION = "1.0"

ENTITY_TYPES = (
    "Device",
    "DesignRevision",
    "Study",
    "Run",
    "Observable",
    "Metric",
    "Artifact",
    "Validation",
    "Provenance",
)

ENTITY_ID_FIELDS = {
    "Device": "device_id",
    "DesignRevision": "design_revision_id",
    "Study": "study_id",
    "Run": "run_id",
    "Observable": "observable_id",
    "Metric": "metric_id",
    "Artifact": "artifact_id",
    "Validation": "validation_id",
    "Provenance": "provenance_id",
}

ACTIVITY_TYPES = (
    "simulation_run",
    "experiment_run",
    "literature_record",
    "data_processing",
    "optimization_step",
)

VALIDATION_TYPES = (
    "schema_validation",
    "solver_convergence",
    "mesh_independence",
    "physical_rule_check",
    "cross_solver_comparison",
    "experiment_comparison",
    "literature_benchmark",
)

VALUE_ORIGINS = (
    "raw_solver_output",
    "raw_experiment_output",
    "literature_extracted",
    "calculated",
    "manual_entry",
)

