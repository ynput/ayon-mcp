"""Load, cache and query the AYON server OpenAPI specification."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Generator

from .rest_client import get_global_rest_client

_BUNDLED_SPEC_PATH = Path(__file__).parent.parent / "ayon_openapi.json"

_SPEC_CACHE: dict[str, Any] | None = None

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

# How many nested ``$ref`` hops to inline before leaving the raw
# reference in place. Keeps deeply nested / recursive schemas bounded.
MAX_REF_DEPTH = 6


async def get_openapi_spec() -> dict[str, Any]:
    """Return the AYON OpenAPI spec, fetched once and cached.

    The live server is tried first (``GET /openapi.json``) so that addon
    endpoints and the server version are current. When the server cannot
    be reached, the spec bundled with the package is used instead.

    Returns:
        The parsed OpenAPI specification document.

    """
    global _SPEC_CACHE  # noqa: PLW0603 - process-wide spec cache
    if _SPEC_CACHE is not None:
        return _SPEC_CACHE

    spec: dict[str, Any] | None = None
    try:
        fetched = await get_global_rest_client().request(
            "GET", "/openapi.json"
        )
        if isinstance(fetched, dict) and "paths" in fetched:
            spec = fetched
    except Exception:  # noqa: BLE001 - any failure falls back to bundle
        spec = None

    if spec is None:
        spec = await asyncio.to_thread(_load_bundled_spec)

    _SPEC_CACHE = spec
    return spec


def _load_bundled_spec() -> dict[str, Any]:
    """Read and parse the OpenAPI spec bundled with the package.

    Returns:
        The parsed OpenAPI specification document.

    """
    return json.loads(_BUNDLED_SPEC_PATH.read_text(encoding="utf-8"))


def set_spec_cache(spec: dict[str, Any] | None) -> None:
    """Set or clear the cached spec (``None`` forces a re-fetch).

    Args:
        spec: Parsed OpenAPI document to cache, or ``None`` to clear.

    """
    global _SPEC_CACHE  # noqa: PLW0603 - process-wide spec cache
    _SPEC_CACHE = spec


def iter_operations(
    spec: dict[str, Any],
) -> Generator[tuple[str, str, dict[str, Any]], None, None]:
    """Yield ``(method, path, operation)`` for every operation in a spec.

    Args:
        spec: Parsed OpenAPI document.

    Yields:
        Tuples of lowercase HTTP method, path template and the operation
        object.

    """
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            yield method, path, operation


def _lookup_ref(spec: dict[str, Any], ref: str) -> Any | None:
    """Return the object a local ``#/...`` reference points to, if any."""
    if not ref.startswith("#/"):
        return None
    node: Any = spec
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def resolve_refs(
    node: Any,
    spec: dict[str, Any],
    *,
    _depth: int = 0,
    _seen: frozenset[str] = frozenset(),
) -> Any:
    """Inline local ``$ref`` references into a schema fragment.

    Resolution is bounded to :data:`MAX_REF_DEPTH` nested reference hops
    and is cycle-safe: a reference that is already being expanded is left
    in place as ``{"$ref": ...}`` instead of recursing forever. Sibling
    keys next to a ``$ref`` (such as ``description``) are kept on the
    inlined result. External references are returned unchanged.

    Args:
        node: Schema fragment (dict, list or scalar) to resolve.
        spec: Full OpenAPI document holding ``components``.

    Returns:
        The fragment with local references inlined.

    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            target = _lookup_ref(spec, ref)
            if target is None or ref in _seen or _depth >= MAX_REF_DEPTH:
                return node
            resolved = resolve_refs(
                target,
                spec,
                _depth=_depth + 1,
                _seen=_seen | {ref},
            )
            extras = {
                key: value
                for key, value in node.items()
                if key != "$ref"
            }
            if extras and isinstance(resolved, dict):
                resolved = {**resolved, **extras}
            return resolved
        return {
            key: resolve_refs(value, spec, _depth=_depth, _seen=_seen)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [
            resolve_refs(item, spec, _depth=_depth, _seen=_seen)
            for item in node
        ]
    return node
