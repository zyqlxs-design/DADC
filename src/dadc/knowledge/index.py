"""Small deterministic retrieval projection for contract tests and local use."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import numpy as np

INDEX_VERSION = "1.0.0"
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*|\d+(?:\.\d+)?|[\u3400-\u9fff]")


def _tokens(text: str) -> list[str]:
    rendered: list[str] = []
    for token in _TOKEN.findall(text.lower()):
        rendered.append(token)
        if "_" in token:
            rendered.extend(part for part in token.split("_") if part)
        if "." in token and token[0].isalpha():
            rendered.extend(part for part in token.split(".") if part)
    return rendered


def _vector(text: str, dimensions: int) -> np.ndarray:
    value = np.zeros(dimensions, dtype=np.float32)
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "little") % dimensions
        sign = 1.0 if digest[8] & 1 else -1.0
        value[index] += sign
    norm = float(np.linalg.norm(value))
    if norm:
        value /= norm
    return value


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def build_index(corpus: str | Path, *, dimensions: int = 512) -> dict[str, Any]:
    """Rebuild a deterministic hashed-token vector projection from canonical chunks."""

    if dimensions < 64 or dimensions > 4096:
        raise ValueError("dimensions must be between 64 and 4096")
    root = Path(corpus).resolve()
    chunks_path = root / "chunks.jsonl"
    chunks = _jsonl(chunks_path)
    if not chunks:
        raise ValueError("Cannot index an empty corpus")
    vectors = np.vstack(
        [_vector(f"{item['heading']}\n{item['text']}", dimensions) for item in chunks]
    )
    index_root = root / "index"
    index_root.mkdir(exist_ok=True)
    vectors_temporary = index_root / ".embeddings.npy.tmp"
    with vectors_temporary.open("wb") as stream:
        np.save(stream, vectors, allow_pickle=False)
    vectors_temporary.replace(index_root / "embeddings.npy")
    records_path = index_root / "records.jsonl"
    records_temporary = index_root / ".records.jsonl.tmp"
    records_temporary.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in chunks),
        encoding="utf-8",
    )
    records_temporary.replace(records_path)
    metadata = {
        "semantic_index_version": "1.0",
        "builder_version": INDEX_VERSION,
        "model": "dadc_hashed_token_v1",
        "dimensions": dimensions,
        "record_count": len(chunks),
        "chunks_sha256": hashlib.sha256(chunks_path.read_bytes()).hexdigest(),
        "records_sha256": hashlib.sha256(records_path.read_bytes()).hexdigest(),
        "rebuild_command": "dadc knowledge-index CORPUS",
        "authoritative": False,
    }
    (index_root / "index.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {**metadata, "index": str(index_root)}


def search_index(
    corpus: str | Path,
    query: str,
    *,
    top_k: int = 5,
    device_class: str | None = None,
    knowledge_type: str | None = None,
    topic: str | None = None,
) -> list[dict[str, Any]]:
    """Search the rebuildable projection and return source-addressable evidence."""

    if not query.strip():
        raise ValueError("query must not be empty")
    if top_k < 1:
        raise ValueError("top_k must be positive")
    root = Path(corpus).resolve()
    metadata = json.loads((root / "index" / "index.json").read_text(encoding="utf-8"))
    chunks_path = root / "chunks.jsonl"
    actual_hash = hashlib.sha256(chunks_path.read_bytes()).hexdigest()
    if actual_hash != metadata["chunks_sha256"]:
        raise ValueError("Corpus chunks changed after indexing; rebuild the index")
    records = _jsonl(root / "index" / "records.jsonl")
    embeddings = np.load(root / "index" / "embeddings.npy", allow_pickle=False)
    if embeddings.shape[0] != len(records):
        raise ValueError("Index record/vector count mismatch")
    query_vector = _vector(query, int(metadata["dimensions"]))
    scores = embeddings @ query_vector
    eligible: list[int] = []
    for index, record in enumerate(records):
        device_classes = set(record.get("device_classes", ["shared"]))
        if device_class and device_class not in device_classes and "shared" not in device_classes:
            continue
        if knowledge_type and record.get("knowledge_type") != knowledge_type:
            continue
        if topic and topic not in set(record.get("topics", [])):
            continue
        eligible.append(index)
    ranked = sorted(eligible, key=lambda index: (-float(scores[index]), index))
    results: list[dict[str, Any]] = []
    for index in ranked[: min(top_k, len(records))]:
        record = records[index]
        results.append(
            {
                "score": float(scores[index]),
                "chunk_id": record["chunk_id"],
                "heading": record["heading"],
                "section_type": record["section_type"],
                "text": record["text"],
                "knowledge_type": record.get("knowledge_type", "unclassified"),
                "device_classes": record.get("device_classes", ["shared"]),
                "topics": record.get("topics", ["unclassified"]),
                "language": record.get("language", "not_recorded"),
                "authority": record.get("authority", "not_recorded"),
                "validation_status": record.get("validation_status", "not_recorded"),
                "evidence": {
                    "source_id": record["source_id"],
                    "source_url": record["source_url"],
                    "product_version": record["product_version"],
                    "locator": record["locator"],
                    "content_sha256": record["content_sha256"],
                },
            }
        )
    return results
