"""Entrypoint for dockerized MCP server.

``AYON_MCP_TRANSPORT`` selects how the service is reached:

- ``http`` (default): listen on ``AYON_MCP_PORT`` (8088).
- ``tunnel``: connect out to the AYON server and serve its ``/api/mcp``
  endpoint (needs an AYON server with the MCP tunnel).
"""
import os

from .server import run_remote, run_tunnel

server_url = os.getenv("AYON_SERVER_URL")
api_key = os.getenv("AYON_API_KEY")
transport = os.getenv("AYON_MCP_TRANSPORT", "http").strip().lower()


if not server_url or not api_key:

    msg = (
        "AYON_SERVER_URL and AYON_API_KEY environment variables must be set. "
    )
    raise RuntimeError(
        msg
    )

if transport not in {"http", "tunnel"}:
    msg = f"AYON_MCP_TRANSPORT must be 'http' or 'tunnel', not {transport!r}"
    raise RuntimeError(msg)

# Create FastMCP instance and register tools
mcp = (
    run_tunnel(server_url, api_key)
    if transport == "tunnel"
    else run_remote(server_url, api_key)
)
