"""On-demand discovery and execution for AYON MCP tools."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
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
    build_guard_chain,
    read_only_enabled,
)
from .rest_policy import endpoint_of, is_admin, side_effect_for

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from chuk_tool_processor.guards import GuardChain

    from .addon_discovery import AddonCatalog, AddonTool
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

    Curated tools are classified by name; generated REST tools by their
    endpoint, via ``rest_policy``. Anything else is a curated read tool.

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
        function for function in functions
        if (endpoint := endpoint_of(function)) is None
        or not is_admin(*endpoint)
    ]


def create_curated_tools(
    functions: list[Callable[..., Any]],
) -> list[AyonTool]:
    """Build discovery records from FastMCP-compatible functions.

    Returns:
        Discovery records with FastMCP-derived input schemas.

    """
    tools = []
    for function in functions:
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

        """
        super().__init__()
        self._base_tools = tools
        self._addon_catalog = addon_catalog
        self._addon_generation = -1
        self._read_only = read_only_enabled()
        self._explicit_guard_chain = guard_chain
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
        """Replace the catalog and rebuild everything derived from it.

        The retry guard is stateful and not tied to the tool list, so a
        rebuilt chain keeps the previous one and its retry history.
        """
        self._tools = tools
        self._tools_by_name = {tool.name: tool for tool in tools}
        previous_retry_guard = getattr(self, "_retry_guard", None)
        self._guard_chain = (
            build_guard_chain(tools)
            if self._explicit_guard_chain is _DEFAULT
            else self._explicit_guard_chain
        )
        retry_guard = (
            self._guard_chain.get("retry_safety")
            if self._guard_chain is not None
            else None
        )
        if (
            isinstance(retry_guard, RetrySafetyGuard)
            and previous_retry_guard is not None
            and retry_guard is not previous_retry_guard
        ):
            self._guard_chain.remove("retry_safety")
            self._guard_chain.add("retry_safety", previous_retry_guard)
            retry_guard = previous_retry_guard
        self._retry_guard = (
            retry_guard if isinstance(retry_guard, RetrySafetyGuard) else None
        )
        self.invalidate_cache()

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

        Returns:
            The discovery catalog, refreshed after addon changes.

        """
        await self._sync_addon_tools()
        return self._tools

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
        self, tool: AyonTool, *, confirmed: object
    ) -> str | None:
        """Return why a mutating call is refused, independent of guards.

        Returns:
            The refusal message, or ``None`` when the call may proceed.

        """
        if not tool.requires_confirmation:
            return None
        if self._read_only:
            return (
                f"Tool '{tool.name}' changes AYON data and is blocked: "
                "the server runs in read-only mode (AYON_MCP_READ_ONLY)."
            )
        if confirmed is not True:
            return (
                f"Tool '{tool.name}' changes AYON data. Set "
                "confirm_mutation=true only after explicit user approval."
            )
        return None

    async def _run_tool(
        self,
        tool: AyonTool,
        arguments: dict[str, Any],
    ) -> tuple[bool, Any]:
        """Call the tool function, recording the attempt for retry safety.

        Returns:
            ``(True, result)`` on success, ``(False, error message)`` if the
            tool raised.

        """
        try:
            result = await _invoke(tool.function, arguments)
        except Exception as exc:  # ruff: ignore[blind-except]
            if self._retry_guard is not None:
                self._retry_guard.record_attempt(
                    tool.name, arguments, ErrorClass.UNKNOWN
                )
            return False, str(exc)

        if self._retry_guard is not None:
            self._retry_guard.record_success(tool.name, arguments)
        return True, result

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
        policy_error = self._policy_error(tool, confirmed=confirmed)
        if policy_error is not None:
            return {"success": False, "error": policy_error}

        if self._guard_chain is not None:
            guard_result = await self._guard_chain.check_all_async(
                tool_name, arguments
            )
            if guard_result.blocked:
                return {"success": False, "error": guard_result.final_reason}
            if guard_result.repaired_args is not None:
                arguments = guard_result.repaired_args

        succeeded, result = await self._run_tool(tool, arguments)
        if not succeeded:
            return {"success": False, "error": result}

        if self._guard_chain is not None:
            output_check = self._guard_chain.check_output_all(
                tool_name, arguments, result
            )
            if output_check.blocked:
                return {"success": False, "error": output_check.final_reason}

        return {"success": True, "result": result}


def create_discovery_tools(
    functions: list[Callable[..., Any]],
    addon_catalog: AddonCatalog | None = None,
) -> list[Callable[..., Any]]:
    """Create the fixed MCP surface for discovering AYON tools on demand.

    Args:
        functions: Curated FastMCP-compatible tool functions.
        addon_catalog: Tools discovered from AYON addons, if enabled.

    Returns:
        The five callable MCP discovery tools.

    """
    provider = AyonDynamicToolProvider(
        create_curated_tools(functions), addon_catalog=addon_catalog
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
