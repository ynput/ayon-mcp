"""Settings for the addon."""
from typing import Any

from ayon_server.settings import BaseSettingsModel, SettingsField

DEFAULT_VALUES: dict[str, Any] = {
    "read_only": False,
}


class MCPSettings(BaseSettingsModel):
    """Settings for the addon."""

    read_only: bool = SettingsField(
        default=False,
        title="Read-only mode",
        description=(
            "Expose only read tools to AI assistants. The REST gateway "
            "then accepts only GET and HEAD requests. The "
            "AYON_MCP_READ_ONLY environment variable on the service "
            "overrides this setting."
        ),
    )
