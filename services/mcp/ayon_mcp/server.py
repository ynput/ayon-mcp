"""Command line entrypoint for the AYON MCP server."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, AsyncIterator

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from .addon_discovery import AddonCatalog
from .client import get_ayon_api, set_global_ayon_client
from .guardrails import read_only_enabled
from .instructions import (
    INSTRUCTIONS,
    OPENAPI_INSTRUCTIONS,
    READ_ONLY_INSTRUCTIONS,
)
from .metrics import TokenMetrics
from .openapi_tools import load_rest_tools, rest_tool_of
from .policy import (
    EffectivePolicy,
    PolicyStore,
    policy_settings_enabled,
    resolve,
    set_current_policy,
)
from .rest_client import RestApiClient, set_global_rest_client
from .rest_policy import admin_tools_enabled
from .tool_discovery import (
    AyonTool,
    create_addon_ayon_tools,
    create_curated_tools,
    create_discovery_tools,
    drop_admin_tools,
    drop_mutating_tools,
    hidden_tool_error,
    is_visible,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    import ayon_api
    import mcp.types as mcp_types
    from fastmcp.tools import Tool, ToolResult


class _AuthMiddleware(Middleware):
    """Bind the caller's AYON client for tool calls and tool listing.

    Subclasses implement ``_bind``. Listing binds too, so per-user policy
    can filter ``tools/list`` in direct exposure mode. A listing without
    credentials stays unbound instead of failing (``required=False``).
    """

    async def _bind(self, *, required: bool) -> None:
        """Set the AYON client contextvars for the current request.

        Args:
            required: Raise when the request carries no credentials.

        """
        raise NotImplementedError

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, object],
    ) -> ToolResult:
        """Bind the caller's AYON client, then run the tool.

        Args:
            context: The middleware context for the current request.
            call_next: The next middleware or tool handler to call.

        Returns:
            The result of the next middleware or tool handler.

        """
        await self._bind(required=True)
        return await call_next(context)  # ty: ignore[invalid-return-type]

    async def on_list_tools(
        self,
        context: MiddlewareContext[mcp_types.ListToolsRequest],
        call_next: CallNext[mcp_types.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Bind the caller's AYON client when known, then list tools.

        Args:
            context: The middleware context for the current request.
            call_next: The next middleware or handler to call.

        Returns:
            The tools the next handler lists.

        """
        await self._bind(required=False)
        return await call_next(context)


class RemoteApiKeyMiddleware(_AuthMiddleware):
    """Resolve AYON API key from request headers for remote tool calls."""

    def __init__(self, base_url: str):
        """Initialize the middleware with the base URL.

        Args:
            base_url: The base URL of the AYON server.

        """
        self._base_url = base_url

    async def _bind(self, *, required: bool) -> None:
        """Bind a request-scoped AYON client from the ``x-api-key`` header.

        Args:
            required: Raise when the header is missing.

        Raises:
            RuntimeError: If required and ``x-api-key`` is missing or empty.

        """
        from .client import set_global_ayon_api_key, set_global_ayon_as_user

        headers = get_http_headers()
        request_api_key = (headers.get("x-api-key") or "").strip()
        if not request_api_key:
            if not required:
                return
            msg = (
                "Missing required 'x-api-key' header in remote mode. "
                "Provide a valid AYON API key with each MCP request."
            )
            raise RuntimeError(msg)

        # Validating the key is a blocking HTTP call - keep it off the loop.
        client = await asyncio.to_thread(
            get_ayon_api, self._base_url, request_api_key
        )
        set_global_ayon_api_key(request_api_key)
        set_global_ayon_as_user(None)
        set_global_ayon_client(client)


class ServiceUserMiddleware(_AuthMiddleware):
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

    def _new_client(self, user_name: str) -> ayon_api.ServerAPI:
        client = get_ayon_api(self._base_url, self._api_key)
        # Raises ValueError if the key is not a service user's key.
        client.set_default_service_username(user_name)
        return client

    async def _client_for(self, user_name: str) -> ayon_api.ServerAPI:
        client = self._clients.get(user_name)
        if client is None:
            # Blocking HTTP calls run in a thread; the cache itself is only
            # touched on the event loop.
            client = await asyncio.to_thread(self._new_client, user_name)
            self._clients[user_name] = client
            while len(self._clients) > self._max_clients:
                self._clients.popitem(last=False)
        else:
            self._clients.move_to_end(user_name)
        return client

    async def _bind(self, *, required: bool) -> None:
        """Bind an AYON client acting as the authenticated user.

        Args:
            required: Raise when the request carries no user name.

        Raises:
            RuntimeError: If required and the request has no user name.

        """
        from .client import set_global_ayon_api_key, set_global_ayon_as_user
        from .tunnel_protocol import USER_HEADER

        user_name = (
            get_http_headers(include_all=True).get(USER_HEADER) or ""
        ).strip()
        if not user_name:
            if not required:
                return
            msg = (
                f"Missing '{USER_HEADER}' header. In tunnel mode requests "
                "must come through the MCP addon's endpoint on the AYON "
                "server."
            )
            raise RuntimeError(msg)

        set_global_ayon_api_key(self._api_key)
        set_global_ayon_as_user(user_name)
        set_global_ayon_client(await self._client_for(user_name))


class StaticApiKeyMiddleware(_AuthMiddleware):
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

    async def _bind(self, *, required: bool) -> None:
        """Bind the lazily initialized AYON client for local mode.

        Args:
            required: Unused; the configured key is always there.

        """
        from .client import set_global_ayon_api_key, set_global_ayon_as_user

        if self._client is None:
            self._client = await asyncio.to_thread(
                get_ayon_api, self._base_url, self._api_key
            )
        set_global_ayon_api_key(self._api_key)
        set_global_ayon_as_user(None)
        set_global_ayon_client(self._client)


class DirectPolicyMiddleware(Middleware):
    """Apply each caller's MCP policy to directly exposed tools.

    In ``direct`` exposure mode tools are registered with FastMCP as they
    are, so calls never pass through ``AyonDynamicToolProvider``. This
    middleware filters ``tools/list`` and refuses calls to hidden tools
    with the same messages. It must run after the auth middleware, which
    binds the caller. Guards and mutation confirmation stay discovery-mode
    only.
    """

    def __init__(
        self, tools: Iterable[AyonTool], policy_store: PolicyStore | None
    ):
        """Initialize the middleware.

        Args:
            tools: The registered tools, with policy metadata.
            policy_store: Per-caller policy source; ``None`` applies the
                env var policy to everyone.

        """
        self._tools = {tool.name: tool for tool in tools}
        self._policy_store = policy_store

    async def _policy(self) -> EffectivePolicy:
        if self._policy_store is None:
            policy = EffectivePolicy.from_env()
        else:
            policy = await self._policy_store.policy()
        # For get_server_info's mcpPolicy.
        set_current_policy(policy)
        return policy

    async def on_list_tools(
        self,
        context: MiddlewareContext[mcp_types.ListToolsRequest],
        call_next: CallNext[mcp_types.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """List only the tools the caller's policy lets it see.

        Args:
            context: The middleware context for the current request.
            call_next: The next middleware or handler to call.

        Returns:
            The visible tools.

        """
        policy = await self._policy()
        listed = await call_next(context)
        return [
            tool
            for tool in listed
            if tool.name not in self._tools
            or is_visible(self._tools[tool.name], policy)
        ]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, object],
    ) -> ToolResult:
        """Refuse a call to a tool the caller's policy hides.

        Args:
            context: The middleware context for the current request.
            call_next: The next middleware or tool handler to call.

        Returns:
            The result of the next middleware or tool handler.

        Raises:
            ToolError: If the caller's policy hides the tool.

        """
        policy = await self._policy()
        tool = self._tools.get(context.message.name)
        if tool is not None:
            error = hidden_tool_error(tool, policy)
            if error is not None:
                raise ToolError(error)
        return await call_next(context)  # ty: ignore[invalid-return-type]


def register_tools(server: FastMCP, tools: Iterable[Callable]) -> None:
    """Registers a sequence of callable tools with the FastMCP instance."""
    for tool in tools:
        rest_tool = rest_tool_of(tool)
        if rest_tool is not None:
            server.add_tool(rest_tool)
        else:
            server.tool()(tool)


def collect_tools(base_url: str, api_key: str) -> list[Callable]:
    """Return the curated and REST tool functions the env vars allow.

    Returns:
        The tool functions, without admin tools unless
        ``AYON_MCP_ADMIN_TOOLS`` and without mutating tools in
        ``AYON_MCP_READ_ONLY`` mode.

    """
    from . import tools as tools_module

    tools = list(tools_module.ALL_TOOLS)
    if tools_module.openapi_tools_enabled():
        tools += load_rest_tools(
            base_url,
            api_key,
            reserved_names={tool.__name__ for tool in tools},
        )
    if not admin_tools_enabled():
        # Secrets, credentials, user/access management and server lifecycle
        # endpoints are opt-in - see rest_policy.ADMIN_PATTERNS.
        tools = drop_admin_tools(tools)
    if read_only_enabled():
        # Not registering mutating tools at all covers direct exposure mode
        # too, where calls never pass through AyonDynamicToolProvider.
        tools = drop_mutating_tools(tools)
    return tools


async def _load_addon_tools(
    base_url: str, api_key: str, reserved_names: set[str]
) -> list[AyonTool]:
    """Discover addon MCP tools once, as the service catalog would.

    Returns:
        The addon tools, empty when ``AYON_MCP_ENABLE_ADDON_TOOLS`` is off.

    """
    if not addon_tools_enabled():
        return []
    catalog = AddonCatalog(
        base_url,
        api_key,
        variant=addon_tools_variant(),
        refresh_interval=0,
    )
    try:
        # The first load is awaited.
        await catalog.refresh()
    finally:
        await catalog.close()
    return create_addon_ayon_tools(
        catalog.tools, reserved_names=reserved_names
    )


async def explain_policy(
    base_url: str, api_key: str, user_name: str
) -> dict[str, Any]:
    """Explain the MCP policy a user gets and the tools it lets them use.

    Covers curated, REST and addon tools, as the discovery provider would
    catalog them.

    Args:
        base_url: AYON server URL.
        api_key: Service API key, able to read users and addon settings.
        user_name: The AYON user to explain.

    Returns:
        The user's identity, effective policy, and visible tools by
        namespace.

    """
    store = PolicyStore(base_url, api_key)
    settings = await store.settings()
    identity = await store.identity_of(user_name)
    policy = resolve(identity, settings, EffectivePolicy.from_env())
    tools = create_curated_tools(
        await asyncio.to_thread(collect_tools, base_url, api_key)
    )
    tools += await _load_addon_tools(
        base_url, api_key, reserved_names={tool.name for tool in tools}
    )
    visible: dict[str, list[str]] = {}
    for tool in tools:
        if policy.allows_tool(
            tool.name,
            tool.namespace,
            tool.side_effect,
            admin=tool.admin,
            endpoint=tool.endpoint,
        ):
            visible.setdefault(tool.namespace, []).append(tool.name)
    visible_count = sum(len(names) for names in visible.values())
    return {
        "user": user_name,
        "identity": (
            {
                "level": identity.level,
                "access_groups": sorted(identity.access_groups),
            }
            if identity is not None
            else None
        ),
        "policy": policy.summary(),
        "visible_tools": visible_count,
        "hidden_tools": len(tools) - visible_count,
        "tools": {
            namespace: sorted(names)
            for namespace, names in sorted(visible.items())
        },
    }


def create_mcp_server(
    base_url: str, api_key: str, auth: Middleware | None = None
) -> FastMCP:
    """Create the MCP server with the given AYON server URL.

    Args:
        base_url: AYON server URL (e.g. http://localhost:5000)
        api_key: AYON API key used to fetch the OpenAPI spec.
        auth: Middleware binding the caller's AYON client. Added first,
            so the per-user policy middleware runs after it.

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
            if addon_catalog is not None:
                # In the background: a slow AYON must not delay start.
                addon_catalog.start()
            # Pass to lifespan context
            yield {"api_client": client}
        finally:
            # Cleanup on shutdown
            if addon_catalog is not None:
                await addon_catalog.close()
            await client.close()
            set_global_rest_client(None)

    from . import tools as tools_module

    instructions = (
        OPENAPI_INSTRUCTIONS
        if tools_module.openapi_tools_enabled()
        else INSTRUCTIONS
    )

    tools = collect_tools(base_url, api_key)
    if read_only_enabled():
        instructions = f"{instructions}\n{READ_ONLY_INSTRUCTIONS}"

    mcp = FastMCP(
        lifespan=server_lifespan,
        name="AYON MCP Server",
        instructions=instructions
    )

    # The lifespan above starts its first load.
    addon_catalog = _create_addon_catalog(
        base_url, api_key, tools_module.tool_exposure_mode()
    )
    if auth is not None:
        mcp.add_middleware(auth)
    if tools_module.tool_exposure_mode() == "direct":
        register_tools(mcp, tools)
        # Calls skip the discovery provider: filter per caller here.
        mcp.add_middleware(
            DirectPolicyMiddleware(
                create_curated_tools(tools),
                _create_policy_store(base_url, api_key),
            )
        )
    else:
        register_tools(
            mcp,
            create_discovery_tools(
                tools,
                addon_catalog=addon_catalog,
                policy_store=_create_policy_store(base_url, api_key),
            ),
        )

    return mcp


def _create_addon_catalog(
    base_url: str, api_key: str, exposure_mode: str
) -> AddonCatalog | None:
    """Return the addon MCP tool catalog, or None when discovery is off.

    Returns:
        The catalog, not loaded yet; None when addon tools are disabled
        or cannot be exposed (direct exposure mode).

    """
    if not addon_tools_enabled():
        return None
    if exposure_mode == "direct":
        # Direct mode cannot expose addon tools, so skip the requests.
        logger.info(
            "Addon MCP tool discovery skipped: AYON_MCP_TOOL_EXPOSURE="
            "direct only exposes curated tools."
        )
        return None
    return AddonCatalog(
        base_url,
        api_key,
        variant=addon_tools_variant(),
        refresh_interval=addon_tools_refresh_interval(),
    )


def _create_policy_store(base_url: str, api_key: str) -> PolicyStore | None:
    """Return the per-caller policy source, or None to use env vars only.

    Returns:
        A store reading the MCP addon's settings with ``api_key``; None
        when disabled or without a key to read them with.

    """
    if not policy_settings_enabled() or not api_key:
        return None
    return PolicyStore(base_url, api_key)


def addon_tools_enabled() -> bool:
    """Return True if addon MCP tool discovery should run at startup."""
    value = (os.getenv("AYON_MCP_ENABLE_ADDON_TOOLS", "true") or "").strip()
    return value.lower() not in {"0", "false", "no", "off"}


def addon_tools_variant() -> str:
    """Return the bundle variant whose addon versions expose MCP tools.

    ``AYON_MCP_ADDON_VARIANT`` wins, then ``AYON_DEFAULT_SETTINGS_VARIANT``
    (the variant an AYON service runs on), then ``production``.
    """
    for name in ("AYON_MCP_ADDON_VARIANT", "AYON_DEFAULT_SETTINGS_VARIANT"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return "production"


def addon_tools_refresh_interval() -> float:
    """Return seconds between checks for changed addon versions.

    ``AYON_MCP_ADDON_REFRESH_SECONDS``, default 60; ``0`` disables the
    periodic check.
    """
    value = (os.getenv("AYON_MCP_ADDON_REFRESH_SECONDS") or "").strip()
    try:
        return max(float(value), 0.0) if value else 60.0
    except ValueError:
        logger.warning(
            "Invalid AYON_MCP_ADDON_REFRESH_SECONDS %r, using 60.", value
        )
        return 60.0


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
        auth=auth or RemoteApiKeyMiddleware(base_url),
    )
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
        api_key: Service API key, used to fetch the OpenAPI spec, to
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
    mcp = create_mcp_server(
        base_url,
        api_key=resolved_api_key,
        auth=StaticApiKeyMiddleware(base_url, resolved_api_key),
    )
    mcp.add_middleware(TokenMetrics())
    mcp.run(show_banner=False)
    return mcp


if __name__ == "__main__":
    run_local(
        os.getenv("AYON_SERVER_URL", "http://localhost:5000"),
        os.getenv("AYON_API_KEY", "")
    )
