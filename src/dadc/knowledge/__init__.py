"""Reproducible documentation corpus and rebuildable search projection."""

from .corpus import collect_corpus
from .index import build_index, search_index

__all__ = ["build_index", "collect_corpus", "search_index"]
