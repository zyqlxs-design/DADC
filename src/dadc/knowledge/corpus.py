"""Controlled documentation collection with immutable raw-byte evidence."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ..contracts import validate_contract

COLLECTOR_VERSION = "1.0.0"
_SPACE = re.compile(r"[ \t\f\v]+")
_SUPPORTED_MANIFEST_VERSIONS = {"1.0", "1.1"}
_LOCAL_SOURCE_TYPES = {"local_fixture", "lab_document", "validated_finding"}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normal_text(value: str) -> str:
    lines = [_SPACE.sub(" ", line).strip() for line in value.replace("\r", "").split("\n")]
    return "\n".join(line for line in lines if line).strip()


@dataclass(frozen=True)
class _Section:
    heading: str
    section_type: str
    text: str
    locator: str


class _SemanticHTMLParser(HTMLParser):
    """Small deterministic parser that retains headings and preformatted code."""

    _BLOCK_TAGS = {"p", "li", "dt", "dd", "div", "section", "article", "tr"}
    _SKIP_TAGS = {"script", "style", "svg", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._title_parts: list[str] = []
        self._heading = "Document"
        self._heading_parts: list[str] = []
        self._heading_level: str | None = None
        self._text_parts: list[str] = []
        self._pre_parts: list[str] = []
        self._in_title = False
        self._in_pre = False
        self._skip_depth = 0
        self._section_number = 0
        self.sections: list[_Section] = []

    def _flush_text(self) -> None:
        text = _normal_text("".join(self._text_parts))
        self._text_parts.clear()
        if not text:
            return
        self._section_number += 1
        self.sections.append(
            _Section(
                heading=self._heading,
                section_type="documentation",
                text=text,
                locator=f"section:{self._section_number}",
            )
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif re.fullmatch(r"h[1-6]", tag):
            self._flush_text()
            self._heading_level = tag
            self._heading_parts = []
        elif tag == "pre":
            self._flush_text()
            self._in_pre = True
            self._pre_parts = []
        elif tag in self._BLOCK_TAGS or tag == "br":
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            self.title = _normal_text("".join(self._title_parts))
        elif tag == self._heading_level:
            heading = _normal_text("".join(self._heading_parts))
            if heading:
                self._heading = heading
            self._heading_level = None
            self._heading_parts = []
        elif tag == "pre" and self._in_pre:
            code = "".join(self._pre_parts).replace("\r\n", "\n").strip("\n")
            if code:
                self._section_number += 1
                self.sections.append(
                    _Section(
                        heading=self._heading,
                        section_type="code",
                        text=code,
                        locator=f"section:{self._section_number}:pre",
                    )
                )
            self._in_pre = False
            self._pre_parts = []
        elif tag in self._BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        if self._heading_level:
            self._heading_parts.append(data)
        elif self._in_pre:
            self._pre_parts.append(data)
        else:
            self._text_parts.append(data)

    def finish(self) -> list[_Section]:
        self._flush_text()
        return self.sections


def _fetch_document(
    source: dict[str, Any],
    manifest_path: Path,
    *,
    allowed_hosts: set[str],
    timeout: float,
    max_bytes: int,
) -> tuple[bytes, str, str]:
    raw_url = str(source["url"])
    local_path = Path(raw_url)
    if local_path.is_absolute():
        if source["source_type"] not in _LOCAL_SOURCE_TYPES:
            raise ValueError(
                "Local paths require source_type=local_fixture, lab_document, or validated_finding"
            )
        value = local_path.read_bytes()
        if len(value) > max_bytes:
            raise ValueError(f"Document exceeds max_bytes_per_document={max_bytes}: {local_path}")
        return value, local_path.resolve().as_uri(), "text/html"

    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme in ("http", "https"):
        if not allowed_hosts or parsed.hostname not in allowed_hosts:
            raise ValueError(f"Host is not explicitly allowed for collection: {parsed.hostname!r}")
        request = urllib.request.Request(
            raw_url,
            headers={"User-Agent": f"DADC-Knowledge-Collector/{COLLECTOR_VERSION}"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            final_host = urllib.parse.urlparse(final_url).hostname
            if final_host not in allowed_hosts:
                raise ValueError(f"Redirect escaped allowed_hosts: {final_host!r}")
            content_type = response.headers.get_content_type()
            value = response.read(max_bytes + 1)
        if len(value) > max_bytes:
            raise ValueError(f"Document exceeds max_bytes_per_document={max_bytes}: {raw_url}")
        return value, final_url, content_type
    if parsed.scheme not in ("", "file"):
        raise ValueError(f"Unsupported source URL scheme: {parsed.scheme!r}")
    if source["source_type"] not in _LOCAL_SOURCE_TYPES:
        raise ValueError(
            "Local paths require source_type=local_fixture, lab_document, or validated_finding"
        )
    candidate = Path(urllib.request.url2pathname(parsed.path)) if parsed.scheme else Path(raw_url)
    if not candidate.is_absolute():
        candidate = (manifest_path.parent / candidate).resolve()
    value = candidate.read_bytes()
    if len(value) > max_bytes:
        raise ValueError(f"Document exceeds max_bytes_per_document={max_bytes}: {candidate}")
    return value, candidate.as_uri(), "text/html"


def _split_section(section: _Section, max_characters: int) -> list[tuple[str, str]]:
    if len(section.text) <= max_characters:
        return [(section.locator, section.text)]
    units = section.text.splitlines(keepends=True)
    parts: list[tuple[str, str]] = []
    current: list[str] = []
    current_length = 0
    for unit in units:
        fragments = [unit[index : index + max_characters] for index in range(0, len(unit), max_characters)]
        for fragment in fragments:
            if current and current_length + len(fragment) > max_characters:
                parts.append((f"{section.locator}:part:{len(parts) + 1}", "".join(current).strip()))
                current = []
                current_length = 0
            current.append(fragment)
            current_length += len(fragment)
    if current:
        parts.append((f"{section.locator}:part:{len(parts) + 1}", "".join(current).strip()))
    return [(locator, text) for locator, text in parts if text]


def collect_corpus(manifest: str | Path, target: str | Path) -> dict[str, Any]:
    """Collect an explicit source list; raw bytes are content-addressed and immutable."""

    manifest_path = Path(manifest).resolve()
    source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_version = str(source_manifest.get("knowledge_manifest_version", ""))
    if manifest_version not in _SUPPORTED_MANIFEST_VERSIONS:
        raise ValueError(
            "knowledge_manifest_version must be one of "
            f"{sorted(_SUPPORTED_MANIFEST_VERSIONS)}"
        )
    validate_contract(
        source_manifest,
        "knowledge_source_manifest",
        version=manifest_version,
    )
    root = Path(target).resolve()
    if root.exists() and (root.is_file() or any(root.iterdir())):
        raise FileExistsError(f"Refusing to overwrite non-empty corpus: {root}")
    root.mkdir(parents=True, exist_ok=True)
    raw_root = root / "raw"
    raw_root.mkdir()

    allowed_hosts = set(source_manifest.get("allowed_hosts", []))
    timeout = float(source_manifest.get("request_timeout_seconds", 30.0))
    max_bytes = int(source_manifest.get("max_bytes_per_document", 5_000_000))
    chunk_max = int(source_manifest.get("chunk_max_characters", 2400))
    document_records: list[dict[str, Any]] = []
    chunk_records: list[dict[str, Any]] = []

    for source in source_manifest["documents"]:
        raw, final_url, content_type = _fetch_document(
            source,
            manifest_path,
            allowed_hosts=allowed_hosts,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        digest = _sha256_bytes(raw)
        raw_path = raw_root / f"{digest}.html"
        if not raw_path.exists():
            raw_path.write_bytes(raw)
        parser = _SemanticHTMLParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        sections = parser.finish()
        if not sections:
            raise ValueError(f"No extractable documentation sections: {final_url}")
        document_id = f"doc_{_sha256_bytes(final_url.encode('utf-8'))[:20]}"
        if manifest_version == "1.1":
            knowledge_metadata = {
                "knowledge_type": source["knowledge_type"],
                "device_classes": list(source["device_classes"]),
                "topics": list(source["topics"]),
                "language": source["language"],
                "authority": source["authority"],
                "validation_status": source["validation_status"],
                "evidence_refs": list(source.get("evidence_refs", [])),
            }
        else:
            source_type = source["source_type"]
            knowledge_metadata = {
                "knowledge_type": (
                    "official_example"
                    if source_type == "official_example"
                    else "api_reference"
                    if source_type == "official_documentation"
                    else "test_fixture"
                ),
                "device_classes": ["shared"],
                "topics": ["unclassified"],
                "language": "en",
                "authority": "test_fixture" if source_type == "local_fixture" else "official",
                "validation_status": (
                    "test_only" if source_type == "local_fixture" else "source_verified"
                ),
                "evidence_refs": [],
            }
        rendered_sections = [
            {
                "heading": section.heading,
                "section_type": section.section_type,
                "locator": section.locator,
                "text": section.text,
            }
            for section in sections
        ]
        document_record = {
            "semantic_document_version": "1.0",
            "document_id": document_id,
            "source_id": source["source_id"],
            "source_url": final_url,
            "source_type": source["source_type"],
            "product": source["product"],
            "product_version": source["product_version"],
            "license": source["license"],
            **knowledge_metadata,
            "retrieved_at": source_manifest["retrieved_at"],
            "title": parser.title or source["source_id"],
            "raw_artifact": {
                "relative_path": raw_path.relative_to(root).as_posix(),
                "sha256": digest,
                "size_bytes": len(raw),
                "media_type": content_type,
                "immutable": True,
            },
            "parser": {"name": "dadc_semantic_html", "version": COLLECTOR_VERSION},
            "sections": rendered_sections,
        }
        document_records.append(document_record)
        for section in sections:
            for locator, text in _split_section(section, chunk_max):
                content_digest = _sha256_bytes(text.encode("utf-8"))
                chunk_records.append(
                    {
                        "semantic_chunk_version": "1.0",
                        "chunk_id": f"chunk_{content_digest[:20]}",
                        "document_id": document_id,
                        "source_id": source["source_id"],
                        "source_url": final_url,
                        "product": source["product"],
                        "product_version": source["product_version"],
                        **knowledge_metadata,
                        "heading": section.heading,
                        "section_type": section.section_type,
                        "locator": locator,
                        "text": text,
                        "content_sha256": content_digest,
                    }
                )

    manifest_snapshot = root / "source_manifest.json"
    _write_json(manifest_snapshot, source_manifest)
    documents_path = root / "documents.jsonl"
    chunks_path = root / "chunks.jsonl"
    documents_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in document_records),
        encoding="utf-8",
    )
    chunks_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in chunk_records),
        encoding="utf-8",
    )
    corpus = {
        "corpus_version": "1.0",
        "knowledge_manifest_version": manifest_version,
        "corpus_id": source_manifest["corpus_id"],
        "created_at": source_manifest["retrieved_at"],
        "collector": {"name": "DADC Knowledge Collector", "version": COLLECTOR_VERSION},
        "source_manifest_sha256": _sha256_bytes(_canonical_json(source_manifest)),
        "document_count": len(document_records),
        "chunk_count": len(chunk_records),
        "documents_sha256": _sha256_bytes(documents_path.read_bytes()),
        "chunks_sha256": _sha256_bytes(chunks_path.read_bytes()),
        "facts_authority": "documentation_corpus_only; not DADC scientific entity truth",
        "projection_policy": "indexes are derived and rebuildable from chunks.jsonl",
    }
    _write_json(root / "corpus.json", corpus)
    return {**corpus, "corpus": str(root)}
