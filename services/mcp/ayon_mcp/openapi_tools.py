"""REST tools built at startup from the AYON server's OpenAPI spec.

``FastMCP.from_openapi`` is only the parser and request builder here: its
tools are wrapped so they run behind the same provider, policy and guards as
the curated tools. The server's live spec is preferred. The tracked
``ayon_openapi.json`` is the fallback when AYON can't be reached or serves an
unusable spec, so a bad spec never stops the MCP server from starting.
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.utilities.openapi import (
    HTTPRoute,  # ruff: ignore[typing-only-third-party-import]
)
from mcp.types import TextContent

from .rest_client import get_global_rest_client
from .rest_policy import HTTP_METHOD_ATTR, HTTP_PATH_ATTR

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


logger = logging.getLogger(__name__)

SPEC_PATH = Path(__file__).resolve().parents[1] / "ayon_openapi.json"
REST_TOOL_ATTR = "__ayon_rest_tool__"

# Oldest server this package supports, see ``ayon_server_version`` in the
# addon's ``package.py``. Package metadata is not checked when the server is
# started from the command line, so an older server is reported at startup.
MIN_SERVER_VERSION = (1, 17)

# Added to a REST tool name when a curated tool already uses it.
RESERVED_NAME_PREFIX = "openapi_"

# ``OpenAPITool.run`` copies the MCP caller's HTTP headers to the AYON
# request. Only these, and the headers the operation declares and the call
# sets, may reach AYON.
_TRANSPORT_HEADERS = frozenset({
    "accept",
    "accept-encoding",
    "connection",
    "content-length",
    "content-type",
    "host",
    "user-agent",
})
_CALL_HEADERS: ContextVar[frozenset[str]] = ContextVar(
    "rest_tool_call_headers", default=frozenset()
)


class _AyonTransport(httpx.AsyncBaseTransport):
    """Send tool requests through the shared client as the current caller.

    The tools are built before the server's lifespan creates that client,
    so it is resolved per request.
    """

    async def handle_async_request(  # ruff: ignore[no-self-use]
        self, request: httpx.Request
    ) -> httpx.Response:
        """Drop headers not allowed for this call and send the request.

        Returns:
            The streamed response.

        """
        allowed = _TRANSPORT_HEADERS | _CALL_HEADERS.get()
        for name in list(request.headers.keys()):
            if name.lower() not in allowed:
                del request.headers[name]
        return await get_global_rest_client().forward(request)


def _declared_headers(
    route: HTTPRoute, arguments: dict[str, Any]
) -> frozenset[str]:
    return frozenset(
        mapping["openapi_name"].lower()
        for argument, mapping in route.parameter_map.items()
        if mapping["location"] == "header"
        and arguments.get(argument) is not None
    )


class RestTool(Tool):
    """One AYON REST operation, usable in both tool exposure modes."""

    openapi_tool: Tool
    route: HTTPRoute
    namespace: str

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Call the operation, allowing only this call's own headers.

        Returns:
            The tool result built by FastMCP.

        """
        token = _CALL_HEADERS.set(_declared_headers(self.route, arguments))
        try:
            return await self.openapi_tool.run(arguments)
        finally:
            _CALL_HEADERS.reset(token)


def _result_value(result: ToolResult) -> Any:  # ruff: ignore[any-type]
    """Return the response body from a FastMCP tool result.

    FastMCP keeps JSON objects as they are and wraps any other JSON value
    as ``{"result": value}``. Non-JSON bodies end up as text content.

    Returns:
        The parsed JSON body, its text, or ``None`` for an empty body.

    """
    structured = result.structured_content
    if structured is not None:
        if structured.keys() == {"result"} and not isinstance(
            structured["result"], dict
        ):
            return structured["result"]
        return structured
    text = "".join(
        item.text for item in result.content if isinstance(item, TextContent)
    )
    return text or None


def _as_function(tool: RestTool) -> Callable[..., Any]:
    """Wrap a tool as a function for the tool selection and policy code.

    Returns:
        A function tagged with its endpoint and with the tool itself.

    """
    async def run(**arguments: Any) -> Any:  # ruff: ignore[any-type]
        return _result_value(await tool.run(arguments))

    run.__name__ = tool.name
    run.__doc__ = tool.description
    setattr(run, HTTP_METHOD_ATTR, tool.route.method.upper())
    setattr(run, HTTP_PATH_ATTR, tool.route.path)
    setattr(run, REST_TOOL_ATTR, tool)
    return run


def rest_tool_of(function: Callable[..., Any]) -> RestTool | None:
    """Return the REST tool behind a function from ``load_rest_tools``.

    Returns:
        The tool, or ``None`` for any other function.

    """
    return getattr(function, REST_TOOL_ATTR, None)


def _namespace(path: str) -> str:
    """Return the discovery namespace for an endpoint path.

    Returns:
        ``ayon.openapi.<first segment after /api>``. The prefix keeps REST
        tools apart from the curated ``ayon.<module>`` namespaces.

    """
    parts = [part for part in path.strip("/").split("/") if part]
    if parts and parts[0] == "api":
        parts = parts[1:]
    group = parts[0].strip("{}") if parts else "root"
    group = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", group)
    group = re.sub(r"[^a-zA-Z0-9]+", "_", group).strip("_").lower()
    return f"ayon.openapi.{group or 'root'}"


def _description(route: HTTPRoute) -> str:
    method = route.method.upper()
    parts = [route.summary or f"{method} {route.path}"]
    parts.append(f"Endpoint: {method} {route.path}")
    if route.description:
        parts.append(route.description)
    return "\n\n".join(part.strip() for part in parts)


def build_rest_tools(
    spec: dict[str, Any],
    base_url: str,
    reserved_names: Iterable[str] = (),
) -> list[Callable[..., Any]]:
    """Build a REST tool for every operation in an OpenAPI spec.

    Args:
        spec: The OpenAPI spec.
        base_url: AYON server URL the tools call.
        reserved_names: Names of curated tools. A REST tool with the same
            name gets ``RESERVED_NAME_PREFIX``, so both stay available.

    Returns:
        The tools, wrapped as functions.

    Raises:
        ValueError: If the spec is invalid or has no operations.

    """
    components: list[tuple[HTTPRoute, Any]] = []
    FastMCP.from_openapi(
        spec,
        client=httpx.AsyncClient(
            base_url=base_url, transport=_AyonTransport()
        ),
        route_maps=[RouteMap(mcp_type=MCPType.TOOL)],
        mcp_component_fn=lambda route, component: components.append(
            (route, component)
        ),
        # The MCP result only passes AYON's response through. A response
        # that doesn't match its schema is still the right answer to return.
        validate_output=False,
    )
    if not components:
        msg = "OpenAPI spec has no operations"
        raise ValueError(msg)

    reserved = set(reserved_names)
    functions = []
    for route, component in components:
        name = component.name
        if name in reserved:
            name = f"{RESERVED_NAME_PREFIX}{name}"
        functions.append(
            _as_function(
                RestTool(
                    name=name,
                    description=_description(route),
                    parameters=component.parameters,
                    tags=component.tags,
                    openapi_tool=component,
                    route=route,
                    namespace=_namespace(route.path),
                )
            )
        )
    return functions


def fetch_openapi_spec(
    base_url: str,
    api_key: str = "",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Fetch the server's OpenAPI spec.

    Args:
        base_url: AYON server URL.
        api_key: AYON API key, required by servers that protect the spec.
        timeout: Timeout in seconds.

    Returns:
        The parsed spec.

    Raises:
        TypeError: If the response is not a JSON object.

    """
    response = httpx.get(
        f"{base_url.rstrip('/')}/openapi.json",
        headers={"x-api-key": api_key} if api_key else None,
        timeout=timeout,
    )
    response.raise_for_status()
    spec = response.json()
    if not isinstance(spec, dict):
        msg = "OpenAPI payload must be a JSON object"
        raise TypeError(msg)
    return spec


def write_tracked_spec(spec: dict[str, Any]) -> None:
    """Replace the tracked fallback spec, in its stable sorted format."""
    SPEC_PATH.write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _warn_if_unsupported(spec: dict[str, Any], base_url: str) -> None:
    info = spec.get("info")
    version = info.get("version") if isinstance(info, dict) else None
    match = re.match(r"(\d+)\.(\d+)", str(version))
    if match and (int(match[1]), int(match[2])) < MIN_SERVER_VERSION:
        logger.warning(
            "AYON server at %s is version %s, this MCP server needs %s or "
            "newer. REST tools may have different names or fail.",
            base_url,
            version,
            ".".join(map(str, MIN_SERVER_VERSION)),
        )


def load_rest_tools(
    base_url: str,
    api_key: str = "",
    reserved_names: Iterable[str] = (),
) -> list[Callable[..., Any]]:
    """Return REST tools from the live spec, or else from the tracked one.

    Never raises: without any usable spec only the curated tools remain.

    Args:
        base_url: AYON server URL.
        api_key: AYON API key used to fetch the spec.
        reserved_names: Names of curated tools, see ``build_rest_tools``.

    Returns:
        The tools, wrapped as functions. Empty if no spec could be used.

    """
    reserved = set(reserved_names)
    try:
        spec = fetch_openapi_spec(base_url, api_key)
        functions = build_rest_tools(spec, base_url, reserved)
    except Exception as exc:  # ruff: ignore[blind-except]
        logger.warning(
            "Can't use the OpenAPI spec of %s, using the tracked %s. Its "
            "endpoints may differ from this server's: %r",
            base_url.rstrip("/"),
            SPEC_PATH.name,
            exc,
        )
    else:
        _warn_if_unsupported(spec, base_url)
        return functions

    try:
        tracked = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        return build_rest_tools(tracked, base_url, reserved)
    except Exception:
        logger.exception(
            "Can't use the tracked %s, REST tools are not available.",
            SPEC_PATH.name,
        )
        return []
