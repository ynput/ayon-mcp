"""Helpers shared by tool implementations."""

from __future__ import annotations

from itertools import islice
from typing import Any, Iterable

MAX_LIMIT = 500


def collect(items: Iterable[dict[str, Any]], limit: int) -> dict[str, Any]:
    """Materialize up to ``limit`` items from a generator.

    Returns a dict with the items and a ``truncated`` flag so the model
    knows there were more results than it received.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    result = list(islice(items, limit + 1))
    truncated = len(result) > limit
    if truncated:
        result = result[:limit]
    return {
        "count": len(result),
        "truncated": truncated,
        "items": result,
    }


def entity_fields(base: set[str], include_attrib: bool) -> set[str]:
    """Extend a default field set with attributes when requested."""
    fields = set(base)
    if include_attrib:
        # ayon_api expands "attrib" to all attribute fields.
        fields.add("attrib")
    return fields
