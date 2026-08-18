"""SHA-256 artifact integrity support."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_file_record(
    root: str | Path,
    path: str | Path,
    *,
    artifact_id: str,
    subject_refs: list[dict[str, str]],
    artifact_role: str,
    media_type: str,
    immutable: bool,
    value_origin: str,
    provenance_id: str,
    created_at: str,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    file_path = Path(path).resolve()
    relative = file_path.relative_to(root_path).as_posix()
    return {
        "entity_type": "Artifact",
        "schema_version": "1.0",
        "artifact_id": artifact_id,
        "subject_refs": subject_refs,
        "artifact_role": artifact_role,
        "relative_path": relative,
        "sha256": sha256_file(file_path),
        "size_bytes": file_path.stat().st_size,
        "media_type": media_type,
        "immutable": immutable,
        "value_origin": value_origin,
        "provenance_id": provenance_id,
        "created_at": created_at,
    }


def verify_artifact(root: str | Path, artifact: dict[str, Any]) -> tuple[bool, str]:
    root_path = Path(root).resolve()
    candidate = (root_path / artifact["relative_path"]).resolve()
    if root_path not in candidate.parents:
        return False, "artifact path escapes repository root"
    if not candidate.is_file():
        return False, f"missing file: {artifact['relative_path']}"
    actual_size = candidate.stat().st_size
    if actual_size != artifact["size_bytes"]:
        return False, f"size mismatch: expected {artifact['size_bytes']}, got {actual_size}"
    actual_hash = sha256_file(candidate)
    if actual_hash != artifact["sha256"]:
        return False, f"SHA-256 mismatch: expected {artifact['sha256']}, got {actual_hash}"
    return True, "ok"

