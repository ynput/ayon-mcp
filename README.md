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
| `AYON_MCP_READ_ONLY` | Set to `true` to expose only read tools (optional) |

The assistant inherits the permissions of that user — use a restricted
user if you only want read access.

### Read-only mode

With `AYON_MCP_READ_ONLY=true` (accepted truthy values: `1`, `true`,
`yes`, `on`), write tools are not registered and the REST gateway
(`call_rest_endpoint`) accepts only GET and HEAD requests. When running
as an AYON service, the same toggle is available in the addon settings
(Studio Settings → AYON MCP server → Read-only mode); the environment
variable takes precedence when both are set.

## Usage

## Remote MCP server as AYON service
You can quickly setup MCP server for remote connections by using it as AYON service:

1) Install ayon-mcp as AYON addon (build it if necesseary `python -X dev ./create_package.py`)
2) Setup the service - in AYON, go the Services (`V+V`) > New Service, select your host, pick *AYON MCP Server* addon, select the version. Take care for selecting proper port (the default one *8088* might be in use already). If you change the port, set also environment variable `AYON_MCP_PORT` so the service can configure it properly.

Once done, service will start and you can add MCP server as remote server. See examples below.

> [!NOTE]
> Right now, the ash responsible for running services is not exposing the ports.
> You need this PR

## VSCode (and derivates)
You can manually configure MCP servers by editing the `mcp.json` file. There are two locations for this file:

**Workspace:** create or open `.vscode/mcp.json` in your project. Include this file in source control to share MCP server configurations with your team.
**User profile:** run the MCP: Open User Configuration command to open the `mcp.json` file in your user profile folder. Servers configured here are available across all your workspaces. When you use multiple profiles, each profile can have its own MCP server configuration.
You can also run MCP: Add Server in the Command Palette (`Ctrl+Shift+P`) to add a server through a guided flow, choosing either Workspace or Global as the target.

### Local (stdio)

Add following:
```json
{
  "servers": {
    "ayon-mcp": {
      "type": "stdio",
				"command": "pwsh",
				"args": [
				"-NoLogo", "-NonInteractive", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        "/path/to/ayon-mcp/scripts/start_local.ps1",
				"--api-key", "${api_key}",
        "--host", "http://localhost:5000"
      ],
      "env": {
      }
    }
  }
}
```
On Linux and macOS, use `bash` with `scripts/start_local.sh` instead.

### Remote (http)

For remote server (replace host - it can be localhost in case you are running it locally in a container):
```json
{
	"servers": {
		"ayon-mcp-remote": {
			"url": "http://host:8088/mcp",
			"headers": {
				"x-api-key": "<your-api-key-or-env-var>"
			},
			"type": "http"
		}
	},
	"inputs": []
}
```

On Linux and macOS, use `bash` with `scripts/start_local.sh` instead.

### Claude Code

#### Local (stdio)

```sh
claude mcp add ayon-mcp \
  -e AYON_SERVER_URL=http://localhost:5000 \
  -e AYON_API_KEY=your-api-key \
  -- pwsh "path/to/ayon-mcp-repo/scripts/start_local.ps1"
```


### Remote (http)

```sh
claude mcp add --transport http ayon-mcp \
  -e AYON_SERVER_URL=http://localhost:5000 \
  -e AYON_API_KEY=your-api-key \
  -- pwsh "path/to/ayon-mcp-repo/scripts/start_local.ps1"
```
On Linux and macOS, use `bash` with `scripts/start_local.sh` instead.

### Claude Desktop

For Claude Desktop follow [this guide](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop).

### Streamable HTTP

You can run docker container as a AYON service or locally.
The MCP endpoint is then served at `http://<host>:8088/mcp`.
The port can be changed using environment variable `AYON_MCP_PORT`


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

List tools are paginated: responses carry `count`, `truncated` and
`next_offset` fields; pass `next_offset` back as `offset` to fetch the
next page.

Anything not covered by a dedicated tool is reachable through the REST
gateway (`list_rest_endpoints`, `get_rest_endpoint`,
`call_rest_endpoint`), which discovers endpoints from the server's
OpenAPI spec at runtime (`services/mcp/ayon_openapi.json` is the bundled
fallback used when the server cannot be reached).

## Development and tests

### Running tests
To run integration test (requiring connection to the server), either set
`AYON_SERVER_URL` and `AYON_API_KEY` or create `.env` file in `./tests`.
Once set, the integration tests will run. There is also pytest mark *integration*.

```sh
uv sync
uv run pytest
```

## Notes
With local / stdio mode, the output can be cluttered by messages coming from your shell profile
init - you might want to mitigate that to reduce amount of warnings from your agent console. For
example how to do it with powershell, follow the example of adding MCP server to VSCode.
