"""On-demand discovery and execution for AYON MCP tools."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chuk_tool_processor.discovery import BaseDynamicToolProvider
from chuk_tool_processor.guards import (
    ErrorClass,
    RetrySafetyGuard,
    SideEffectClass,
)
from fastmcp.tools.function_tool import FunctionTool

from .guardrails import (
    DESTRUCTIVE_TOOL_NAMES,
    build_guard_chain,
    read_only_enabled,
)
from .rest_policy import endpoint_of, is_admin, side_effect_for

if TYPE_CHECKING:
    from collections.abc import Callable

    from chuk_tool_processor.guards import GuardChain


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


class AyonDynamicToolProvider(BaseDynamicToolProvider[AyonTool]):
    """Expose AYON tools through chuk's compact discovery protocol."""

    def __init__(
        self,
        tools: list[AyonTool],
        guard_chain: GuardChain | object | None = _DEFAULT,
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

        """
        super().__init__()
        self._tools = tools
        self._tools_by_name = {tool.name: tool for tool in tools}
        self._read_only = read_only_enabled()
        self._guard_chain = (
            build_guard_chain(tools)
            if guard_chain is _DEFAULT
            else guard_chain
        )
        retry_guard = (
            self._guard_chain.get("retry_safety")
            if self._guard_chain is not None
            else None
        )
        self._retry_guard = (
            retry_guard if isinstance(retry_guard, RetrySafetyGuard) else None
        )

    async def get_all_tools(self) -> list[AyonTool]:
        """Return every catalogued AYON tool.

        Returns:
            The stable discovery catalog.

        """
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
            result = tool.function(**arguments)
            if inspect.isawaitable(result):
                result = await result
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
) -> list[Callable[..., Any]]:
    """Create the fixed MCP surface for discovering AYON tools on demand.

    Returns:
        The five callable MCP discovery tools.

    """
    provider = AyonDynamicToolProvider(create_curated_tools(functions))

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
