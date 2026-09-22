"""Entrypoint for dockerized MCP server."""
import os

from .server import run_remote

server_url = os.getenv("AYON_SERVER_URL")
api_key = os.getenv("AYON_API_KEY")


if not server_url or not api_key:

    msg = (
        "AYON_SERVER_URL and AYON_API_KEY environment variables must be set. "
    )
    raise RuntimeError(
        msg
    )

# Create FastMCP instance and register tools
mcp = run_remote(server_url, api_key)
