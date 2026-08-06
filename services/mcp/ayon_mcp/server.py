"""Command line entrypoint for the AYON MCP server."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, AsyncIterator

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

# from fastmcp.server.providers.openapi import MCPType, RouteMap
from .client import get_ayon_api, set_global_ayon_client
from .instructions import INSTRUCTIONS, OPENAPI_INSTRUCTIONS
from .rest_client import RestApiClient, set_global_rest_client
from .tools import ALL_TOOLS, openapi_tools_enabled

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    import mcp.types as mcp_types


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


def register_tools(server: FastMCP, tools: Iterable[Callable]) -> None:
    """Registers a sequence of callable tools with the FastMCP instance."""
    for tool in tools:
        server.tool()(tool)


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

    # mcp = FastMCP.from_openapi(
    #     openapi_spec=openapi_spec,
    #     validate_output=False,
    #     client=client,
    #     name="AYON MCP Server",
    #     instructions=INSTRUCTIONS,
    #     route_maps=semantic_maps
    # )

    instructions = INSTRUCTIONS if not openapi_tools_enabled() else OPENAPI_INSTRUCTIONS

    mcp = FastMCP(
        lifespan=server_lifespan,
        name="AYON MCP Server",
        instructions=instructions
    )

    register_tools(mcp, ALL_TOOLS)

    return mcp


def run_remote(base_url: str) -> FastMCP:
    """Run the MCP server with the given AYON server URL and API key.

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)

    Returns:
        FastMCP instance configured with the AYON OpenAPI spec.

    """
    mcp = create_mcp_server(base_url)
    mcp.add_middleware(RemoteApiKeyMiddleware(base_url))
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",  # ruff:ignore[hardcoded-bind-all-interfaces]
        port=int(os.getenv("AYON_MCP_PORT", "8088")),
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
    mcp.run()
    return mcp


if __name__ == "__main__":
    run_local(
        os.getenv("AYON_SERVER_URL", "http://localhost:5000"),
        os.getenv("AYON_API_KEY", "")
    )
