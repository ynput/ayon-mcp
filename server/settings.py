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
            "Projects where the action is offered. Empty: every project."
        ),
    )
    allowed_email_domains: list[str] = SettingsField(
        default_factory=list,
        title="Allowed email domains",
        description=(
            "Only users whose email is in one of these domains may use "
            "the action. Empty: every user. Guests and service users "
            "never may. Users can edit their own email, and AYON lets "
            "every user create API keys through its API, so this limits "
            "the action, not who can have a key."
        ),
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
