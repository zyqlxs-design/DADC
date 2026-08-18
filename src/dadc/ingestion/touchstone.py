"""Touchstone 1.x parser with deterministic complex normalization.

The source file is never changed. RI, MA, and DB encodings are converted to an
in-memory complex matrix whose shape is ``(frequency, port_out, port_in)``.
Touchstone's column-major ordering (S11, S21, S12, S22 for two ports) is
therefore made explicit before a repository writer stores real and imaginary
datasets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class TouchstoneError(ValueError):
    """Raised when a Touchstone file cannot be parsed without guessing."""


_PORT_SUFFIX = re.compile(r"\.s(?P<count>[1-9][0-9]*)p$", re.IGNORECASE)
_PORT_COMMENT = re.compile(r"^Port\[(?P<index>[1-9][0-9]*)\]\s*=\s*(?P<name>.+)$", re.IGNORECASE)
_HFSS_EXPORT_COMMENT = re.compile(
    r"^Touchstone file exported from HFSS\s+(?P<version>\S+)$",
    re.IGNORECASE,
)
_UNICODE_ESCAPE = re.compile(r"%x(?P<code>[0-9A-Fa-f]{4})")
_VARIABLE_VALUE = re.compile(
    r"^(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)\s*(?P<unit>.*)$"
)

_FREQUENCY_SCALE = {
    "HZ": 1.0,
    "KHZ": 1.0e3,
    "MHZ": 1.0e6,
    "GHZ": 1.0e9,
}


@dataclass(frozen=True)
class TouchstoneData:
    """Parsed Touchstone data and source metadata."""

    source: Path
    port_count: int
    port_names: tuple[str, ...]
    frequency_unit: str
    parameter: str
    source_complex_format: str
    source_text_encoding: str
    reference_impedance_ohm: float
    frequencies_hz: np.ndarray
    values: np.ndarray
    comments: tuple[str, ...]
    metadata: dict[str, str]
    variables: dict[str, dict[str, float | str]]
    option_line: str

    @property
    def components(self) -> tuple[str, ...]:
        return tuple(
            f"S{row + 1}{column + 1}"
            for row in range(self.port_count)
            for column in range(self.port_count)
        )


def decode_aedt_unicode_escapes(value: str) -> str:
    """Decode the ``%x4E34`` form used by AEDT in exported comments."""

    return _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group("code"), 16)), value)


def _parse_option_line(line: str) -> tuple[str, str, str, float]:
    tokens = line[1:].split()
    if len(tokens) < 3:
        raise TouchstoneError(f"Incomplete option line: {line!r}")
    frequency_unit = tokens[0].upper()
    parameter = tokens[1].upper()
    complex_format = tokens[2].upper()
    if frequency_unit not in _FREQUENCY_SCALE:
        raise TouchstoneError(f"Unsupported frequency unit: {frequency_unit}")
    if parameter != "S":
        raise TouchstoneError(f"Only S-parameters are supported; got {parameter}")
    if complex_format not in {"RI", "MA", "DB"}:
        raise TouchstoneError(f"Unsupported complex format: {complex_format}")
    reference = 50.0
    upper = [token.upper() for token in tokens]
    if "R" in upper:
        index = upper.index("R")
        if index + 1 >= len(tokens):
            raise TouchstoneError("Option line contains R without an impedance")
        try:
            reference = float(tokens[index + 1])
        except ValueError as exc:
            raise TouchstoneError(f"Invalid reference impedance: {tokens[index + 1]!r}") from exc
    return frequency_unit, parameter, complex_format, reference


def _complex_values(pairs: np.ndarray, representation: str) -> np.ndarray:
    first = pairs[..., 0]
    second = pairs[..., 1]
    if representation == "RI":
        return first + 1j * second
    angle = np.deg2rad(second)
    magnitude = first if representation == "MA" else np.power(10.0, first / 20.0)
    return magnitude * np.exp(1j * angle)


def _parse_variable(raw: str) -> dict[str, float | str]:
    match = _VARIABLE_VALUE.fullmatch(raw.strip())
    if not match:
        return {"value": raw.strip(), "unit": "source_native"}
    unit = match.group("unit").strip() or "1"
    return {"value": float(match.group("number")), "unit": unit}


def parse_touchstone(path: str | Path) -> TouchstoneData:
    """Parse a Touchstone 1.x ``.sNp`` file without modifying it."""

    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    suffix_match = _PORT_SUFFIX.search(source.name)
    if not suffix_match:
        raise TouchstoneError(f"Port count is not encoded in the .sNp suffix: {source.name}")
    port_count = int(suffix_match.group("count"))

    comments: list[str] = []
    numeric_tokens: list[float] = []
    option_line: str | None = None
    port_names: dict[int, str] = {}
    metadata: dict[str, str] = {}
    variables: dict[str, dict[str, float | str]] = {}
    in_variables = False

    source_bytes = source.read_bytes()
    try:
        source_text = source_bytes.decode("utf-8-sig")
        source_text_encoding = "utf-8-sig" if source_bytes.startswith(b"\xef\xbb\xbf") else "utf-8"
    except UnicodeDecodeError:
        # Touchstone 1.x has no encoding declaration. Several vendor exports
        # use Windows punctuation (for example an en dash in comments) while
        # keeping all numeric records ASCII-compatible. CP1252 is therefore a
        # deterministic, lossless fallback; undecodable inputs still fail.
        try:
            source_text = source_bytes.decode("cp1252")
            source_text_encoding = "windows-1252"
        except UnicodeDecodeError as exc:  # pragma: no cover - cp1252 maps all bytes
            raise TouchstoneError("Touchstone text is neither UTF-8 nor Windows-1252") from exc

    for line_number, raw_line in enumerate(source_text.splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("!"):
            comment = stripped[1:].strip()
            comments.append(comment)
            hfss_match = _HFSS_EXPORT_COMMENT.fullmatch(comment)
            if hfss_match:
                metadata["hfss_version"] = hfss_match.group("version")
            if comment.lower() == "variables:":
                in_variables = True
                continue
            if in_variables:
                if not comment:
                    in_variables = False
                    continue
                if "=" in comment:
                    name, value = (part.strip() for part in comment.split("=", 1))
                    variables[name] = _parse_variable(value)
                    continue
                in_variables = False
            port_match = _PORT_COMMENT.fullmatch(comment)
            if port_match:
                port_names[int(port_match.group("index"))] = port_match.group("name").strip()
                continue
            if ":" in comment:
                key, value = (part.strip() for part in comment.split(":", 1))
                if key and value:
                    normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
                    metadata[normalized_key] = value
            continue
        data_part = raw_line.split("!", 1)[0].strip()
        if not data_part:
            continue
        if data_part.startswith("#"):
            if option_line is not None:
                raise TouchstoneError(f"Multiple option lines are not supported (line {line_number})")
            option_line = data_part
            continue
        try:
            numeric_tokens.extend(float(token) for token in data_part.split())
        except ValueError as exc:
            raise TouchstoneError(f"Non-numeric data at line {line_number}: {data_part!r}") from exc

    if option_line is None:
        raise TouchstoneError("Missing Touchstone option line")
    frequency_unit, parameter, complex_format, reference = _parse_option_line(option_line)
    record_width = 1 + 2 * port_count * port_count
    if not numeric_tokens or len(numeric_tokens) % record_width:
        raise TouchstoneError(
            f"Numeric token count {len(numeric_tokens)} is not divisible by record width {record_width}"
        )
    records = np.asarray(numeric_tokens, dtype=np.float64).reshape((-1, record_width))
    frequencies_hz = records[:, 0] * _FREQUENCY_SCALE[frequency_unit]
    if not np.all(np.isfinite(records)):
        raise TouchstoneError("Touchstone data contains NaN or infinity")
    if len(frequencies_hz) > 1 and not np.all(np.diff(frequencies_hz) > 0.0):
        raise TouchstoneError("Frequency axis must be strictly increasing")

    encoded_pairs = records[:, 1:].reshape((-1, port_count * port_count, 2))
    ordered = _complex_values(encoded_pairs, complex_format)
    matrix = np.empty((records.shape[0], port_count, port_count), dtype=np.complex128)
    for column in range(port_count):
        for row in range(port_count):
            matrix[:, row, column] = ordered[:, column * port_count + row]

    resolved_names = tuple(port_names.get(index, f"P{index}") for index in range(1, port_count + 1))
    if "file" in metadata:
        metadata["file_decoded"] = decode_aedt_unicode_escapes(metadata["file"])
    return TouchstoneData(
        source=source,
        port_count=port_count,
        port_names=resolved_names,
        frequency_unit=frequency_unit,
        parameter=parameter,
        source_complex_format=complex_format,
        source_text_encoding=source_text_encoding,
        reference_impedance_ohm=reference,
        frequencies_hz=frequencies_hz,
        values=matrix,
        comments=tuple(comments),
        metadata=metadata,
        variables=variables,
        option_line=option_line,
    )
