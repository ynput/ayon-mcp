"""Helpers shared by tool implementations."""

from __future__ import annotations

from itertools import islice
from typing import Any, Iterable

MAX_LIMIT = 500


def collect(items: Iterable[dict[str, Any]], limit: int) -> dict[str, Any]:
    """Materialize up to ``limit`` items from a generator.

    Returns a dict with the items and a ``truncated`` flag so the model
    knows there were more results than it received.

    Args:
        items: Iterable of items to collect.
        limit: Maximum number of items to return (default 50, max 500).

    Returns:
        A dict with the following keys:
            - count: Number of items returned.
            - truncated: True if there were more items than the limit.
            - items: List of collected items (up to the limit).

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


def entity_fields(base: set[str], *, include_attrib: bool) -> set[str]:
    """Extend a default field set with attributes when requested.

    Args:
        base: Base set of fields to include.
        include_attrib: Whether to include the "attrib" field.

    Returns:
        A set of fields to request from the API,
        including "attrib" if requested.

    """
    fields = set(base)
    if include_attrib:
        # ayon_api expands "attrib" to all attribute fields.
        fields.add("attrib")
    return fields
