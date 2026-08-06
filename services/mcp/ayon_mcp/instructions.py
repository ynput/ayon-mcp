"""Instructions for the MCP Server."""

INSTRUCTIONS = """\
Tools for the AYON pipeline platform (projects, folder hierarchies, tasks,
products, versions, representations, events, addon settings).

Typical workflow: call `list_projects` first, then `get_folder_hierarchy`
or the `list_*` tools scoped to a project. Entity ids are hex strings;
folder paths look like `/assets/characters/hero`. Write tools
(`create_entity`, `update_entity`, `delete_entity`) modify production
data — use them only when explicitly asked to change something.
"""


OPENAPI_INSTRUCTIONS = """\
Tools are exposed from AYON OpenAPI endpoints.

Use read endpoints first to gather context, then call write endpoints only
when explicitly asked to change data.

Typical workflow:
1. Start with project discovery (`list_projects` or project-list endpoints).
2. Inspect project scope (`get_project`, folder hierarchy, `list_*` endpoints).
3. Resolve entity identifiers (ids are hex strings; folder paths look like
   `/assets/characters/hero`).
4. Perform targeted mutations only after confirming exact project and entity.

Safety rules:
- Prefer GET/list/read operations before POST/PATCH/DELETE.
- Do not call destructive or mutating endpoints unless the user requested it.
- For bulk operations, verify scope and intent because changes can affect
  production data.
"""

