"""Command line entrypoint for the AYON MCP server."""

from __future__ import annotations

import os

import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap

from .instructions import INSTRUCTIONS
from .registry import get_ayon_api, register_tools


def create_mcp_server(base_url: str, api_key: str) -> FastMCP:
    """Run the MCP server with the given AYON server URL and API key.

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)
        api_key: AYON API key

    Returns:
        FastMCP instance configured with the AYON OpenAPI spec.

    """
    openapi_url = f"{base_url}/openapi.json"
    headers = {"x-api-key": api_key or os.getenv("AYON_API_KEY", "")}

    # parse arguments

    client = httpx.AsyncClient(base_url=base_url)
    client.headers.update(headers)

    openapi_spec = httpx.get(
        openapi_url, headers=headers).raise_for_status().json()

    semantic_maps = [
    # GET requests with path parameters become ResourceTemplates
    RouteMap(
        methods=["GET"],
        pattern=r".*\{.*\}.*",
        mcp_type=MCPType.RESOURCE_TEMPLATE
    ),
    # All other GET requests become Resources
    RouteMap(
        methods=["GET"],
        pattern=r".*",
        mcp_type=MCPType.RESOURCE
    ),
]

    mcp = FastMCP.from_openapi(
        openapi_spec=openapi_spec,
        client=client,
        name="AYON MCP Server",
        instructions=INSTRUCTIONS,
        route_maps=semantic_maps
    )

    api = get_ayon_api(base_url, api_key)
    register_tools(mcp, api)

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
