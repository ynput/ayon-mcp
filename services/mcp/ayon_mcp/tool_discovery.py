"""On-demand discovery and execution for AYON MCP tools."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from chuk_tool_processor.discovery import (
    BaseDynamicToolProvider,
    find_tool_by_alias,
)
from chuk_tool_processor.guards import (
    ErrorClass,
    RetrySafetyGuard,
    SideEffectClass,
)
from fastmcp.tools.function_tool import FunctionTool

from .addon_tools import call_addon_tool
from .guardrails import (
    DESTRUCTIVE_TOOL_NAMES,
    GuardsMode,
    build_guard_chain,
    read_only_enabled,
)
from .openapi_tools import rest_tool_of
from .policy import EffectivePolicy
from .rest_policy import endpoint_of, is_admin, side_effect_for

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from chuk_tool_processor.guards import GuardChain

    from .addon_discovery import AddonCatalog, AddonTool
    from .policy import PolicyStore
    from .rest_client import RestApiClient


logger = logging.getLogger(__name__)


# Sentinel: "build the guard chain from env" vs. an explicit None
_DEFAULT = object()

MUTATING_TOOL_NAMES = frozenset({
    "add_comment",
    "create_entity",
    "delete_entity",
    "dispatch_event",
    "set_addon_settings",
    "set_attribute_description",
    "update_entity",
})


@dataclass(frozen=True)
class AyonTool:
    """A curated AYON tool exposed through the discovery provider."""

    name: str
    namespace: str
    description: str
    parameters: dict[str, Any]
    function: Callable[..., Any]
    side_effect: SideEffectClass = SideEffectClass.READ_ONLY
    # Calls an admin/security REST endpoint (``rest_policy.is_admin``).
    admin: bool = False

    @property
    def requires_confirmation(self) -> bool:
        """Whether the tool changes AYON data and needs confirm_mutation."""
        return self.side_effect is not SideEffectClass.READ_ONLY


def _tool_description(function: Callable[..., Any]) -> str:
    """Return a useful tool description.

    Returns:
        The function docstring or a generated fallback description.

    """
    return (
        inspect.getdoc(function)
        or f"Run the {function.__name__} AYON tool."
    )


def tool_side_effect(function: Callable[..., Any]) -> SideEffectClass:
    """Classify a tool function as read-only, write, or destructive.

    Curated tools are classified by name; REST tools by their endpoint,
    via ``rest_policy``. Anything else is a curated read tool.

    Returns:
        The tool's ``SideEffectClass``.

    """
    name = function.__name__
    if name in DESTRUCTIVE_TOOL_NAMES:
        return SideEffectClass.DESTRUCTIVE
    if name in MUTATING_TOOL_NAMES:
        return SideEffectClass.WRITE
    endpoint = endpoint_of(function)
    if endpoint is not None:
        return side_effect_for(*endpoint)
    return SideEffectClass.READ_ONLY


def _is_admin_tool(function: Callable[..., Any]) -> bool:
    endpoint = endpoint_of(function)
    return endpoint is not None and is_admin(*endpoint)


def _requires_confirmation(function: Callable[..., Any]) -> bool:
    return tool_side_effect(function) is not SideEffectClass.READ_ONLY


def drop_mutating_tools(
    functions: list[Callable[..., Any]],
) -> list[Callable[..., Any]]:
    """Remove every state-changing tool, for ``AYON_MCP_READ_ONLY`` mode.

    Returns:
        Only the functions not classified as mutating.

    """
    return [
        function for function in functions
        if not _requires_confirmation(function)
    ]


def drop_admin_tools(
    functions: list[Callable[..., Any]],
) -> list[Callable[..., Any]]:
    """Remove admin/security REST tools, unless ``AYON_MCP_ADMIN_TOOLS``.

    Returns:
        Only the functions not calling an admin endpoint.

    """
    return [
        function for function in functions if not _is_admin_tool(function)
    ]


def create_curated_tools(
    functions: list[Callable[..., Any]],
) -> list[AyonTool]:
    """Build discovery records from curated and REST tool functions.

    Returns:
        Discovery records with FastMCP-derived input schemas, or for REST
        tools the schemas from the OpenAPI spec.

    """
    tools = []
    for function in functions:
        rest_tool = rest_tool_of(function)
        if rest_tool is not None:
            tools.append(
                AyonTool(
                    name=rest_tool.name,
                    namespace=rest_tool.namespace,
                    description=rest_tool.description or rest_tool.name,
                    parameters=rest_tool.parameters,
                    function=function,
                    side_effect=tool_side_effect(function),
                    admin=_is_admin_tool(function),
                )
            )
            continue
        fastmcp_tool = FunctionTool.from_function(function)
        module = function.__module__.rsplit(".", maxsplit=1)[-1]
        tools.append(
            AyonTool(
                name=function.__name__,
                namespace=f"ayon.{module}",
                description=_tool_description(function),
                parameters=fastmcp_tool.parameters,
                function=function,
                side_effect=tool_side_effect(function),
                admin=_is_admin_tool(function),
            )
        )
    return tools


def _addon_function(
    tool: AddonTool,
    client: RestApiClient | None,
) -> Callable[..., Any]:
    async def run(**arguments: Any) -> Any:  # ruff: ignore[any-type]
        return await call_addon_tool(tool, arguments, client)

    run.__name__ = tool.full_name
    return run


def create_addon_ayon_tools(
    addon_tools: list[AddonTool],
    reserved_names: Iterable[str] = (),
    client: RestApiClient | None = None,
) -> list[AyonTool]:
    """Convert addon tools to AyonTool format for discovery.

    Addon tools carry the side effect their addon declared, so the same
    confirmation policy and guard chain apply as for curated tools. In
    ``AYON_MCP_READ_ONLY`` mode only read-only addon tools are kept.
    Name conflicts between addons are resolved by the ``AddonCatalog``
    already; here an addon only loses against a built-in tool.

    Args:
        addon_tools: List of discovered addon tools.
        reserved_names: Names already taken (curated tools); addon tools
            colliding with them are skipped so an addon cannot shadow a
            built-in tool.
        client: REST client for the addon calls. When omitted, the
            process-global client is resolved per call.

    Returns:
        List of AyonTool instances for the discovery provider.

    """
    read_only = read_only_enabled()
    reserved = set(reserved_names)
    tools: list[AyonTool] = []
    for tool in addon_tools:
        if tool.full_name in reserved:
            logger.warning(
                "Addon tool %s from %s/%s conflicts with a built-in tool, "
                "keeping the built-in tool",
                tool.full_name,
                tool.addon_name,
                tool.addon_version,
            )
            continue
        if read_only and tool.side_effect is not SideEffectClass.READ_ONLY:
            continue
        tools.append(
            AyonTool(
                name=tool.full_name,
                namespace=f"ayon.addon.{tool.addon_name}",
                description=tool.description,
                parameters=tool.parameters,
                function=_addon_function(tool, client),
                side_effect=tool.side_effect,
            )
        )
    return tools


def _retry_guard_of(chain: GuardChain) -> RetrySafetyGuard | None:
    guard = chain.get("retry_safety")
    return guard if isinstance(guard, RetrySafetyGuard) else None


def _read_only_error(tool: AyonTool, policy: EffectivePolicy) -> str:
    """Return the refusal for a write tool in read-only mode.

    Returns:
        The message, naming the rules or the env var that set read-only.

    """
    if policy.matched_rules or not read_only_enabled():
        reason = f"read-only for this user by {policy.describe()}"
    else:
        reason = "the server runs in read-only mode (AYON_MCP_READ_ONLY)"
    return f"Tool '{tool.name}' changes AYON data and is blocked: {reason}."


async def _invoke(
    function: Callable[..., Any], arguments: dict[str, Any]
) -> Any:  # ruff: ignore[any-type]
    """Call a tool function without blocking the event loop.

    Curated tools block on ayon_api HTTP calls, so sync functions run in a
    worker thread. ``asyncio.to_thread`` copies contextvars, so the call
    still uses the caller's AYON client, and one slow call doesn't stall
    other users' requests or the tunnel's WebSocket keepalive.

    Returns:
        The function's result.

    """
    if inspect.iscoroutinefunction(function):
        result = await function(**arguments)
    else:
        result = await asyncio.to_thread(function, **arguments)
    if inspect.isawaitable(result):
        result = await result
    return result


class AyonDynamicToolProvider(BaseDynamicToolProvider[AyonTool]):
    """Expose AYON tools through chuk's compact discovery protocol."""

    def __init__(
        self,
        tools: list[AyonTool],
        guard_chain: GuardChain | object | None = _DEFAULT,
        addon_catalog: AddonCatalog | None = None,
        policy_store: PolicyStore | None = None,
    ) -> None:
        """Initialize the provider with its available AYON tools.

        Args:
            tools: The catalogued AYON tools this provider exposes.
            guard_chain: Guard chain to run before/after execution.
                Defaults to the env-configured chain from
                ``guardrails.build_guard_chain`` (itself ``None`` when
                ``AYON_MCP_GUARDS=off``). Pass ``None`` explicitly to skip
                guardrails regardless of env. ``AYON_MCP_READ_ONLY`` is
                enforced either way.
            addon_catalog: Source of addon tools. Its tools are appended
                to ``tools`` and re-read whenever the catalog reports a
                bundle change, so the catalog follows addon updates.
            policy_store: Source of the per-caller policy (addon
                settings). Without it every caller gets the env var
                policy.

        """
        super().__init__()
        self._base_tools = tools
        self._addon_catalog = addon_catalog
        self._addon_generation = -1
        self._policy_store = policy_store
        self._explicit_guard_chain = guard_chain
        # One guard chain per (guards, read_only) a caller's policy asks
        # for; a ``None`` value means guards off.
        self._guard_chains: dict[
            tuple[GuardsMode, bool], GuardChain | None
        ] = {}
        # Shared by every strict chain and kept across catalog rebuilds,
        # so retry history survives both.
        self._retry_guard: RetrySafetyGuard | None = None
        self._set_tools(self._combined_tools())

    def _combined_tools(self) -> list[AyonTool]:
        if self._addon_catalog is None:
            return self._base_tools
        self._addon_generation = self._addon_catalog.generation
        return [
            *self._base_tools,
            *create_addon_ayon_tools(
                self._addon_catalog.tools,
                reserved_names={tool.name for tool in self._base_tools},
            ),
        ]

    def _set_tools(self, tools: list[AyonTool]) -> None:
        """Replace the catalog and drop everything derived from it."""
        self._tools = tools
        self._tools_by_name = {tool.name: tool for tool in tools}
        self._guard_chains.clear()
        self.invalidate_cache()

    def _chain_for(self, policy: EffectivePolicy) -> GuardChain | None:
        """Return the guard chain for a caller's policy, built once.

        Returns:
            The explicitly passed chain if there is one, otherwise the
            chain for the policy's guard profile and read-only mode.

        """
        if self._explicit_guard_chain is not _DEFAULT:
            return self._explicit_guard_chain  # ty: ignore[invalid-return-type]
        key = (policy.guards, policy.read_only)
        if key in self._guard_chains:
            return self._guard_chains[key]
        chain = build_guard_chain(
            self._tools, policy.guards, read_only=policy.read_only
        )
        retry_guard = chain.get("retry_safety") if chain is not None else None
        if chain is not None and isinstance(retry_guard, RetrySafetyGuard):
            if self._retry_guard is None:
                self._retry_guard = retry_guard
            else:
                chain.remove("retry_safety")
                chain.add("retry_safety", self._retry_guard)
        self._guard_chains[key] = chain
        return chain

    async def _current_policy(self) -> EffectivePolicy:
        """Return the policy of the caller of the current tool call.

        Returns:
            The addon settings' policy for the caller, or the env var
            policy without a policy store.

        """
        if self._policy_store is None:
            return EffectivePolicy.from_env()
        return await self._policy_store.policy()

    @staticmethod
    def _is_visible(tool: AyonTool, policy: EffectivePolicy) -> bool:
        """Return True if the caller's policy lets it see ``tool``."""
        if tool.admin and not policy.admin_tools:
            return False
        return not (policy.read_only and tool.requires_confirmation)

    def _find_tool(self, tool_name: str) -> AyonTool | None:
        """Return a catalogued tool by exact name or alias."""
        tool = self._tools_by_name.get(tool_name)
        if tool is None:
            tool = find_tool_by_alias(tool_name, self._tools)
        return tool

    def _access_error(
        self, tool_name: str, policy: EffectivePolicy
    ) -> str | None:
        """Return why the caller can't use a tool hidden by its policy.

        A hidden admin tool looks exactly like an unknown one. A write
        tool hidden only by read-only mode says so, so the model explains
        the refusal instead of searching for a workaround. A tool that
        would stay hidden without read-only mode (an admin tool that also
        writes) still looks unknown, so its name is never confirmed.

        Returns:
            The error message, or ``None`` if the tool is not hidden.

        """
        tool = self._find_tool(tool_name)
        if tool is None or self._is_visible(tool, policy):
            return None
        if (
            policy.read_only
            and tool.requires_confirmation
            and self._is_visible(tool, replace(policy, read_only=False))
        ):
            return _read_only_error(tool, policy)
        return f"Tool '{tool_name}' was not found."

    def _is_unknown(self, tool_name: str) -> bool:
        """Return True if ``tool_name`` may exist on the server only.

        Exact names, aliases (``ayon.entities.get_folders``) and addon
        tools hidden here (read-only mode, name conflict) are known, so
        they never force a re-check whose result cannot change.
        """
        if tool_name in self._tools_by_name:
            return False
        if find_tool_by_alias(tool_name, self._tools) is not None:
            return False
        return self._addon_catalog is None or all(
            tool.full_name != tool_name for tool in self._addon_catalog.tools
        )

    async def _sync_addon_tools(self, *, force: bool = False) -> None:
        """Pick up addon tools changed by a bundle update.

        Every service replica discovers on its own, so a replica may not
        know a tool another replica already listed. ``force`` re-checks
        the server right away (rate limited by the catalog) when a tool
        name is unknown here.
        """
        if self._addon_catalog is None:
            return
        await self._addon_catalog.refresh(force=force)
        if self._addon_catalog.generation != self._addon_generation:
            self._set_tools(self._combined_tools())

    async def get_all_tools(self) -> list[AyonTool]:
        """Return every catalogued AYON tool.

        Only the tools the current caller's policy lets it see, so list,
        search and alias resolution of the base class never reveal hidden
        tools.

        Returns:
            The discovery catalog, refreshed after addon changes.

        """
        await self._sync_addon_tools()
        policy = await self._current_policy()
        return [
            tool for tool in self._tools if self._is_visible(tool, policy)
        ]

    async def get_tool_schema(self, tool_name: str) -> dict[str, Any]:
        """Return the schema and mutation confirmation requirement.

        For a tool that requires confirmation, the target tool's own
        parameters are wrapped under an ``arguments`` property so the
        schema mirrors the actual `call_ayon_tool(tool_name, arguments,
        confirm_mutation)` contract - `confirm_mutation` is a sibling of
        `arguments`, never one of the target tool's own properties, so it
        must not be nested inside `arguments` when calling `call_ayon_tool`.

        Returns:
            An OpenAI-style function schema with confirmation metadata.

        """
        # Before the base class answers from its schema cache, which
        # _set_tools clears when the addon tools changed.
        await self._sync_addon_tools(force=self._is_unknown(tool_name))
        # The schema cache is shared by all callers: check this caller's
        # access before it can answer.
        access_error = self._access_error(
            tool_name, await self._current_policy()
        )
        if access_error is not None:
            return {"success": False, "error": access_error}
        schema = await super().get_tool_schema(tool_name)
        tool = self._tools_by_name.get(
            schema.get("function", {}).get("name", tool_name)
        )
        if tool is None or not tool.requires_confirmation:
            return schema

        target_parameters = schema["function"]["parameters"]
        schema["function"]["parameters"] = {
            "type": "object",
            "properties": {
                "arguments": {
                    **target_parameters,
                    "description": (
                        f"{tool.name}'s own parameters, passed as the "
                        "`arguments` value of the call_ayon_tool call."
                    ),
                },
                "confirm_mutation": {
                    "type": "boolean",
                    "description": (
                        "Top-level argument of call_ayon_tool itself - a "
                        "sibling of `tool_name` and `arguments`, never "
                        "nested inside `arguments`. Set true only after "
                        "the user explicitly authorized this "
                        "state-changing operation."
                    ),
                },
            },
            "required": ["arguments", "confirm_mutation"],
        }
        schema["function"]["description"] += (
            " This operation changes AYON data. Call call_ayon_tool with "
            "confirm_mutation=true as a top-level argument (a sibling of "
            "tool_name and arguments) - do not put confirm_mutation "
            "inside arguments."
        )
        return schema

    def _policy_error(
        self,
        tool: AyonTool,
        policy: EffectivePolicy,
        *,
        confirmed: object,
    ) -> str | None:
        """Return why a call is refused, independent of guards.

        Returns:
            The refusal message, or ``None`` when the call may proceed.

        """
        access_error = self._access_error(tool.name, policy)
        if access_error is not None:
            return access_error
        if not tool.requires_confirmation:
            return None
        if confirmed is not True:
            return (
                f"Tool '{tool.name}' changes AYON data. Set "
                "confirm_mutation=true only after explicit user approval."
            )
        return None

    @staticmethod
    async def _run_tool(
        tool: AyonTool,
        arguments: dict[str, Any],
        retry_guard: RetrySafetyGuard | None = None,
    ) -> tuple[bool, Any]:
        """Call the tool function, recording the attempt for retry safety.

        Returns:
            ``(True, result)`` on success, ``(False, error message)`` if the
            tool raised.

        """
        try:
            result = await _invoke(tool.function, arguments)
        except Exception as exc:  # ruff: ignore[blind-except]
            if retry_guard is not None:
                retry_guard.record_attempt(
                    tool.name, arguments, ErrorClass.UNKNOWN
                )
            return False, str(exc)

        if retry_guard is not None:
            retry_guard.record_success(tool.name, arguments)
        return True, result

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a tool by name or alias, after the caller's access check.

        The base class would answer a hidden tool with a generic "not
        found"; a write tool hidden by read-only mode says why instead.

        Returns:
            The tool's result or an error.

        """
        if self._is_unknown(tool_name):
            await self._sync_addon_tools(force=True)
        access_error = self._access_error(
            tool_name, await self._current_policy()
        )
        if access_error is not None:
            return {"success": False, "error": access_error}
        return await super().call_tool(tool_name, arguments)

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a resolved tool after enforcing the mutation policy.

        Returns:
            A structured success result or an execution/policy error.

        """
        if self._is_unknown(tool_name):
            await self._sync_addon_tools(force=True)
        tool = self._tools_by_name.get(tool_name)
        if tool is None:
            return {
                "success": False,
                "error": f"Tool '{tool_name}' was not found.",
            }

        arguments = dict(arguments)
        confirmed = arguments.pop("confirm_mutation", False)
        policy = await self._current_policy()
        policy_error = self._policy_error(tool, policy, confirmed=confirmed)
        if policy_error is not None:
            return {"success": False, "error": policy_error}

        guard_chain = self._chain_for(policy)
        retry_guard = None
        if guard_chain is not None:
            guard_result = await guard_chain.check_all_async(
                tool_name, arguments
            )
            if guard_result.blocked:
                return {"success": False, "error": guard_result.final_reason}
            arguments = guard_result.repaired_args or arguments
            retry_guard = _retry_guard_of(guard_chain)

        succeeded, result = await self._run_tool(tool, arguments, retry_guard)
        if not succeeded:
            return {"success": False, "error": result}

        if guard_chain is not None:
            output_check = guard_chain.check_output_all(
                tool_name, arguments, result
            )
            if output_check.blocked:
                return {"success": False, "error": output_check.final_reason}

        return {"success": True, "result": result}


def create_discovery_tools(
    functions: list[Callable[..., Any]],
    addon_catalog: AddonCatalog | None = None,
    policy_store: PolicyStore | None = None,
) -> list[Callable[..., Any]]:
    """Create the fixed MCP surface for discovering AYON tools on demand.

    Args:
        functions: Curated FastMCP-compatible tool functions.
        addon_catalog: Tools discovered from AYON addons, if enabled.
        policy_store: Per-caller policy from the addon settings.

    Returns:
        The five callable MCP discovery tools.

    """
    provider = AyonDynamicToolProvider(
        create_curated_tools(functions),
        addon_catalog=addon_catalog,
        policy_store=policy_store,
    )

    async def list_ayon_tools(limit: int = 50) -> dict[str, Any]:
        """List AYON tools with concise descriptions.

        Returns:
            The list-tool discovery response.

        """
        return await provider.execute_dynamic_tool(
            "list_tools", {"limit": limit}
        )

    async def search_ayon_tools(query: str, limit: int = 10) -> dict[str, Any]:
        """Search AYON tools by task, name, or description.

        Returns:
            The search discovery response.

        """
        return await provider.execute_dynamic_tool(
            "search_tools", {"query": query, "limit": limit}
        )

    async def get_ayon_tool_schema(tool_name: str) -> dict[str, Any]:
        """Get the complete input schema for one AYON tool.

        Returns:
            The requested tool schema.

        """
        return await provider.execute_dynamic_tool(
            "get_tool_schema", {"tool_name": tool_name}
        )

    async def get_ayon_tool_schemas(tool_names: list[str]) -> dict[str, Any]:
        """Get input schemas for several AYON tools.

        Returns:
            The requested tool schemas.

        """
        return await provider.execute_dynamic_tool(
            "get_tool_schemas", {"tool_names": tool_names}
        )

    async def call_ayon_tool(
        tool_name: str,
        arguments: dict[str, Any],
        *,
        confirm_mutation: bool = False,
    ) -> dict[str, Any]:
        """Run one discovered AYON tool after inspecting its schema.

        Returns:
            The target tool's structured execution result.

        """
        return await provider.call_tool(
            tool_name,
            {**arguments, "confirm_mutation": confirm_mutation},
        )

    return [
        list_ayon_tools,
        search_ayon_tools,
        get_ayon_tool_schema,
        get_ayon_tool_schemas,
        call_ayon_tool,
    ]
