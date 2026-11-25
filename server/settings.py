"""Settings for the addon."""
from typing import Any

from ayon_server.config.serverconfig import get_server_config
from ayon_server.settings import BaseSettingsModel, SettingsField

DEFAULT_VALUES: dict[str, Any] = {}
ENDPOINT_PATH = "/api/mcp"


async def get_endpoint_url() -> str:
    """Return the MCP endpoint URL, or only its path if the URL is unknown.

    The server URL comes from the Global settings ("Server URL", AYON
    1.17.3 or newer), cached in Redis. It is not built from request
    headers, which the client controls.

    Returns:
        The endpoint URL, e.g. https://ayon.example.com/api/mcp.

    """
    config = await get_server_config()
    server_url = getattr(config, "server_url", "")  # AYON < 1.17.3
    return f"{server_url.rstrip('/')}{ENDPOINT_PATH}"


class MCPSettings(BaseSettingsModel):
    """Connect Claude, ChatGPT and other AI apps to AYON.

    Copy the **MCP endpoint** below into the app. If it shows only
    `/api/mcp`, set the Server URL in Studio settings → Server.

    - **claude.ai**: Settings → Connectors → Add custom connector.
    - **ChatGPT**: Settings → Apps & Connectors → Create (developer mode).

    The app then asks you to log in to AYON. Needs the OAuth addon and a
    server reachable from the internet over HTTPS. An administrator must
    allow each new app.

    **Apps without OAuth, or a server without the OAuth addon**: use the
    endpoint with your AYON API key in the `x-api-key` header. See the
    addon README.
    """

    # Read-only: AYON lists an addon in the settings only when its model
    # has a field, and this one shows where clients connect. The addon's
    # default settings fill in the server URL (see get_endpoint_url).
    endpoint_url: str = SettingsField(
        default=ENDPOINT_PATH,
        title="MCP endpoint",
        widget="label",
        scope=["studio"],
    )
