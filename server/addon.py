"""Server addon definition."""
from typing import Any, Type

from ayon_server.addons import BaseServerAddon
from ayon_server.api.auth import user_from_request
from ayon_server.api.dependencies import CurrentUser
from ayon_server.exceptions import AyonException
from ayon_server.settings import BaseSettingsModel
from fastapi import APIRouter, Request, Response, WebSocket

from .mcp_tunnel import (
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHORIZED,
    TunnelHub,
    TunnelRejectedError,
    proxy_request,
    serve_tunnel,
)
from .mcp_tunnel.redis_bus import RedisTunnelBus
from .settings import DEFAULT_VALUES, MCPSettings


async def _authenticate_service(websocket: WebSocket) -> str:
    """Return the service user that opened a tunnel WebSocket.

    The HTTP auth middleware doesn't run for WebSockets, so the API key
    is checked here. Only service users may open a tunnel.

    Returns:
        Name of the service user.

    Raises:
        TunnelRejectedError: If the caller is not a service user.

    """
    try:
        user = await user_from_request(websocket)  # type: ignore[arg-type]
    except AyonException as exc:
        raise TunnelRejectedError(
            CLOSE_UNAUTHORIZED, f"Unauthorized: {exc}"
        ) from exc
    if not user.is_service:
        raise TunnelRejectedError(
            CLOSE_FORBIDDEN, f"User '{user.name}' is not a service user"
        )
    return user.name


class MCPAddon(BaseServerAddon):
    """Add-on class for the server."""
    settings_model: Type[MCPSettings] = MCPSettings
    addon_type = "server"
    frontend_scopes: dict[str, Any] = {"settings": {}}  # ruff: ignore[mutable-class-default]

    def initialize(self) -> None:
        """Set up the MCP endpoint and the service tunnel.

        MCP clients use ``/api/addons/mcp/{version}/mcp`` (or the server's
        ``/api/mcp``, which redirects to the production version). The MCP
        service connects to ``ws`` and answers the requests.
        """
        # Each addon version has its own hub and Redis keys, so production
        # and staging services each serve their own version's endpoint.
        self.tunnels = TunnelHub(RedisTunnelBus(f"mcp-{self.version}:"))
        router = APIRouter(include_in_schema=False)
        router.add_api_route(
            "/mcp",
            self.mcp_endpoint,
            methods=["GET", "POST", "DELETE"],
        )
        self.add_router(router)

    async def mcp_endpoint(
        self, request: Request, user: CurrentUser
    ) -> Response:
        """MCP streamable HTTP endpoint, answered by the MCP service.

        Returns:
            The service's response, streamed.

        """
        return await proxy_request(request, self.tunnels, user.name)

    async def ws(self, websocket: WebSocket) -> None:
        """Tunnel connection of the MCP service."""
        await serve_tunnel(websocket, self.tunnels, _authenticate_service)

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
