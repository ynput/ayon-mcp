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

## VSCode
You can manually configure MCP servers by editing the mcp.json file. There are two locations for this file:

Workspace: create or open .vscode/mcp.json in your project. Include this file in source control to share MCP server configurations with your team.
User profile: run the MCP: Open User Configuration command to open the mcp.json file in your user profile folder. Servers configured here are available across all your workspaces. When you use multiple profiles, each profile can have its own MCP server configuration.
You can also run MCP: Add Server in the Command Palette (Ctrl+Shift+P) to add a server through a guided flow, choosing either Workspace or Global as the target.

Add following:
```json
{
  "servers": {
    "ayon-mcp": {
      "type": "stdio",
      "command": "powershell",
      "args": [
        "path/to/ayon-mcp-repo/scripts/start_local.ps1",
        "--api-key", "${ayon_api_key}",
				"--host", "${ayon_server}$",
				"--port", "${ayon_port}"
      ],
      "env": {

      }
    }
  }
}
```

### Claude Code

```sh
claude mcp add ayon-mcp \
  -e AYON_SERVER_URL=http://localhost:5000 \
  -e AYON_API_KEY=your-api-key \
  -- powershell "path/to/ayon-mcp-repo/scripts/start_local.ps1"
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
