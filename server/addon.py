"""Server addon definition."""
from typing import Any, Type

from ayon_server.addons import BaseServerAddon
from ayon_server.settings import BaseSettingsModel

from .settings import DEFAULT_VALUES, MCPSettings


class MCPAddon(BaseServerAddon):
    """Add-on class for the server."""
    settings_model: Type[MCPSettings] = MCPSettings
    addon_type = "server"
    frontend_scopes: dict[str, Any] = {"settings": {}}  # ruff: ignore[mutable-class-default]

    async def get_default_settings(self) -> BaseSettingsModel:
        """Return default settings.

        Returns:
            BaseSettingsModel: Default settings model instance.

        Raises:
            RuntimeError: If the settings model class is not defined.

        """
        settings_model_cls = self.get_settings_model()
        if not settings_model_cls:
            msg = "Settings model class is not defined."
            raise RuntimeError(msg)
        return settings_model_cls(**DEFAULT_VALUES)
