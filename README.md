# AYON MCP Server

MCP ([Model Context Protocol](https://modelcontextprotocol.io)) server for
the [AYON](https://ynput.io/ayon/) pipeline platform. It lets AI assistants
like Claude browse and operate an AYON server: projects, folder hierarchies,
tasks, publishes, the event stream, and addon settings.

> [!WARNING]
> Not recommended for production use. An LLM-driven assistant can call the
> wrong tool, or call the right tool with wrong arguments, and write tools
> go through AYON's operations endpoint with real permissions — mistakes can
> mutate or delete production data. Use a restricted API key, review
> mutations before confirming, and prefer a non-production server until
> you've validated the behavior for your workflow.

## Requirements

- Python 3.10+ (or [uv](https://docs.astral.sh/uv/), which handles Python for you)
- A reachable AYON server and an API key
  (user profile → API keys, or a service user key)

## Usage

## Remote MCP server as AYON service
You can quickly setup MCP server for remote connections by using it as AYON service:

1) Install ayon-mcp as an AYON addon (build it if necessary: `python -X dev ./create_package.py`)
2) Set up the service - in AYON, go to Services (`V+V`) > New Service, select your host, pick the *AYON MCP Server* addon, and select the version.

Once done, service will start and you can add MCP server as remote server. See examples below.

> [!NOTE]
> When running MCP as an AYON service, you can use `https://<server_name>/api/mcp` as your stable URL in
> client configuration. When running as standalone server (using containers or directly), you need accessible
> host ip address and port exposed. For manual setup using tunnel mode, see Technical details below.

<details>
<summary>
Technical details - tunnel mode
</summary>
### Through the AYON server (tunnel mode)

The MCP addon serves the MCP endpoint on the AYON server, and the service answers it
through a WebSocket tunnel. A service run by ASH (the AYON Services page) uses tunnel mode
by default; elsewhere set `AYON_MCP_TRANSPORT=tunnel` or run `ayon-mcp --tunnel`. To keep
an ASH service listening on a port instead, set `AYON_MCP_TRANSPORT=http` on it. The
service then opens no port: it
connects out to the addon's WebSocket (`/api/addons/mcp/<version>/ws`), and MCP clients
use the AYON server itself:

```
https://<ayon-server>/api/mcp
```

`/api/mcp` redirects (307) to the MCP addon of the production bundle,
`/api/addons/mcp/<version>/mcp`, so client configuration survives addon updates. It needs
an AYON server with that redirect; on older servers use the versioned URL directly.

The addon forwards plain HTTP through the tunnel and streams responses back
chunk by chunk, so SSE responses (progress notifications) are not buffered.

- Clients authenticate against AYON as usual (`x-api-key` or `Authorization` header).
  Unauthenticated requests never reach the service, and neither do the caller's credentials:
  the addon passes only the authenticated user name. The service calls AYON with its own
  `AYON_API_KEY` and `x-as-user: <user>`, so the caller's permissions apply.
- `AYON_API_KEY` must belong to a service user. Only service users can open the tunnel.
- Tunnel URL: `AYON_MCP_TUNNEL_URL` if set; otherwise the addon given by
  `AYON_ADDON_NAME`/`AYON_ADDON_VERSION` (ASH sets these for services it runs); otherwise
  the MCP addon of the production bundle, looked up when the service starts.
- Each addon version has its own endpoint and tunnels, so production and staging services
  don't mix.
- The MCP app runs stateless, so several service replicas can connect at once.
  The addon prefers a tunnel held by the worker serving the request and otherwise routes
  through Redis to the worker holding one, so any number of server workers and replicas work.
- Responses are flow controlled per request: a client that reads slowly holds back only its
  own response, and server memory per request stays bounded (about 1 MiB).
- If the service can't open the tunnel it logs why once and keeps retrying every ~30 s:
  wrong or non-service `AYON_API_KEY`, a tunnel protocol version mismatch between service
  and addon (run the service of the addon's version), or no MCP addon of that version on
  the server (or use `AYON_MCP_TRANSPORT=http`).
</details>


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

For remote server (replace `server_name` - it can be localhost in case you are running it locally in a container).
See above for differences between running it as AYON service, standalone server or local mode.
```json
{
	"servers": {
		"ayon-mcp-remote": {
			"url": "https://<server_name>/api/mcp",
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

See above for differences between running it as AYON service, standalone server or local mode. 

```sh
claude mcp add --transport http ayon-mcp-remote \
  https://<server_name>/api/mcp \
  --header "x-api-key: your-api-key"
```

### Claude Desktop

Claude Desktop only supports local (stdio) MCP servers configured through its
`claude_desktop_config.json` — see
[this guide](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)
for the file's location on your platform. Add:

```json
{
  "mcpServers": {
    "ayon-mcp": {
      "command": "pwsh",
      "args": [
        "-NoLogo", "-NonInteractive", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        "/path/to/ayon-mcp/scripts/start_local.ps1",
        "--api-key", "your-api-key",
        "--host", "http://localhost:5000"
      ]
    }
  }
}
```

On Linux and macOS, use `bash` with `scripts/start_local.sh` instead.

### Streamable HTTP

You can run docker container as a AYON service or locally.
The MCP endpoint is then served at `http://<host>:8088/mcp`.
The port can be changed using environment variable `AYON_MCP_PORT`


## Advanced Configuration

Environment variables (or the matching CLI flags):

| Variable | Meaning |
| --- | --- |
| `AYON_SERVER_URL` | e.g. `https://ayon.mystudio.com` or `http://localhost:5000` |
| `AYON_API_KEY` | API key of the user the assistant acts as |
| `AYON_MCP_TOOL_EXPOSURE` | Set direct-tool surface (tools are directly exposed) |
| `AYON_MCP_ENABLE_OPENAPI_TOOLS` | set to `false` to disable all OpenAPI generated tools |
| `AYON_MCP_PORT` | control port the MCP service will listen on |
| `AYON_MCP_TRANSPORT` | `http` or `tunnel` for remote mode (default: `tunnel` under ASH, otherwise `http`) |

Telemetry:
| Variable | Meaning |
| --- | --- |
| `AYON_MCP_OTEL_ENABLED` | Set to `true` to enable OpenTelemetry (default: `false`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP endpoint for metrics/logs |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | OTLP endpoint for traces  |

The assistant inherits the permissions of that user — use a restricted
user if you only want read access.

For the Docker service, set `AYON_MCP_OTEL_ENABLED=true` to export traces,
metrics, and logs to the configured OTLP endpoints. OpenTelemetry is disabled
by default.


## Tools

By default, the server exposes five discovery tools instead of every AYON
operation schema. Use `search_ayon_tools` to find a capability,
`get_ayon_tool_schema` to inspect its arguments, and `call_ayon_tool` to run
it. This keeps the initial MCP tool schema payload small even when OpenAPI
generation produces many endpoints. State-changing tools require
`confirm_mutation=true` only after the user explicitly authorizes the change.

Set `AYON_MCP_TOOL_EXPOSURE=direct` to restore the legacy direct-tool surface
for compatibility.

| Area | Tools |
| --- | --- |
| Projects | `list_projects`, `get_project`, `get_server_info` |
| Entities (read) | `get_folder_hierarchy`, `list_folders`, `list_tasks`, `list_products`, `list_versions`, `list_representations`, `get_entity`, `query_graphql` |
| Entities (write) | `create_entity`, `update_entity`, `delete_entity`, `add_comment` |
| Events | `list_events`, `get_event`, `dispatch_event` |
| Settings | `list_addons`, `list_bundles`, `get_addon_settings`, `set_addon_settings` |

Write tools modify production data through the standard AYON operations
endpoint, so server-side validation, permissions and events all apply.

### Generated OpenAPI Tools

This repository can expose an additional auto-generated tool layer from
`services/mcp/ayon_openapi.json`.

- Generated code location (ignored by Git):
  `services/mcp/ayon_mcp/tools/openapi_generated/`
- Generator script:
  `services/mcp/scripts/generate_openapi_tools.py`
- Transport client used by generated tools:
  `RestApiClient` via `services/mcp/ayon_mcp/rest_client.py`

Generated OpenAPI tools are **enabled by default** and are available through
the discovery tools. They are registered directly only when
`AYON_MCP_TOOL_EXPOSURE=direct` is set.

On server startup, AYON MCP now:

1. Fetches `<AYON_SERVER_URL>/openapi.json`
2. Computes a deterministic hash of the fetched spec
3. Compares it with the local `services/mcp/ayon_openapi.json` hash
4. Regenerates `openapi_generated` only when the spec changed (or generated files are missing)

To disable generated OpenAPI tools, set:

- `AYON_MCP_ENABLE_OPENAPI_TOOLS=false`

Accepted falsey values are: `0`, `false`, `no`, `off` (case-insensitive).

You can still manually regenerate from the local spec file when needed:

```sh
uv run python ./services/mcp/scripts/generate_openapi_tools.py
```

## Development and tests

MCP clients use ``/api/addons/mcp/{version}/mcp`` (or the server's
``/api/mcp``, which redirects to the production version).
        """
Each addon version has its own hub and Redis keys, so production
and staging services each serve their own version's endpoint.


### Running tests
Test dependencies (pytest, pytest-ayon, dotenv, ...) live in the `test`
dependency group and are not installed by a plain `uv sync`. Install them
with:

```sh
uv sync --group test
```

To run integration tests (requiring connection to the server), either set
`AYON_SERVER_URL` and `AYON_API_KEY` or create `.env` file in `./tests`.
Once set, the integration tests will run. There is also pytest mark *integration*.

```sh
uv run pytest
```

### LLM agent tests

`tests/test_llm_agent.py` drives the MCP server through a real local model
(via [Ollama](https://ollama.com)) to check that it can discover and call
AYON tools correctly. These are marked `llm` and **disabled by default**
(`addopts` deselects `-m "not llm"`) since they need Ollama running with a
tool-calling model pulled.

Setup:

```sh
# installs Ollama if missing, starts it, and pulls the default model (qwen2.5:7b)
./scripts/setup_ollama.ps1   # or scripts/setup_ollama.sh on Linux/macOS
```

Run just the LLM tests:

```sh
uv run pytest -m llm tests/test_llm_agent.py
```

Configure via environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API base URL |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Model tag to drive the agent with |

If Ollama isn't reachable or the model isn't pulled, the tests skip
themselves rather than fail. Each scenario's outcome (checks, tool calls,
token/latency metrics) is written to `tests/reports/`.

#### Claude instead of a local model

`tests/test_llm_agent_claude.py` runs the same scenarios against the real
Anthropic API instead of Ollama - useful to compare a small local model
against Claude, at the cost of real API credits per run. Also marked `llm`
and disabled by default.

```sh
ANTHROPIC_API_KEY=sk-ant-... uv run pytest -m llm tests/test_llm_agent_claude.py
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Required; tests skip themselves if unset |
| `ANTHROPIC_MODEL` | `claude-opus-5` | Model id to drive the agent with |

The `OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
telemetry options below apply here too.

#### Optional: telemetry

When an OTLP collector is reachable at `OTEL_EXPORTER_OTLP_ENDPOINT`, the
spawned MCP server is launched through `opentelemetry-instrument` so its
real traces/metrics show up there too (mirrors the split the Dockerfile
uses: metrics/logs go through Vector's OTLP source, traces go straight to
Tempo's OTLP receiver, since Vector can't round-trip OTLP trace framing):

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:4319 \
uv run pytest -m llm tests/test_llm_agent.py
```

Point these at wherever your Vector/Tempo (or other OTLP-compatible)
collector's endpoints are published on the host.

For example telemetry stack, see ayon-vector repo.


### Benchmarking the LLM agent

Run the LLM scenarios repeatedly and aggregate their efficiency metrics:

```sh
uv run python scripts/benchmark_llm_agent.py --runs 5
```

The benchmark continues after a failing pytest run. It writes each raw report
and an aggregate summary to `tests/reports/benchmarks/`. The aggregate reports
average pass rate, turns, prompt tokens, completion tokens, and wall time per
scenario. Prompt tokens are cumulative across all model turns in a scenario,
so a run that needs more tool-calling turns will cost more.

## Notes
With local / stdio mode, the output can be cluttered by messages coming from your shell profile
init - you might want to mitigate that to reduce amount of warnings from your agent console. For
example how to do it with powershell, follow the example of adding MCP server to VSCode.
