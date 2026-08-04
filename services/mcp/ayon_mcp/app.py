"""Entrypoint for dockerized MCP server."""
import os
from collections.abc import Callable, Iterable

from fastmcp import FastMCP

from .server import run_remote
from .tools import ALL_TOOLS

server_url = os.getenv("AYON_SERVER_URL")
api_key = os.getenv("AYON_API_KEY")


def register_tools(server: FastMCP, tools: Iterable[Callable]) -> None:
    """Registers a sequence of callable tools with the FastMCP instance."""
    for tool in tools:
        server.tool()(tool)


if not server_url or not api_key:

    msg = (
        "AYON_SERVER_URL and AYON_API_KEY environment variables must be set. "
    )
    raise RuntimeError(
        msg
    )

# 1. Create FastMCP instance and register tools
mcp = run_remote(server_url, api_key)

# 2. Attach tools dynamically/flexibly
register_tools(mcp, ALL_TOOLS)

# 3. Expose FastMCP HTTP app for uvicorn
app = mcp.http_app()
