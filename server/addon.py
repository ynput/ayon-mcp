"""Server addon definition."""
from typing import TYPE_CHECKING, Any, Type

from ayon_server.addons import BaseServerAddon
from ayon_server.settings import BaseSettingsModel

from . import api_keys
from .settings import DEFAULT_VALUES, MCPSettings

if TYPE_CHECKING:
    from ayon_server.actions import (
        ActionExecutor,
        ExecuteResponseModel,
        SimpleActionManifest,
    )


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

    async def get_simple_actions(
        self,
        project_name: str | None = None,
        variant: str = "production",
    ) -> "list[SimpleActionManifest]":
        """Return the addon's actions for a project.

        Args:
            project_name: The project whose actions are listed.
            variant: Bundle variant.

        Returns:
            The "Create my MCP key" action where it is enabled.

        """
        return await api_keys.get_actions(self, project_name, variant)

    async def execute_action(
        self, executor: "ActionExecutor"
    ) -> "ExecuteResponseModel":
        """Run one of the addon's actions.

        Args:
            executor: The action executor.

        Returns:
            The action's response.

        """
        if executor.identifier == api_keys.ACTION_ID:
            return await api_keys.execute(self, executor)
        return await super().execute_action(executor)
