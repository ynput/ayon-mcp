"""Server addon definition."""
from typing import Any, ClassVar, Type

from ayon_server.addons import BaseServerAddon

from .settings import DEFAULT_VALUES, MCPSettings


class MCPAddon(BaseServerAddon):
    """Add-on class for the server."""
    settings_model: Type[MCPSettings] = MCPSettings
    addon_type = "server"
    frontend_scopes: ClassVar[dict[str, Any]] = {"settings": {}}

    async def get_default_settings(self) -> MCPSettings:
        """Return default settings."""
        settings_model_cls = self.get_settings_model()
        return settings_model_cls(**DEFAULT_VALUES)
