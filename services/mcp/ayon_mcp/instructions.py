"""Instructions for the MCP Server."""

INSTRUCTIONS = """\
Tools for the AYON pipeline platform (projects, folder hierarchies, tasks,
products, versions, representations, events, addon settings).

Typical workflow: call `list_projects` first, then `get_folder_hierarchy`
or the `list_*` tools scoped to a project. Entity ids are hex strings;
folder paths look like `/assets/characters/hero`. Write tools
(`create_entity`, `update_entity`, `delete_entity`) modify production
data — use them only when explicitly asked to change something.

List tools are paginated: when a response has `truncated: true`, call
the tool again with `offset` set to the returned `next_offset` to fetch
the next page.

Knowledge tools tell you what is valid before you act: `get_documentation`
explains AYON concepts (call it with no arguments for the topic list),
`get_project_anatomy` returns a project's valid folder/task types,
statuses and tags, `list_attributes` the typed attribute definitions,
`get_graphql_schema` the GraphQL schema for `query_graphql`, and
`get_addon_settings_schema` the JSON schema behind addon settings.
Consult them instead of guessing values.

Anything not covered by a dedicated tool is reachable through the REST
gateway: `list_rest_endpoints` to discover endpoints,
`get_rest_endpoint` to inspect one endpoint's parameters and schemas,
and `call_rest_endpoint` to execute it. Prefer dedicated tools when they
cover the task, and treat POST/PUT/PATCH/DELETE endpoints as write
operations — call them only when explicitly asked to change something.
"""


READ_ONLY_NOTE = """
This server runs in READ-ONLY mode: write tools are not available and
the REST gateway only accepts GET and HEAD requests. When asked to
modify data, explain that read-only mode is enabled instead of trying
workarounds.
"""
