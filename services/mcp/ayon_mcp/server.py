"""Command line entrypoint for the AYON MCP server."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from collections import OrderedDict
from typing import TYPE_CHECKING, AsyncIterator

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

# from fastmcp.server.providers.openapi import MCPType, RouteMap
from .client import get_ayon_api, set_global_ayon_client
from .instructions import INSTRUCTIONS, OPENAPI_INSTRUCTIONS
from .metrics import TokenMetrics
from .openapi_codegen import sync_openapi_tools_from_server
from .rest_client import RestApiClient, set_global_rest_client
from .tool_discovery import create_discovery_tools

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    import ayon_api
    import mcp.types as mcp_types
    from fastmcp.tools import ToolResult


class RemoteApiKeyMiddleware(Middleware):
    """Resolve AYON API key from request headers for remote tool calls."""

    def __init__(self, base_url: str):
        """Initialize the middleware with the base URL.

        Args:
            base_url: The base URL of the AYON server.

        """
        self._base_url = base_url

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, object],
    ) -> ToolResult:
        """Inject request-scoped AYON client based on ``x-api-key`` header.

        Args:
            context: The middleware context for the current request.
            call_next: The next middleware or tool handler to call.

        Returns:
            The result of the next middleware or tool handler.

        Raises:
            RuntimeError: If the ``x-api-key`` header is missing or empty.

        """
        from .client import set_global_ayon_api_key, set_global_ayon_as_user

        headers = get_http_headers()
        request_api_key = (headers.get("x-api-key") or "").strip()
        if not request_api_key:
            msg = (
                "Missing required 'x-api-key' header in remote mode. "
                "Provide a valid AYON API key with each MCP request."
            )
            raise RuntimeError(msg)

        set_global_ayon_api_key(request_api_key)
        set_global_ayon_as_user(None)
        set_global_ayon_client(get_ayon_api(self._base_url, request_api_key))
        return await call_next(context)  # ty: ignore[invalid-return-type]


class ServiceUserMiddleware(Middleware):
    """Act as the user the AYON server authenticated (tunnel mode).

    Behind the tunnel the AYON server has already authenticated the caller
    and passes only the user name in ``USER_HEADER``. Tool calls then use
    the service's own API key with ``x-as-user``, so the caller's
    permissions apply and the caller's credentials never leave the server.

    Only safe behind the tunnel: the server drops ``USER_HEADER`` from
    client requests. Never use it on a directly reachable HTTP port.
    """

    def __init__(
        self, base_url: str, service_api_key: str, max_clients: int = 128
    ):
        """Initialize the middleware.

        Args:
            base_url: The base URL of the AYON server.
            service_api_key: API key of an AYON service user.
            max_clients: Number of per-user AYON clients to keep.

        """
        self._base_url = base_url
        self._api_key = service_api_key
        self._max_clients = max_clients
        self._clients: OrderedDict[str, ayon_api.ServerAPI] = OrderedDict()

    def _client_for(self, user_name: str) -> ayon_api.ServerAPI:
        client = self._clients.get(user_name)
        if client is None:
            client = get_ayon_api(self._base_url, self._api_key)
            # Raises ValueError if the key is not a service user's key.
            client.set_default_service_username(user_name)
            self._clients[user_name] = client
            while len(self._clients) > self._max_clients:
                self._clients.popitem(last=False)
        else:
            self._clients.move_to_end(user_name)
        return client

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, object],
    ) -> ToolResult:
        """Inject an AYON client acting as the authenticated user.

        Args:
            context: The middleware context for the current request.
            call_next: The next middleware or tool handler to call.

        Returns:
            The result of the next middleware or tool handler.

        Raises:
            RuntimeError: If the request carries no user name.

        """
        from .client import set_global_ayon_api_key, set_global_ayon_as_user
        from .tunnel_protocol import USER_HEADER

        user_name = (
            get_http_headers(include_all=True).get(USER_HEADER) or ""
        ).strip()
        if not user_name:
            msg = (
                f"Missing '{USER_HEADER}' header. In tunnel mode requests "
                "must come through the MCP addon's endpoint on the AYON "
                "server."
            )
            raise RuntimeError(msg)

        set_global_ayon_api_key(self._api_key)
        set_global_ayon_as_user(user_name)
        set_global_ayon_client(self._client_for(user_name))
        return await call_next(context)  # ty: ignore[invalid-return-type]


class StaticApiKeyMiddleware(Middleware):
    """Use one configured AYON API key for local tool calls."""

    def __init__(self, base_url: str, api_key: str):
        """Initialize the middleware with the base URL and API key.

        Args:
            base_url: The base URL of the AYON server.
            api_key: The AYON API key for local tool calls.

        """
        self._base_url = base_url
        self._api_key = api_key
        self._client = None

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, object],
    ) -> ToolResult:
        """Inject a lazily initialized AYON client for local mode.

        Args:
            context: The middleware context for the current request.
            call_next: The next middleware or tool handler to call.

        Returns:
            The result of the next middleware or tool handler.

        """
        from .client import set_global_ayon_api_key, set_global_ayon_as_user

        if self._client is None:
            self._client = get_ayon_api(self._base_url, self._api_key)
        set_global_ayon_api_key(self._api_key)
        set_global_ayon_as_user(None)
        set_global_ayon_client(self._client)
        return await call_next(context)  # ty: ignore[invalid-return-type]


def register_tools(server: FastMCP, tools: Iterable[Callable]) -> None:
    """Registers a sequence of callable tools with the FastMCP instance."""
    for tool in tools:
        server.tool()(tool)


def create_mcp_server(base_url: str, api_key: str) -> FastMCP:
    """Create the MCP server with the given AYON server URL.

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)
        api_key: AYON API key used for authenticated OpenAPI sync.

    Returns:
        FastMCP instance configured with the AYON OpenAPI spec.

    """

    @lifespan
    async def server_lifespan(_server: FastMCP) -> AsyncIterator[dict]:
        """FastMCP Lifespan manager to manage client state cleanly.

        Args:
            _server: FastMCP instance.

        Yields:
            A dictionary containing the shared ApiClient instance.

        """
        client = RestApiClient(
            base_url=base_url
        )
        try:
            set_global_rest_client(client)
            # Pass to lifespan context
            yield {"api_client": client}
        finally:
            # Cleanup on shutdown
            await client.close()
            set_global_rest_client(None)

    from . import tools as tools_module

    if tools_module.openapi_tools_enabled():
        try:
            changed = sync_openapi_tools_from_server(
                base_url,
                api_key=api_key,
            )
            if changed:
                importlib.reload(tools_module)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "OpenAPI tool sync failed for %s/openapi.json: %s",
                base_url.rstrip("/"),
                exc,
            )

    instructions = (
        OPENAPI_INSTRUCTIONS
        if tools_module.openapi_tools_enabled()
        else INSTRUCTIONS
    )

    mcp = FastMCP(
        lifespan=server_lifespan,
        name="AYON MCP Server",
        instructions=instructions
    )

    if tools_module.tool_exposure_mode() == "direct":
        register_tools(mcp, tools_module.ALL_TOOLS)
    else:
        register_tools(
            mcp,
            create_discovery_tools(list(tools_module.ALL_TOOLS)),
        )

    return mcp


def create_remote_server(
    base_url: str, api_key: str, auth: Middleware | None = None
) -> FastMCP:
    """Create the MCP server for service mode.

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)
        api_key: AYON API key used for the OpenAPI sync.
        auth: Middleware resolving the AYON client per tool call;
            defaults to per-request API keys (``RemoteApiKeyMiddleware``).

    Returns:
        FastMCP instance with the remote-mode middleware installed.

    """
    mcp = create_mcp_server(
        base_url,
        api_key=api_key,
    )
    mcp.add_middleware(auth or RemoteApiKeyMiddleware(base_url))
    if os.getenv("AYON_MCP_OTEL_ENABLED", "false") == "true":
        mcp.add_middleware(TokenMetrics())
    return mcp


def run_tunnel(base_url: str, api_key: str) -> FastMCP:
    """Serve the MCP server through a WebSocket tunnel to the AYON server.

    The service connects out to the MCP addon on the AYON server
    (``/api/addons/mcp/{version}/ws``, see ``resolve_tunnel_url``), which
    exposes the MCP endpoint at ``/api/addons/mcp/{version}/mcp``; the
    server's ``/api/mcp`` redirects there for the production version.
    No port is opened, so no ingress or public IP is needed.

    The HTTP app runs stateless, so any request can be served by any
    service replica and no MCP session state has to survive reconnects.

    Tool calls act as the user the AYON server authenticated, using the
    service API key with ``x-as-user`` (see ``ServiceUserMiddleware``).

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)
        api_key: Service API key, used for the OpenAPI sync, to
            authenticate the tunnel and to act as the calling users.

    Returns:
        FastMCP instance, once the tunnel loop stops.

    """
    from .tunnel import resolve_tunnel_url, serve_forever

    mcp = create_remote_server(
        base_url, api_key, auth=ServiceUserMiddleware(base_url, api_key)
    )
    app = mcp.http_app(path="/mcp", stateless_http=True)

    async def serve() -> None:
        url = await resolve_tunnel_url(base_url, api_key)
        await serve_forever(app, url, api_key, server_url=base_url)

    asyncio.run(serve())
    return mcp


def run_remote(base_url: str, api_key: str) -> FastMCP:
    """Run the MCP server with the given AYON server URL and API key.

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)
        api_key: AYON API key.

    Returns:
        FastMCP instance configured with the AYON OpenAPI spec.

    """
    mcp = create_remote_server(base_url, api_key)
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",  # ruff:ignore[hardcoded-bind-all-interfaces]
        port=int(os.getenv("AYON_MCP_PORT", "8088")),
        show_banner=False
    )
    return mcp


def run_local(base_url: str, api_key: str) -> FastMCP:
    """Run the MCP server with the given AYON server URL and API key.

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)
        api_key: AYON API key

    Returns:
        FastMCP instance configured with the AYON OpenAPI spec.

    """
    resolved_api_key = api_key or os.getenv("AYON_API_KEY", "")
    mcp = create_mcp_server(base_url, api_key=resolved_api_key)
    mcp.add_middleware(
        StaticApiKeyMiddleware(
            base_url,
            resolved_api_key,
        )
    )
    mcp.add_middleware(TokenMetrics())
    mcp.run(show_banner=False)
    return mcp


if __name__ == "__main__":
    run_local(
        os.getenv("AYON_SERVER_URL", "http://localhost:5000"),
        os.getenv("AYON_API_KEY", "")
    )
