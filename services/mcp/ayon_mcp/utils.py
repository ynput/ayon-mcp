"""Helpers shared by tool implementations."""

from __future__ import annotations

import os
from itertools import islice
from typing import Any, Iterable

MAX_LIMIT = 500

_READ_ONLY_OVERRIDE: bool | None = None


def collect(
    items: Iterable[dict[str, Any]],
    limit: int,
    offset: int = 0,
) -> dict[str, Any]:
    """Materialize up to ``limit`` items from a generator, skipping ``offset``.

    Returns a dict with the items, a ``truncated`` flag and, when there
    are more results, a ``next_offset`` the caller can pass back as
    ``offset`` to fetch the next page.

    Args:
        items: Iterable of items to collect.
        limit: Maximum number of items to return (default 50, max 500).
        offset: Number of items to skip from the start (for paging).

    Returns:
        A dict with the following keys:
            - count: Number of items returned.
            - truncated: True if there were more items than the limit.
            - next_offset: Offset of the next page (None when not truncated).
            - items: List of collected items (up to the limit).

    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    result = list(islice(items, offset, offset + limit + 1))
    truncated = len(result) > limit
    if truncated:
        result = result[:limit]
    return {
        "count": len(result),
        "truncated": truncated,
        "next_offset": offset + len(result) if truncated else None,
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


def env_flag(name: str) -> bool:
    """Return True when the environment variable holds a truthy value.

    Accepted truthy values are "1", "true", "yes" and "on"
    (case-insensitive).

    Args:
        name: Environment variable name.

    Returns:
        True when the variable is set to a truthy value.

    """
    value = (os.getenv(name, "") or "").strip()
    return value.lower() in {"1", "true", "yes", "on"}


def set_read_only(value: bool | None) -> None:  # ruff: ignore[boolean-type-hint-positional-argument]
    """Override the read-only mode resolved from the environment.

    Used by remote mode to apply the addon settings toggle. Passing
    None removes the override so ``AYON_MCP_READ_ONLY`` applies again.

    Args:
        value: Explicit read-only state, or None to clear the override.

    """
    global _READ_ONLY_OVERRIDE  # noqa: PLW0603 - process-wide mode flag
    _READ_ONLY_OVERRIDE = value


def read_only_enabled() -> bool:
    """Return True when the server should expose only read tools.

    An explicit override set via :func:`set_read_only` wins; otherwise
    the ``AYON_MCP_READ_ONLY`` environment variable decides.

    Returns:
        True when read-only mode is enabled.

    """
    if _READ_ONLY_OVERRIDE is not None:
        return _READ_ONLY_OVERRIDE
    return env_flag("AYON_MCP_READ_ONLY")
