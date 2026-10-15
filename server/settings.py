"""Settings for the addon."""
from typing import Any

from ayon_server.settings import BaseSettingsModel, SettingsField

DEFAULT_VALUES: dict[str, Any] = {}


class ApiKeyActionSettings(BaseSettingsModel):
    """The "Create my MCP key" project action."""

    enabled: bool = SettingsField(
        default=True,
        title="Enabled",
    )
    projects: list[str] = SettingsField(
        default_factory=list,
        title="Projects",
        description=(
            "Projects whose overview page shows the action. "
            "Empty: every project."
        ),
    )
    allowed_email_domains: list[str] = SettingsField(
        default_factory=list,
        title="Allowed email domains",
        description=(
            "Only users whose email is in one of these domains get a key. "
            "Empty: every user. Guests and service users never do."
        ),
    )
    mcp_url: str = SettingsField(
        default="",
        title="MCP endpoint URL",
        description=(
            "Where MCP clients connect. Empty: this AYON server's "
            "/api/mcp. Set it when the MCP server runs on its own host."
        ),
    )
    server_name: str = SettingsField(
        default="ayon",
        title="Server name in the client",
        description="The name the copied client configuration registers.",
    )
    key_valid_days: int = SettingsField(
        default=365,
        title="Key lifetime in days",
        description="0: the key never expires.",
        ge=0,
    )


class MCPSettings(BaseSettingsModel):
    """Settings for the addon."""

    api_key_action: ApiKeyActionSettings = SettingsField(
        default_factory=ApiKeyActionSettings,
        title="API key action",
    )
