"""Shared FastMCP instance.

Tool modules import ``mcp`` from here and register themselves with the
``@mcp.tool()`` decorator at import time. :mod:`ayon_mcp.server` imports
the tool modules and runs the server.
"""

from mcp.server.fastmcp import FastMCP

INSTRUCTIONS = """\
Tools for the AYON pipeline platform (projects, folder hierarchies, tasks,
products, versions, representations, events, addon settings).

Typical workflow: call `list_projects` first, then `get_folder_hierarchy`
or the `list_*` tools scoped to a project. Entity ids are hex strings;
folder paths look like `/assets/characters/hero`. Write tools
(`create_entity`, `update_entity`, `delete_entity`) modify production
data — use them only when explicitly asked to change something.
"""

mcp = FastMCP("AYON", instructions=INSTRUCTIONS)
