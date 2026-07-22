"""MCP tools registry.

Provide ayon python api to tools and register them on the MCP app.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import ayon_api

from . import tools as tools_pkg

if TYPE_CHECKING:
    from fastmcp import FastMCP


def get_ayon_api(server_url: str, api_key: str) -> ayon_api.ServerAPI:
    """Return an instance of the API client.

    Args:
        server_url: AYON server URL (e.g. http://localhost:5000)
        api_key: AYON API key

    Returns:
        An instance of the AYON API client.

    Raises:
        RuntimeError: If the API key is invalid or the server is unreachable.

    """
    server = ayon_api.ServerAPI(server_url, token=api_key)

    if not server.has_valid_token:
        msg = (
            f"Could not authenticate against AYON server at {server_url!r}. "
            "Check that AYON_API_KEY is a valid, non-expired API key "
            "and that the server is reachable."
        )
        raise RuntimeError(
            msg
        )

    return server


def register_tools(mcp: FastMCP, api: ayon_api.ServerAPI) -> None:
    """Register all tools on *mcp* using *api* for data access.

    Args:
        mcp: FastMCP instance to register tools on.
        api: Authenticated AYON API client.

    """
    tools_pkg.register_tools(mcp, api)
