"""Repository loading, validation, query, and lineage tracing."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .constants import ENTITY_ID_FIELDS, ENTITY_TYPES
from .integrity import verify_artifact
from .schema import SchemaRegistry

try:
    import h5py
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover - dependency error path
    raise RuntimeError(
        "DADC requires h5py and pyarrow. Install with `python3 -m pip install -e .`."
    ) from exc


@dataclass(frozen=True)
class ValidationIssue:
    category: str
    location: str
    message: str


@dataclass
class ValidationReport:
    repository: str
    issues: list[ValidationIssue] = field(default_factory=list)
    checked_records: int = 0
    checked_artifacts: int = 0
    checked_data_refs: int = 0

    @property
    def valid(self) -> bool:
        return not self.issues

    def add(self, category: str, location: str, message: str) -> None:
        self.issues.append(ValidationIssue(category, location, message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "valid": self.valid,
            "checked_records": self.checked_records,
            "checked_artifacts": self.checked_artifacts,
            "checked_data_refs": self.checked_data_refs,
            "issues": [asdict(issue) for issue in self.issues],
        }


class DADCRepository:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.manifest_path = self.root / "repository.json"
        if not self.manifest_path.is_file():
            raise FileNotFoundError(f"Missing repository.json under {self.root}")
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.schemas = SchemaRegistry(self.root / "schemas")
        self._records: dict[str, dict[str, dict[str, Any]]] | None = None
        self._locations: dict[tuple[str, str], str] = {}

    def _read_records(self) -> dict[str, dict[str, dict[str, Any]]]:
        records = {entity_type: {} for entity_type in ENTITY_TYPES}
        for case in self.manifest.get("cases", []):
            case_path = self.root / case["path"]
            metadata = case_path / "metadata"
            for entity_type in ENTITY_TYPES:
                if entity_type == "Validation":
                    continue
                directory = metadata / entity_type.lower()
                if not directory.is_dir():
                    continue
                for path in sorted(directory.glob("*.json")):
                    record = json.loads(path.read_text(encoding="utf-8"))
                    self._insert_record(records, record, path)
            validation_path = case_path / "validation.json"
            if validation_path.is_file():
                collection = json.loads(validation_path.read_text(encoding="utf-8"))
                for record in collection.get("validations", []):
                    self._insert_record(records, record, validation_path)
        return records

    def _insert_record(
        self,
        records: dict[str, dict[str, dict[str, Any]]],
        record: dict[str, Any],
        path: Path,
    ) -> None:
        entity_type = record.get("entity_type")
        if entity_type not in ENTITY_ID_FIELDS:
            key = f"invalid:{path}:{len(self._locations)}"
            records.setdefault("_invalid", {})[key] = record
            self._locations[("_invalid", key)] = path.relative_to(self.root).as_posix()
            return
        identifier = record.get(ENTITY_ID_FIELDS[entity_type])
        if identifier in records[entity_type]:
            raise ValueError(f"Duplicate {entity_type} id {identifier!r} in {path}")
        records[entity_type][identifier] = record
        self._locations[(entity_type, identifier)] = path.relative_to(self.root).as_posix()

    @property
    def record_map(self) -> dict[str, dict[str, dict[str, Any]]]:
        if self._records is None:
            self._records = self._read_records()
        return self._records

    def records(self, entity_type: str) -> list[dict[str, Any]]:
        return list(self.record_map[entity_type].values())

    def get(self, entity_type: str, identifier: str) -> dict[str, Any]:
        return self.record_map[entity_type][identifier]

    def _all_records(self) -> Iterable[tuple[str, str, dict[str, Any]]]:
        for entity_type in ENTITY_TYPES:
            for identifier, record in self.record_map[entity_type].items():
                yield entity_type, identifier, record

    def validate(self) -> ValidationReport:
        report = ValidationReport(repository=str(self.root))
        schema_invalid: set[tuple[str, str]] = set()
        for message in self.schemas.validate_repository_manifest(self.manifest):
            report.add("schema", "repository.json", message)
        for case in self.manifest.get("cases", []):
            validation_path = self.root / case["path"] / "validation.json"
            if not validation_path.is_file():
                report.add("schema", validation_path.relative_to(self.root).as_posix(), "missing validation.json")
                continue
            collection = json.loads(validation_path.read_text(encoding="utf-8"))
            for message in self.schemas.validate_validation_collection(collection):
                report.add("schema", validation_path.relative_to(self.root).as_posix(), message)

        if "_invalid" in self.record_map:
            for identifier in self.record_map["_invalid"]:
                report.add("schema", identifier, "Record has unsupported or missing entity_type")

        for entity_type, identifier, record in self._all_records():
            report.checked_records += 1
            location = self._locations[(entity_type, identifier)]
            messages = self.schemas.validate_record(record)
            if messages:
                schema_invalid.add((entity_type, identifier))
            for message in messages:
                report.add("schema", location, message)

        self._validate_references(report, schema_invalid)
        self._validate_data_refs(report, schema_invalid)
        self._validate_integrity(report, schema_invalid)
        self._validate_indexes(report)
        return report

    def _expect_ref(
        self,
        report: ValidationReport,
        source_location: str,
        entity_type: str,
        identifier: str,
    ) -> None:
        if identifier not in self.record_map[entity_type]:
            report.add("reference", source_location, f"Missing {entity_type} reference: {identifier}")

    def _validate_subject_refs(self, report: ValidationReport, location: str, record: dict[str, Any]) -> None:
        for ref in record.get("subject_refs", []):
            self._expect_ref(report, location, ref["entity_type"], ref["entity_id"])

    def _validate_references(
        self,
        report: ValidationReport,
        schema_invalid: set[tuple[str, str]],
    ) -> None:
        for entity_type, identifier, record in self._all_records():
            if (entity_type, identifier) in schema_invalid:
                continue
            location = self._locations[(entity_type, identifier)]
            if entity_type == "DesignRevision":
                self._expect_ref(report, location, "Device", record["device_id"])
                for artifact_id in record["artifact_ids"]:
                    self._expect_ref(report, location, "Artifact", artifact_id)
                for material in record["materials"]:
                    self._expect_ref(report, location, "Provenance", material["source_provenance_id"])
            elif entity_type == "Study":
                self._expect_ref(report, location, "Device", record["device_id"])
                for revision_id in record["design_revision_ids"]:
                    self._expect_ref(report, location, "DesignRevision", revision_id)
                for run_id in record["run_ids"]:
                    self._expect_ref(report, location, "Run", run_id)
                for edge in record.get("coupling_edges", []):
                    self._expect_ref(report, location, "Run", edge["source_run_id"])
                    self._expect_ref(report, location, "Run", edge["target_run_id"])
                    self._expect_ref(report, location, "Artifact", edge["mapping_artifact_id"])
                    for observable_id in edge["transferred_observable_ids"]:
                        self._expect_ref(report, location, "Observable", observable_id)
            elif entity_type == "Run":
                self._expect_ref(report, location, "Study", record["study_id"])
                self._expect_ref(report, location, "DesignRevision", record["design_revision_id"])
                self._expect_ref(report, location, "Provenance", record["provenance_id"])
                expected_marker = {
                    "simulation_run": "solver",
                    "experiment_run": "experiment",
                    "literature_record": "literature",
                    "data_processing": "processing",
                    "optimization_step": "optimization",
                }.get(record.get("activity_type"))
                activity_markers = {"solver", "experiment", "literature", "processing", "optimization"}
                present_markers = activity_markers.intersection(record.get("source_context", {}))
                if expected_marker is not None and present_markers != {expected_marker}:
                    report.add(
                        "activity_semantics",
                        location,
                        f"activity_type requires only source_context.{expected_marker}; got {sorted(present_markers)}",
                    )
                if "parent_run_id" in record:
                    self._expect_ref(report, location, "Run", record["parent_run_id"])
                for artifact_id in record["input_artifact_ids"] + record["output_artifact_ids"]:
                    self._expect_ref(report, location, "Artifact", artifact_id)
                if record.get("failure"):
                    self._expect_ref(report, location, "Artifact", record["failure"]["log_artifact_id"])
            elif entity_type == "Observable":
                self._expect_ref(report, location, "Run", record["run_id"])
                self._expect_ref(report, location, "Artifact", record["artifact_id"])
                self._expect_ref(report, location, "Provenance", record["provenance_id"])
                for source_id in record.get("derived_from_observable_ids", []):
                    self._expect_ref(report, location, "Observable", source_id)
                if record.get("derivation"):
                    self._expect_ref(report, location, "Artifact", record["derivation"]["script_artifact_id"])
            elif entity_type == "Metric":
                self._expect_ref(report, location, "Run", record["run_id"])
                self._expect_ref(report, location, "Provenance", record["provenance_id"])
                for source_id in record["source_observable_ids"]:
                    self._expect_ref(report, location, "Observable", source_id)
            elif entity_type == "Artifact":
                self._validate_subject_refs(report, location, record)
                self._expect_ref(report, location, "Provenance", record["provenance_id"])
            elif entity_type == "Validation":
                self._validate_subject_refs(report, location, record)
                self._expect_ref(report, location, "Provenance", record["provenance_id"])
                for artifact_id in record["evidence_artifact_ids"]:
                    self._expect_ref(report, location, "Artifact", artifact_id)
                script_id = record["method"].get("script_artifact_id")
                if script_id:
                    self._expect_ref(report, location, "Artifact", script_id)
            elif entity_type == "Provenance":
                self._validate_subject_refs(report, location, record)
                for artifact_id in record["scripts"]:
                    self._expect_ref(report, location, "Artifact", artifact_id)

    @staticmethod
    def _split_hdf5_ref(data_ref: str) -> tuple[str, str]:
        file_part, object_path = data_ref.split(":", 1)
        return file_part, object_path

    def _check_hdf5_ref(
        self,
        report: ValidationReport,
        location: str,
        data_ref: str,
        complex_representation: str | None = None,
    ) -> None:
        report.checked_data_refs += 1
        try:
            file_part, object_path = self._split_hdf5_ref(data_ref)
            path = (self.root / file_part).resolve()
            if self.root not in path.parents:
                raise ValueError("HDF5 path escapes repository root")
            with h5py.File(path, "r") as handle:
                if object_path not in handle:
                    raise KeyError(f"HDF5 object does not exist: {object_path}")
                obj = handle[object_path]
                if complex_representation == "real_imaginary":
                    if not isinstance(obj, h5py.Group):
                        raise TypeError("complex data_ref must point to a group")
                    if "real" not in obj or "imaginary" not in obj:
                        raise KeyError("complex group must contain real and imaginary datasets")
                    if obj["real"].shape != obj["imaginary"].shape:
                        raise ValueError("real and imaginary dataset shapes differ")
        except (OSError, KeyError, TypeError, ValueError) as exc:
            report.add("hdf5", location, f"{data_ref}: {exc}")

    def _validate_data_refs(
        self,
        report: ValidationReport,
        schema_invalid: set[tuple[str, str]],
    ) -> None:
        for observable in self.records("Observable"):
            if ("Observable", observable.get("observable_id", "")) in schema_invalid:
                continue
            location = self._locations[("Observable", observable["observable_id"])]
            self._check_hdf5_ref(
                report,
                location,
                observable["data_ref"],
                observable["complex_representation"],
            )
            for axis in observable["axes"]:
                self._check_hdf5_ref(report, location, axis["data_ref"])
            field_metadata = observable.get("field_metadata")
            if field_metadata:
                if field_metadata["data_ref"] != observable["data_ref"]:
                    report.add("field", location, "field_metadata.data_ref must equal Observable.data_ref")
                if field_metadata["coordinate_system_ref"] != observable["coordinate_system_ref"]:
                    report.add("field", location, "field coordinate_system_ref values differ")
                if field_metadata["components"] != observable["components"]:
                    report.add("field", location, "field component names differ")

    def _validate_integrity(
        self,
        report: ValidationReport,
        schema_invalid: set[tuple[str, str]],
    ) -> None:
        for artifact in self.records("Artifact"):
            if ("Artifact", artifact.get("artifact_id", "")) in schema_invalid:
                continue
            report.checked_artifacts += 1
            location = self._locations[("Artifact", artifact["artifact_id"])]
            valid, message = verify_artifact(self.root, artifact)
            if not valid:
                report.add("integrity", location, message)

    def _validate_indexes(self, report: ValidationReport) -> None:
        for index in self.manifest.get("indexes", []):
            path = (self.root / index["path"]).resolve()
            try:
                table = pq.read_table(path)
                if table.num_rows < 1:
                    raise ValueError("Parquet index is empty")
            except (OSError, ValueError) as exc:
                report.add("parquet", index["path"], str(exc))

    def trace_metric(self, metric_id: str) -> dict[str, Any]:
        metric = self.get("Metric", metric_id)
        observables: dict[str, dict[str, Any]] = {}

        def collect_observable(observable_id: str) -> None:
            if observable_id in observables:
                return
            observable = self.get("Observable", observable_id)
            observables[observable_id] = observable
            for parent_id in observable.get("derived_from_observable_ids", []):
                collect_observable(parent_id)

        runs: dict[str, dict[str, Any]] = {}
        artifacts: dict[str, dict[str, Any]] = {}
        provenances: dict[str, dict[str, Any]] = {}
        coupling_edges: list[dict[str, Any]] = []

        def collect_run(run_id: str) -> None:
            if run_id in runs:
                return
            run = self.get("Run", run_id)
            runs[run_id] = run
            if "parent_run_id" in run:
                collect_run(run["parent_run_id"])

        def collect_artifact(artifact_id: str) -> None:
            if artifact_id in artifacts:
                return
            artifact = self.get("Artifact", artifact_id)
            artifacts[artifact_id] = artifact
            provenances[artifact["provenance_id"]] = self.get(
                "Provenance", artifact["provenance_id"]
            )

        # A Metric is produced by its own Run, which can differ from the Run
        # that produced the source Observable (for example experiment -> data
        # processing). Include both sides so lineage never skips the extraction
        # activity that calculated the reported value.
        collect_run(metric["run_id"])
        for observable_id in metric["source_observable_ids"]:
            collect_observable(observable_id)

        # Resolve a fixed point because a thermal Metric can first reach its
        # thermal Run, then a Study coupling edge can introduce the upstream
        # electrical Run and transferred loss Observable. This makes the
        # cross-physics lineage explicit instead of stopping at the target Run.
        seen_edges: set[tuple[str, str, str]] = set()
        while True:
            before = (len(observables), len(runs), len(artifacts), len(seen_edges))
            for observable in list(observables.values()):
                collect_run(observable["run_id"])
                collect_artifact(observable["artifact_id"])
                provenances[observable["provenance_id"]] = self.get(
                    "Provenance", observable["provenance_id"]
                )
            for run in list(runs.values()):
                for artifact_id in run["input_artifact_ids"] + run["output_artifact_ids"]:
                    collect_artifact(artifact_id)
                provenances[run["provenance_id"]] = self.get(
                    "Provenance", run["provenance_id"]
                )
            for study in self.records("Study"):
                for edge in study.get("coupling_edges", []):
                    if edge["target_run_id"] not in runs:
                        continue
                    edge_key = (
                        study["study_id"],
                        edge["source_run_id"],
                        edge["target_run_id"],
                    )
                    if edge_key not in seen_edges:
                        coupling_edges.append({"study_id": study["study_id"], **edge})
                        seen_edges.add(edge_key)
                    collect_run(edge["source_run_id"])
                    collect_artifact(edge["mapping_artifact_id"])
                    for observable_id in edge["transferred_observable_ids"]:
                        collect_observable(observable_id)
            after = (len(observables), len(runs), len(artifacts), len(seen_edges))
            if after == before:
                break
        script_id = metric.get("calculation", {}).get("script_artifact_id")
        if script_id:
            collect_artifact(script_id)
        provenances[metric["provenance_id"]] = self.get("Provenance", metric["provenance_id"])
        return {
            "metric": metric,
            "observables": list(observables.values()),
            "runs": list(runs.values()),
            "artifacts": list(artifacts.values()),
            "provenance": list(provenances.values()),
            "coupling_edges": coupling_edges,
        }

    def query_metrics(self, **equals: Any) -> list[dict[str, Any]]:
        metrics_path = self.root / "index" / "metrics.parquet"
        rows = pq.read_table(metrics_path).to_pylist()
        return [row for row in rows if all(row.get(key) == value for key, value in equals.items())]

    def null_statistics(self) -> dict[str, int | float]:
        nulls = 0
        values = 0

        def visit(value: Any) -> None:
            nonlocal nulls, values
            if isinstance(value, dict):
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)
            else:
                values += 1
                if value is None:
                    nulls += 1

        for _, _, record in self._all_records():
            visit(record)
        return {"nulls": nulls, "leaf_values": values, "ratio": nulls / values if values else 0.0}
