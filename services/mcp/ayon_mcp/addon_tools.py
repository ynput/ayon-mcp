"""Addon tool execution."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from .addon_discovery import allowed_endpoint_prefix, is_within_prefix
from .rest_client import get_global_rest_client

if TYPE_CHECKING:
    from .addon_discovery import AddonTool
    from .rest_client import RestApiClient

logger = logging.getLogger(__name__)
_PLACEHOLDER = re.compile(r"\{([^{}/]+)\}")


class AddonToolError(RuntimeError):
    """An addon tool call failed before or at the addon endpoint."""


def _resolve_path(tool: AddonTool, arguments: dict[str, Any]) -> str:
    """Fill ``{param}`` placeholders, consuming those arguments.

    A ``None`` argument counts as missing rather than becoming ``"None"``.

    Returns:
        The resolved request path, still under the addon prefix.

    Raises:
        AddonToolError: If a placeholder has no argument or the resolved
            path leaves the addon prefix.

    """
    path = tool.endpoint.path
    for key, value in list(arguments.items()):
        placeholder = f"{{{key}}}"
        if placeholder in path:
            del arguments[key]
            if value is not None:
                path = path.replace(placeholder, quote(str(value), safe=""))

    unresolved = _PLACEHOLDER.findall(path)
    if unresolved:
        msg = f"Missing required path argument(s): {', '.join(unresolved)}"
        raise AddonToolError(msg)

    prefix = allowed_endpoint_prefix(tool.addon_name, tool.addon_version)
    if not is_within_prefix(path, prefix):
        msg = f"Resolved path {path!r} is outside the addon prefix {prefix!r}."
        raise AddonToolError(msg)
    return path


async def call_addon_tool(
    tool: AddonTool,
    arguments: dict[str, Any],
    client: RestApiClient | None = None,
) -> Any:  # ruff: ignore[any-type]
    """Call an addon tool's endpoint with the caller's credentials.

    Path placeholders are filled from ``arguments``; the remaining
    arguments go to the query string for GET (``None`` values dropped,
    as AYON rejects an empty ``?limit=``) and to the JSON body otherwise.

    Args:
        tool: The discovered addon tool.
        arguments: Tool arguments from the MCP call.
        client: Optional REST client. When omitted, the process-global
            client is resolved per call so the request-scoped API key
            (or ``x-as-user`` in tunnel mode) set by the auth middleware
            is used and the addon's own ACL applies.

    Returns:
        The parsed endpoint response.

    Raises:
        AddonToolError: If the path cannot be resolved, the endpoint
            returns an error status or the request fails.

    """
    arguments = dict(arguments)
    method = tool.endpoint.method
    path = _resolve_path(tool, arguments)

    client = client or get_global_rest_client()
    try:
        if method == "GET":
            # Same as the generated REST tools' query parameters.
            params = {k: v for k, v in arguments.items() if v is not None}
            return await client.request(
                method=method, path=path, params=params or None
            )
        # An empty object, not None: a body model with only optional
        # fields still requires a body, otherwise the endpoint returns 422.
        return await client.request(method=method, path=path, json=arguments)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500]
        logger.warning(
            "Addon tool %s returned %s: %s",
            tool.full_name,
            exc.response.status_code,
            detail,
        )
        msg = f"Addon endpoint returned {exc.response.status_code}: {detail}"
        raise AddonToolError(msg) from exc
    except (httpx.HTTPError, ValueError) as exc:
        # RestApiClient raises ValueError for malformed JSON bodies.
        logger.warning(
            "Addon tool %s request failed: %s", tool.full_name, exc
        )
        msg = f"Addon tool request failed: {exc}"
        raise AddonToolError(msg) from exc
