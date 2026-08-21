"""Transactional, append-only ingestion into one shared DADC warehouse."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .indexing import rebuild_indexes, scan_records
from .integrity import sha256_file
from .repository import DADCRepository
from .ingestion.registry import AdapterRegistry

REGISTRY_VERSION = "1.0"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class WarehouseIngestionResult:
    status: str
    source: str
    source_sha256: str
    warehouse: str
    case_id: str | None = None
    adapter_id: str | None = None
    adapter_version: str | None = None
    duplicate_of_case_id: str | None = None
    quarantine_path: str | None = None
    message: str | None = None
    validation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def initialize_data_root(data_root: str | Path) -> dict[str, str]:
    """Create operational folders without inventing an empty DADC repository."""

    root = Path(data_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    result = {"data_root": str(root)}
    for name in ("inbox", "staging", "quarantine"):
        path = root / name
        path.mkdir(exist_ok=True)
        result[name] = str(path)
    # repository.schema.json requires at least one Case. Therefore ``warehouse``
    # is intentionally created by the first successful transaction, not here.
    result["warehouse"] = str(root / "warehouse")
    return result


class WarehouseManager:
    def __init__(
        self,
        warehouse: str | Path,
        *,
        adapters: AdapterRegistry | None = None,
    ):
        self.warehouse = Path(warehouse).resolve()
        self.data_root = self.warehouse.parent
        self.staging = self.data_root / "staging"
        self.quarantine = self.data_root / "quarantine"
        self.registry = adapters or AdapterRegistry()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(exist_ok=True)
        self.quarantine.mkdir(exist_ok=True)

    @property
    def registry_path(self) -> Path:
        return self.warehouse / "system" / "ingestion_registry.json"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        lock_path = self.data_root / ".dadc-ingest.lock"
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError(
                f"Warehouse ingestion is already locked: {lock_path}. "
                "If no ingestion process is running, inspect and remove the stale lock manually."
            ) from exc
        try:
            os.write(descriptor, f"pid={os.getpid()} created_at={_utc_now()}\n".encode("utf-8"))
            os.close(descriptor)
            yield
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            lock_path.unlink(missing_ok=True)

    def _load_registry(self) -> dict[str, Any]:
        if not self.registry_path.is_file():
            return {"registry_version": REGISTRY_VERSION, "records": []}
        value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        if value.get("registry_version") != REGISTRY_VERSION or not isinstance(value.get("records"), list):
            raise ValueError(f"Invalid ingestion registry: {self.registry_path}")
        return value

    def _source_hash_cases(self) -> dict[str, str]:
        matches: dict[str, str] = {}
        if self.warehouse.is_dir():
            for path in sorted((self.warehouse / "cases").glob("*/metadata/artifact/*.json")):
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("artifact_role") in {"raw_input", "measurement_data"}:
                    matches.setdefault(str(record["sha256"]), path.relative_to(self.warehouse).parts[1])
        for record in self._load_registry()["records"]:
            if record.get("status") == "ingested":
                matches.setdefault(str(record["source_sha256"]), str(record["case_id"]))
        return matches

    def _append_registry_record(self, record: dict[str, Any]) -> None:
        registry = self._load_registry()
        registry["records"].append(record)
        _atomic_json(self.registry_path, registry)

    def _quarantine_source(
        self,
        source: Path,
        source_hash: str,
        *,
        reason: str,
        case_id: str | None,
        adapter_id: str | None,
    ) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self.quarantine / f"{stamp}_{source_hash[:12]}"
        destination.mkdir(parents=True)
        preserved = destination / source.name
        if source.is_dir():
            shutil.copytree(source, preserved)
        else:
            shutil.copy2(source, preserved)
        _atomic_json(
            destination / "quarantine.json",
            {
                "quarantine_version": "1.0",
                "created_at": _utc_now(),
                "source_name": source.name,
                "source_sha256": source_hash,
                "case_id": case_id,
                "adapter_id": adapter_id,
                "reason": reason,
                "preserved_path": preserved.name,
            },
        )
        return destination

    @staticmethod
    def _identifier_set(root: Path) -> set[tuple[str, str]]:
        return {(entity_type, identifier) for entity_type, identifier, _, _ in scan_records(root)}

    @staticmethod
    def _canonical_schema_sha256(path: Path) -> str:
        """Hash JSON meaning, not checkout-specific whitespace or line endings."""

        value = json.loads(path.read_text(encoding="utf-8-sig"))
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _merge_schemas(staged: Path, warehouse: Path) -> list[Path]:
        added: list[Path] = []
        for source in sorted((staged / "schemas").rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(staged / "schemas")
            target = warehouse / "schemas" / relative
            if target.exists():
                if source.suffix.lower() == ".json" and target.suffix.lower() == ".json":
                    equal = (
                        WarehouseManager._canonical_schema_sha256(source)
                        == WarehouseManager._canonical_schema_sha256(target)
                    )
                else:
                    equal = sha256_file(source) == sha256_file(target)
                if not equal:
                    raise ValueError(f"Schema conflict; existing schema is immutable: schemas/{relative.as_posix()}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            shutil.copy2(source, temporary)
            temporary.replace(target)
            added.append(target)
        return added

    def _publish_first(self, staged: Path, processed_at: str) -> dict[str, Any]:
        if self.warehouse.exists():
            if self.warehouse.is_file() or any(self.warehouse.iterdir()):
                raise FileExistsError(f"Warehouse has data but no repository.json: {self.warehouse}")
            self.warehouse.rmdir()
        manifest_path = staged / "repository.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["repository_id"] = "dadc_shared_warehouse"
        manifest["created_at"] = processed_at
        _atomic_json(manifest_path, manifest)
        staged.replace(self.warehouse)
        try:
            validation = DADCRepository(self.warehouse).validate()
            if not validation.valid:
                raise RuntimeError(json.dumps(validation.to_dict(), ensure_ascii=False, indent=2))
            return validation.to_dict()
        except Exception:
            staged.parent.mkdir(parents=True, exist_ok=True)
            self.warehouse.replace(staged)
            raise

    def _append_staged(self, staged: Path, case_id: str) -> dict[str, Any]:
        current = DADCRepository(self.warehouse).validate()
        if not current.valid:
            raise RuntimeError(
                "Refusing to append to an invalid warehouse:\n"
                + json.dumps(current.to_dict(), ensure_ascii=False, indent=2)
            )
        manifest_path = self.warehouse / "repository.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        existing_case_ids = {item["case_id"] for item in manifest["cases"]}
        if case_id in existing_case_ids or (self.warehouse / "cases" / case_id).exists():
            raise ValueError(f"case_id already exists with different source content: {case_id}")
        conflicts = self._identifier_set(self.warehouse).intersection(self._identifier_set(staged))
        if conflicts:
            rendered = ", ".join(f"{kind}:{identifier}" for kind, identifier in sorted(conflicts))
            raise ValueError(f"Entity identifier conflict: {rendered}")

        backup = staged.parent / "rollback"
        backup.mkdir()
        shutil.copy2(manifest_path, backup / "repository.json")
        for name in ("catalog.parquet", "metrics.parquet"):
            shutil.copy2(self.warehouse / "index" / name, backup / name)

        target_case = self.warehouse / "cases" / case_id
        source_case = staged / "cases" / case_id
        added_schemas: list[Path] = []
        case_published = False
        try:
            added_schemas = self._merge_schemas(staged, self.warehouse)
            source_case.replace(target_case)
            case_published = True
            manifest["cases"].append({"case_id": case_id, "path": f"cases/{case_id}"})
            manifest["cases"] = sorted(manifest["cases"], key=lambda item: item["case_id"])
            _atomic_json(manifest_path, manifest)
            rebuild_indexes(self.warehouse)
            validation = DADCRepository(self.warehouse).validate()
            if not validation.valid:
                raise RuntimeError(json.dumps(validation.to_dict(), ensure_ascii=False, indent=2))
            return validation.to_dict()
        except Exception:
            if case_published and target_case.exists():
                source_case.parent.mkdir(parents=True, exist_ok=True)
                target_case.replace(source_case)
            shutil.copy2(backup / "repository.json", manifest_path)
            for name in ("catalog.parquet", "metrics.parquet"):
                shutil.copy2(backup / name, self.warehouse / "index" / name)
            for path in reversed(added_schemas):
                path.unlink(missing_ok=True)
            raise

    def ingest(self, source: str | Path, intake: dict[str, Any]) -> WarehouseIngestionResult:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Ingestion source must be a file: {source_path}")
        source_hash = sha256_file(source_path)
        case_id = str(intake.get("case_id")) if intake.get("case_id") else None
        processed_at = str(intake.get("processed_at") or _utc_now())

        with self._lock():
            duplicate = self._source_hash_cases().get(source_hash)
            if duplicate:
                return WarehouseIngestionResult(
                    status="duplicate",
                    source=str(source_path),
                    source_sha256=source_hash,
                    warehouse=str(self.warehouse),
                    case_id=case_id,
                    duplicate_of_case_id=duplicate,
                    message="Identical source bytes already exist; no new Case was created.",
                )

        transaction = self.staging / f"txn_{uuid.uuid4().hex}"
        staged_repository = transaction / "repository"
        transaction.mkdir(parents=True)
        adapter_id: str | None = None
        adapter_version: str | None = None
        try:
            adapter, _probe = self.registry.select(source_path, intake)
            adapter_id = adapter.adapter_id
            adapter_version = adapter.adapter_version
            staged_result = adapter.build_case_repository(source_path, staged_repository, intake)
            case_id = staged_result.case_id

            with self._lock():
                duplicate = self._source_hash_cases().get(source_hash)
                if duplicate:
                    return WarehouseIngestionResult(
                        status="duplicate",
                        source=str(source_path),
                        source_sha256=source_hash,
                        warehouse=str(self.warehouse),
                        case_id=case_id,
                        adapter_id=adapter_id,
                        adapter_version=adapter_version,
                        duplicate_of_case_id=duplicate,
                        message="Identical source bytes were committed by another ingestion; no Case was added.",
                    )
                if (self.warehouse / "repository.json").is_file():
                    validation = self._append_staged(staged_repository, case_id)
                else:
                    validation = self._publish_first(staged_repository, processed_at)
                registry_warning: str | None = None
                try:
                    self._append_registry_record(
                        {
                            "status": "ingested",
                            "ingested_at": _utc_now(),
                            "source_name": source_path.name,
                            "source_sha256": source_hash,
                            "case_id": case_id,
                            "adapter_id": adapter_id,
                            "adapter_version": adapter_version,
                        }
                    )
                except Exception as exc:
                    # The immutable raw Artifact is the authoritative dedup
                    # fallback. A registry write failure must not misreport a
                    # successfully committed and validated Case as quarantined.
                    registry_warning = f"; registry warning: {type(exc).__name__}: {exc}"
            return WarehouseIngestionResult(
                status="ingested",
                source=str(source_path),
                source_sha256=source_hash,
                warehouse=str(self.warehouse),
                case_id=case_id,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                message=(
                    "Case committed; raw source preserved and global indexes rebuilt."
                    + (registry_warning or "")
                ),
                validation=validation,
            )
        except Exception as exc:
            quarantine_path = self._quarantine_source(
                source_path,
                source_hash,
                reason=f"{type(exc).__name__}: {exc}",
                case_id=case_id,
                adapter_id=adapter_id,
            )
            return WarehouseIngestionResult(
                status="quarantined",
                source=str(source_path),
                source_sha256=source_hash,
                warehouse=str(self.warehouse),
                case_id=case_id,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                quarantine_path=str(quarantine_path),
                message=f"{type(exc).__name__}: {exc}",
            )
        finally:
            shutil.rmtree(transaction, ignore_errors=True)
