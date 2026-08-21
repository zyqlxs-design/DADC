"""DADC V1.0 repository toolkit.

The repository classes are loaded lazily so lightweight adapters can inspect an
input file before the binary HDF5 and Parquet dependencies are imported.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .repository import DADCRepository, ValidationReport

__all__ = ["DADCRepository", "ValidationReport"]
__version__ = "1.7.0.dev0"


def __getattr__(name: str) -> Any:
    if name in {"DADCRepository", "ValidationReport"}:
        from .repository import DADCRepository, ValidationReport

        return {"DADCRepository": DADCRepository, "ValidationReport": ValidationReport}[name]
    raise AttributeError(name)
