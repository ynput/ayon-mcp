"""Instructions for the MCP Server."""

INSTRUCTIONS = """\
Tools for the AYON pipeline platform (projects, folder hierarchies, tasks,
products, versions, representations, events, addon settings).

Typical workflow: call `list_projects` first, then `get_folder_hierarchy`
or the `list_*` tools scoped to a project. Entity ids are hex strings;
folder paths look like `/assets/characters/hero`. Write tools
(`create_entity`, `update_entity`, `delete_entity`) modify production
data — use them only when explicitly asked to change something.

Attribute descriptions (`set_attribute_description`) are studio-wide.
Never invent a description from the attribute name: ask the user what the
attribute means and save their wording. If one already exists, show it and
overwrite only after the user agrees.
"""


OPENAPI_INSTRUCTIONS = """\
AYON tools are discovered on demand to keep the initial tool surface compact.

Use `search_ayon_tools` to find an operation, then `get_ayon_tool_schema`
before calling `call_ayon_tool`. Write operations require
`confirm_mutation=true` only after the user explicitly authorized the change.

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
- Never invent an attribute description from the attribute name. Ask the
  user what it means, save it with `set_attribute_description`, and pass
  `overwrite=true` only after the user approved replacing the current text.
"""

READ_ONLY_INSTRUCTIONS = """\
This server runs in read-only mode (AYON_MCP_READ_ONLY): no tool that
changes AYON data is available. If the user asks for a change, explain that
it can't be made through this server instead of looking for a workaround.
"""
