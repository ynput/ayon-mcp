"""On-demand discovery and execution for AYON MCP tools."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chuk_tool_processor.discovery import BaseDynamicToolProvider
from fastmcp.tools.function_tool import FunctionTool

if TYPE_CHECKING:
    from collections.abc import Callable


MUTATING_TOOL_NAMES = frozenset({
    "add_comment",
    "create_entity",
    "delete_entity",
    "dispatch_event",
    "set_addon_settings",
    "update_entity",
})
MUTATING_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class AyonTool:
    """A curated AYON tool exposed through the discovery provider."""

    name: str
    namespace: str
    description: str
    parameters: dict[str, Any]
    function: Callable[..., Any]
    requires_confirmation: bool = False


def _tool_description(function: Callable[..., Any]) -> str:
    """Return a useful tool description.

    Returns:
        The function docstring or a generated fallback description.

    """
    return (
        inspect.getdoc(function)
        or f"Run the {function.__name__} AYON tool."
    )


def _requires_confirmation(function: Callable[..., Any]) -> bool:
    if function.__name__ in MUTATING_TOOL_NAMES:
        return True
    docstring = inspect.getdoc(function) or ""
    return any(
        f"Endpoint: {method} " in docstring
        for method in MUTATING_HTTP_METHODS
    )


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
                requires_confirmation=_requires_confirmation(function),
            )
        )
    return tools


class AyonDynamicToolProvider(BaseDynamicToolProvider[AyonTool]):
    """Expose AYON tools through chuk's compact discovery protocol."""

    def __init__(self, tools: list[AyonTool]) -> None:
        """Initialize the provider with its available AYON tools."""
        super().__init__()
        self._tools = tools
        self._tools_by_name = {tool.name: tool for tool in tools}

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
        if tool.requires_confirmation and confirmed is not True:
            return {
                "success": False,
                "error": (
                    f"Tool '{tool.name}' changes AYON data. Set "
                    "confirm_mutation=true only after explicit user approval."
                ),
            }

        try:
            result = tool.function(**arguments)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

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
