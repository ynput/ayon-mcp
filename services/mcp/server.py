"""Command line entrypoint for the AYON MCP server."""

from __future__ import annotations

import argparse
import os
from wsgiref import headers

import click
import httpx
from fastmcp import FastMCP
from fastmcp.experimental.server.openapi import MCPType, RouteMap


from services.mcp.app import mcp
import services.mcp.tools  # noqa: F401  (registers all tools)

base_url = os.getenv("AYON_SERVER_URL", "http://localhost:5000")
openapi_url = f"{base_url}/openapi.json"
_headers = {"x-api-key": os.getenv("AYON_API_KEY", "")}


command = click.Command()
command.add_argument("--transport", type=click.Choice(["stdio", "http"]), default="stdio")
command.add_argument("--host", default="127.0.0.1")
command.add_argument("--port", type=int, default=8021)
command.add_argument("--server-url", default=os.getenv("AYON_SERVER_URL"))
command.add_argument("--api-key", default=os.getenv("AYON_API_KEY"))


def main() -> None:

    # parse arguments


    client = httpx.Client(base_url=base_url)
    client.headers.update(_headers)

    openapi_spec = httpx.get(
        openapi_url, headers=_headers).raise_for_status().json()

    mcp = FastMCP.from_openapi(
        openapi_spec=openapi_spec,
        client=client,
        name="AYON MCP Server",
        route_maps=[
            RouteMap(mcp_type=MCPType.TOOL)
        ]
    )
    mcp.run()

def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="ayon-mcp",
        description=(
            "MCP server for the AYON pipeline platform. Requires "
            "AYON_SERVER_URL and AYON_API_KEY (environment variables "
            "or CLI options)."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio for local clients (default), http for streamable HTTP",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8021, help="HTTP bind port")
    parser.add_argument("--server-url", help="AYON server URL (overrides AYON_SERVER_URL)")
    parser.add_argument("--api-key", help="AYON API key (overrides AYON_API_KEY)")
    args = parser.parse_args()

    if args.server_url:
        os.environ["AYON_SERVER_URL"] = args.server_url
    if args.api_key:
        os.environ["AYON_API_KEY"] = args.api_key

    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
