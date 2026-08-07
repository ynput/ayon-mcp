"""Command line entrypoint for the AYON MCP server."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, AsyncIterator

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from .client import get_ayon_api, set_global_ayon_client
from .instructions import INSTRUCTIONS, READ_ONLY_NOTE
from .rest_client import RestApiClient, set_global_rest_client
from .tools import ALL_TOOLS, TOOL_ANNOTATIONS
from .utils import read_only_enabled, set_read_only

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    import mcp.types as mcp_types

logger = logging.getLogger(__name__)


class RemoteApiKeyMiddleware(Middleware):
    """Resolve AYON API key from request headers for remote tool calls."""

    def __init__(self, base_url: str):
        """Initialize the middleware with the base URL.

        Args:
            base_url: The base URL of the AYON server.

        """
        self._base_url = base_url

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, object],
    ) -> object:
        """Inject request-scoped AYON client based on ``x-api-key`` header."""
        from .client import set_global_ayon_api_key

        headers = get_http_headers()
        request_api_key = (headers.get("x-api-key") or "").strip()
        if not request_api_key:
            msg = (
                "Missing required 'x-api-key' header in remote mode. "
                "Provide a valid AYON API key with each MCP request."
            )
            raise RuntimeError(msg)

        set_global_ayon_api_key(request_api_key)
        set_global_ayon_client(get_ayon_api(self._base_url, request_api_key))
        return await call_next(context)


class StaticApiKeyMiddleware(Middleware):
    """Use one configured AYON API key for local tool calls."""

    def __init__(self, base_url: str, api_key: str):
        self._base_url = base_url
        self._api_key = api_key
        self._client = None

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, object],
    ) -> object:
        """Inject a lazily initialized AYON client for local mode."""
        from .client import set_global_ayon_api_key

        if self._client is None:
            self._client = get_ayon_api(self._base_url, self._api_key)
        set_global_ayon_api_key(self._api_key)
        set_global_ayon_client(self._client)
        return await call_next(context)


def register_tools(
    server: FastMCP,
    tools: Iterable[Callable],
    *,
    read_only: bool = False,
) -> None:
    """Register callable tools with annotations on the FastMCP instance.

    Each tool is registered with its MCP annotations from
    ``TOOL_ANNOTATIONS`` (readOnlyHint, destructiveHint...). In
    read-only mode, tools that are not read-only are skipped — except
    `call_rest_endpoint`, which restricts itself to GET/HEAD at runtime
    and is therefore registered as read-only.

    Args:
        server: FastMCP instance to register the tools on.
        tools: Callables to register as tools.
        read_only: Skip write tools when True.

    """
    for tool in tools:
        annotations = dict(TOOL_ANNOTATIONS.get(tool.__name__, {}))
        if read_only and not annotations.get("readOnlyHint"):
            if tool.__name__ != "call_rest_endpoint":
                continue
            # The runtime guard limits the gateway to GET/HEAD.
            annotations.update(readOnlyHint=True, destructiveHint=False)
        server.tool(tool, annotations=annotations or None)


def create_mcp_server(base_url: str) -> FastMCP:
    """Create the MCP server with the given AYON server URL.

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)

    Returns:
        FastMCP instance configured with the AYON OpenAPI spec.

    """

    @lifespan
    async def server_lifespan(server: FastMCP) -> AsyncIterator[dict]:
        """FastMCP Lifespan manager to manage client state cleanly.

        Args:
            server: FastMCP instance.

        Yields:
            A dictionary containing the shared ApiClient instance.

        """
        client = RestApiClient(
            base_url=base_url
        )
        try:
            set_global_rest_client(client)
            # Pass to lifespan context
            yield {"api_client": client}
        finally:
            # Cleanup on shutdown
            await client.close()
            set_global_rest_client(None)

    read_only = read_only_enabled()
    instructions = INSTRUCTIONS + (READ_ONLY_NOTE if read_only else "")

    mcp = FastMCP(
        lifespan=server_lifespan,
        name="AYON MCP Server",
        instructions=instructions
    )

    register_tools(mcp, ALL_TOOLS, read_only=read_only)

    return mcp


def _fetch_addon_read_only_setting(base_url: str) -> bool | None:
    """Read the addon's `read_only` setting from the AYON server.

    Remote mode runs as an AYON service; ash injects the service
    credentials and addon identity into the environment. Returns None
    when the setting cannot be determined (missing env, unreachable
    server), leaving the environment variable in charge.

    Args:
        base_url: AYON server URL.

    Returns:
        The `read_only` setting value, or None when unavailable.

    """
    addon_name = os.getenv("AYON_ADDON_NAME", "mcp")
    addon_version = os.getenv("AYON_ADDON_VERSION", "")
    api_key = os.getenv("AYON_API_KEY", "")
    if not addon_version or not api_key:
        return None
    try:
        response = httpx.get(
            f"{base_url}/api/addons/{addon_name}/{addon_version}/settings",
            headers={"x-api-key": api_key},
            timeout=10.0,
        )
        response.raise_for_status()
        value = response.json().get("read_only")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Could not fetch addon settings: %s", exc)
        return None
    return value if isinstance(value, bool) else None


def run_remote(base_url: str) -> FastMCP:
    """Run the MCP server with the given AYON server URL and API key.

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)

    Returns:
        FastMCP instance configured with the AYON OpenAPI spec.

    """
    # The env var wins; otherwise the addon settings toggle applies.
    if "AYON_MCP_READ_ONLY" not in os.environ:
        set_read_only(_fetch_addon_read_only_setting(base_url))

    mcp = create_mcp_server(base_url)
    mcp.add_middleware(RemoteApiKeyMiddleware(base_url))
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",  # ruff:ignore[hardcoded-bind-all-interfaces]
        port=int(os.getenv("AYON_MCP_PORT", "8088")),
        show_banner=False
    )
    return mcp


def run_local(base_url: str, api_key: str) -> FastMCP:
    """Run the MCP server with the given AYON server URL and API key.

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)
        api_key: AYON API key

    Returns:
        FastMCP instance configured with the AYON OpenAPI spec.

    """
    mcp = create_mcp_server(base_url)
    mcp.add_middleware(
        StaticApiKeyMiddleware(base_url, api_key or os.getenv("AYON_API_KEY", ""))
    )
    mcp.run(show_banner=False)
    return mcp


if __name__ == "__main__":
    run_local(
        os.getenv("AYON_SERVER_URL", "http://localhost:5000"),
        os.getenv("AYON_API_KEY", "")
    )
