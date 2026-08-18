"""DADC command-line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .demo import create_demo_repository
from .migration import migrate_record
from .repository import DADCRepository


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _read_intake_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path).resolve()
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Intake manifest must be a JSON object: {manifest_path}")
    version = value.get("intake_schema_version")
    if version != "1.0":
        raise ValueError(f"intake_schema_version must be '1.0', got {version!r}")
    companions = value.get("companion_artifacts", [])
    if not isinstance(companions, list):
        raise ValueError(f"companion_artifacts must be an array: {manifest_path}")
    for item in companions:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError(f"Each companion_artifacts item requires a string path: {manifest_path}")
        candidate = Path(item["path"])
        if not candidate.is_absolute():
            item["path"] = str((manifest_path.parent / candidate).resolve())
    return value


def _ingest_cli_values(args: argparse.Namespace) -> dict[str, Any]:
    intake = _read_intake_manifest(args.manifest) if args.manifest else {"intake_schema_version": "1.0"}
    for key in (
        "adapter",
        "case_id",
        "device_name",
        "device_class",
        "device_subtype",
        "filter_order",
        "feed_type",
        "radiation_mode",
        "source_timezone",
        "operator_id",
        "platform",
        "compute",
        "solver_edition",
        "activity_type",
        "processed_at",
    ):
        value = getattr(args, key, None)
        if value is not None:
            intake[key] = value
    return intake


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dadc")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create-demo", help="create deterministic examples")
    create_parser.add_argument("target")
    create_parser.add_argument("--replace", action="store_true")

    validate_parser = subparsers.add_parser("validate", help="validate a DADC repository")
    validate_parser.add_argument("repository")

    trace_parser = subparsers.add_parser("trace-metric", help="trace a metric to original files")
    trace_parser.add_argument("repository")
    trace_parser.add_argument("metric_id")

    migrate_parser = subparsers.add_parser("migrate", help="migrate a JSON record")
    migrate_parser.add_argument("input")
    migrate_parser.add_argument("output")
    migrate_parser.add_argument("--target", default="1.0")

    touchstone_parser = subparsers.add_parser(
        "ingest-touchstone",
        help="create a validated RF-filter repository from one .sNp file",
    )
    touchstone_parser.add_argument("source")
    touchstone_parser.add_argument("target")
    touchstone_parser.add_argument("--case-id", required=True)
    touchstone_parser.add_argument("--device-name", required=True)
    touchstone_parser.add_argument("--filter-order", required=True, type=int)
    touchstone_parser.add_argument("--source-timezone", required=True)
    touchstone_parser.add_argument("--operator-id", default="local_user")
    touchstone_parser.add_argument("--platform", default="windows")
    touchstone_parser.add_argument("--compute", default="not_recorded")
    touchstone_parser.add_argument("--solver-edition", default="Student")

    init_parser = subparsers.add_parser(
        "init-warehouse",
        help="create inbox/staging/quarantine folders for a shared data root",
    )
    init_parser.add_argument("data_root")

    subparsers.add_parser(
        "adapters",
        help="print the machine-readable capability boundary of installed source adapters",
    )

    ingest_parser = subparsers.add_parser(
        "ingest",
        help="detect an adapter and append one source (or a folder of manifests) to a shared warehouse",
    )
    ingest_parser.add_argument("source")
    ingest_parser.add_argument("--warehouse", required=True)
    ingest_parser.add_argument("--manifest", help="DADC intake JSON for a single source")
    ingest_parser.add_argument("--adapter")
    ingest_parser.add_argument("--case-id")
    ingest_parser.add_argument("--device-name")
    ingest_parser.add_argument("--device-class")
    ingest_parser.add_argument("--device-subtype")
    ingest_parser.add_argument("--filter-order", type=int)
    ingest_parser.add_argument("--feed-type")
    ingest_parser.add_argument("--radiation-mode")
    ingest_parser.add_argument("--source-timezone")
    ingest_parser.add_argument("--operator-id")
    ingest_parser.add_argument("--platform")
    ingest_parser.add_argument("--compute")
    ingest_parser.add_argument("--solver-edition")
    ingest_parser.add_argument(
        "--activity-type",
        choices=(
            "simulation_run",
            "experiment_run",
            "literature_record",
            "data_processing",
            "optimization_step",
        ),
    )
    ingest_parser.add_argument("--processed-at")

    args = parser.parse_args(argv)
    if args.command == "create-demo":
        create_demo_repository(args.target, replace=args.replace)
        print(Path(args.target).resolve())
        return 0
    if args.command == "validate":
        report = DADCRepository(args.repository).validate()
        _print_json(report.to_dict())
        return 0 if report.valid else 1
    if args.command == "trace-metric":
        _print_json(DADCRepository(args.repository).trace_metric(args.metric_id))
        return 0
    if args.command == "migrate":
        source = json.loads(Path(args.input).read_text(encoding="utf-8"))
        migrated = migrate_record(source, args.target)
        output = Path(args.output)
        if output.exists():
            raise FileExistsError(f"Refusing to overwrite {output}")
        output.write_text(json.dumps(migrated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    if args.command == "ingest-touchstone":
        from dataclasses import asdict

        from .ingestion.importer import ingest_touchstone_filter_repository

        result = ingest_touchstone_filter_repository(
            args.source,
            args.target,
            case_id=args.case_id,
            device_name=args.device_name,
            filter_order=args.filter_order,
            source_timezone=args.source_timezone,
            operator_id=args.operator_id,
            platform=args.platform,
            compute=args.compute,
            solver_edition=args.solver_edition,
        )
        rendered = asdict(result)
        rendered["repository"] = str(result.repository)
        _print_json(rendered)
        return 0
    if args.command == "init-warehouse":
        from .warehouse import initialize_data_root

        _print_json(initialize_data_root(args.data_root))
        return 0
    if args.command == "adapters":
        from .ingestion.registry import AdapterRegistry

        _print_json(
            {
                "adapter_catalog_version": "1.0",
                "adapters": AdapterRegistry().catalog(),
            }
        )
        return 0
    if args.command == "ingest":
        from .warehouse import WarehouseManager

        manager = WarehouseManager(args.warehouse)
        source = Path(args.source).resolve()
        if source.is_dir():
            if args.manifest:
                raise ValueError("--manifest is only valid when SOURCE is one file")
            manifest_paths = sorted(source.rglob("*.dadc.json"))
            if not manifest_paths:
                raise ValueError(
                    "Folder ingestion requires one or more *.dadc.json manifests; "
                    "each manifest must contain a source path relative to itself"
                )
            rendered: list[dict[str, Any]] = []
            for manifest_path in manifest_paths:
                intake = _read_intake_manifest(manifest_path)
                relative_source = intake.get("source")
                if not isinstance(relative_source, str) or not relative_source:
                    raise ValueError(f"Manifest has no source string: {manifest_path}")
                item_source = (manifest_path.parent / relative_source).resolve()
                result = manager.ingest(item_source, intake)
                rendered.append(result.to_dict())
            _print_json({"results": rendered})
            return 1 if any(item["status"] == "quarantined" for item in rendered) else 0

        result = manager.ingest(source, _ingest_cli_values(args))
        _print_json(result.to_dict())
        return 1 if result.status == "quarantined" else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
