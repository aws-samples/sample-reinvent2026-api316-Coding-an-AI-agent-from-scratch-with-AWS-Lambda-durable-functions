"""Loads the exhibitor catalog that Agent 2 (Booth Matcher) searches against."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "data", "exhibitor_catalog.json")


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    """Return the full catalog document (cached after first read)."""
    with open(_CATALOG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def booths() -> list[dict[str, Any]]:
    """Return just the list of booth records."""
    return load_catalog()["booths"]


@lru_cache(maxsize=1)
def all_tags() -> tuple[str, ...]:
    """Every topic tag in the catalog, lowercased and sorted.

    This is the search vocabulary. It is published in the ``search_catalog`` tool
    spec so the model picks real tags instead of guessing at phrasing.
    """
    return tuple(sorted({t.lower().strip() for b in booths() for t in b["tags"]}))


def booth_by_number(booth_number: str) -> dict[str, Any] | None:
    """Look up a single booth by its number, or ``None`` if not found."""
    for booth in booths():
        if booth["booth_number"] == booth_number:
            return booth
    return None
