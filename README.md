# ayon-mcp

MCP ([Model Context Protocol](https://modelcontextprotocol.io)) server for
the [AYON](https://ynput.io/ayon/) pipeline platform. It lets AI assistants
like Claude browse and operate an AYON server: projects, folder hierarchies,
tasks, publishes, the event stream, and addon settings.

## Requirements

- Python 3.10+ (or [uv](https://docs.astral.sh/uv/), which handles Python for you)
- A reachable AYON server and an API key
  (user profile → API keys, or a service user key)

## Configuration

Two environment variables (or the matching CLI flags):

| Variable | Meaning |
| --- | --- |
| `AYON_SERVER_URL` | e.g. `https://ayon.mystudio.com` or `http://localhost:5001` |
| `AYON_API_KEY` | API key of the user the assistant acts as |

The assistant inherits the permissions of that user — use a restricted
user if you only want read access.

## Usage

### Claude Code

```sh
claude mcp add ayon \
  -e AYON_SERVER_URL=http://localhost:5001 \
  -e AYON_API_KEY=your-api-key \
  -- uv run --directory /path/to/ayon-mcp ayon-mcp
```

### Claude Desktop

```json
{
  "mcpServers": {
    "ayon": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/ayon-mcp", "ayon-mcp"],
      "env": {
        "AYON_SERVER_URL": "http://localhost:5001",
        "AYON_API_KEY": "your-api-key"
      }
    }
  }
}
```

### Streamable HTTP

For remote or shared deployments:

```sh
ayon-mcp --transport http --host 0.0.0.0 --port 8021
```

The MCP endpoint is then served at `http://<host>:8021/mcp`.

## Tools

| Area | Tools |
| --- | --- |
| Projects | `list_projects`, `get_project`, `get_server_info` |
| Entities (read) | `get_folder_hierarchy`, `list_folders`, `list_tasks`, `list_products`, `list_versions`, `list_representations`, `get_entity`, `query_graphql` |
| Entities (write) | `create_entity`, `update_entity`, `delete_entity`, `add_comment` |
| Events | `list_events`, `get_event`, `dispatch_event` |
| Settings | `list_addons`, `list_bundles`, `get_addon_settings`, `set_addon_settings` |

Write tools modify production data through the standard AYON operations
endpoint, so server-side validation, permissions and events all apply.

## Development

```sh
uv sync
uv run pytest
```

## License

Apache-2.0
