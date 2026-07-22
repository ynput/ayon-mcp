"""Utility functions for the MCP service."""
from typing import TYPE_CHECKING

from . import entities, events, projects, settings, write

if TYPE_CHECKING:
    import ayon_api
    from fastmcp import FastMCP


def register_tools(mcp: FastMCP, api: ayon_api.ServerAPI) -> None:
    """Register all tool modules on *mcp* using *api* for data access.

    Args:
        mcp: FastMCP instance to register tools on.
        api: Authenticated AYON API client to pass to each tool module.

    """
    for module in (entities, events, projects, settings, write):
        module.register(mcp, api)
