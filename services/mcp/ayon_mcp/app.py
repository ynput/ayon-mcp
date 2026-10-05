"""Entrypoint for dockerized MCP server.

``AYON_MCP_TRANSPORT`` selects how the service is reached:

- ``http``: listen on ``AYON_MCP_PORT`` (8088).
- ``tunnel``: connect out to the MCP addon on the AYON server and serve
  its MCP endpoint (``/api/addons/mcp/{version}/mcp``, or ``/api/mcp``).

Unset, it is ``tunnel`` when the service runs under ASH and ``http``
otherwise (see ``select_transport``).
"""
import os

from .server import run_remote, run_tunnel
from .tunnel import select_transport

server_url = os.getenv("AYON_SERVER_URL")
api_key = os.getenv("AYON_API_KEY")
transport = select_transport()


if not server_url or not api_key:

    msg = (
        "AYON_SERVER_URL and AYON_API_KEY environment variables must be set. "
    )
    raise RuntimeError(
        msg
    )

# Create FastMCP instance and register tools
mcp = (
    run_tunnel(server_url, api_key)
    if transport == "tunnel"
    else run_remote(server_url, api_key)
)
