"""Command line entrypoint for the AYON MCP server."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

import httpx
from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from fastmcp.server.providers.openapi import MCPType, RouteMap

from .instructions import INSTRUCTIONS
from .tools import ALL_TOOLS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


class RestApiClient:
    """Wrapped shared HTTP client for server interaction."""

    def __init__(self, base_url: str, api_key: str):
        """Initialize the RestApiClient with base URL and API key."""
        self.http_client = httpx.AsyncClient(
            base_url=base_url,
            headers={"x-api-key": api_key or os.getenv("AYON_API_KEY", "")},
            timeout=30.0,
        )

    async def close(self):
        """Close the underlying HTTP client."""
        await self.http_client.aclose()


def register_tools(server: FastMCP, tools: Iterable[Callable]) -> None:
    """Registers a sequence of callable tools with the FastMCP instance."""
    for tool in tools:
        server.tool()(tool)


def create_mcp_server(base_url: str, api_key: str) -> FastMCP:
    """Run the MCP server with the given AYON server URL and API key.

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)
        api_key: AYON API key

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
        # Initialize shared client on startup
        client = RestApiClient(
            base_url=base_url,
            api_key=api_key or os.getenv("AYON_API_KEY", "")
        )
        try:
            # Pass to lifespan context
            yield {"api_client": client}
        finally:
            # Cleanup on shutdown
            await client.close()


    # mcp = FastMCP.from_openapi(
    #     openapi_spec=openapi_spec,
    #     validate_output=False,
    #     client=client,
    #     name="AYON MCP Server",
    #     instructions=INSTRUCTIONS,
    #     route_maps=semantic_maps
    # )

    mcp = FastMCP(
        lifespan=server_lifespan,
        name="AYON MCP Server",
        instructions=INSTRUCTIONS,
    )

    register_tools(mcp, ALL_TOOLS)

    return mcp


def run_remote(base_url: str, api_key: str) -> FastMCP:
    """Run the MCP server with the given AYON server URL and API key.

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)
        api_key: AYON API key

    Returns:
        FastMCP instance configured with the AYON OpenAPI spec.

    """
    mcp = create_mcp_server(base_url, api_key)
    register_tools(mcp, ALL_TOOLS)
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(
                base_url.rsplit(":", maxsplit=1)[-1]
            ) if ":" in base_url else 5000
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
    mcp = create_mcp_server(base_url, api_key)
    mcp.run()
    return mcp


if __name__ == "__main__":
    run_local(
        os.getenv("AYON_SERVER_URL", "http://localhost:5000"),
        os.getenv("AYON_API_KEY", "")
    )
