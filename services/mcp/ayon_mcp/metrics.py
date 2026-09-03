"""Token accounting."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import tiktoken
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from opentelemetry import metrics

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    import mcp.types as mcp_types
    from fastmcp.tools import ToolResult


_encoding = tiktoken.get_encoding("cl100k_base")
_meter = metrics.get_meter(__name__)
_tool_tokens = _meter.create_counter(
    name="tool_tokens",
    description="Token usage for tool calls",
    unit="tokens",
)
_tool_call_duration = _meter.create_histogram(
    name="tool_call_duration",
    description="Duration of tool calls",
    unit="milliseconds",
)
# Tools that only dispatch to another tool named in their arguments.
_DISPATCH_TOOLS = {"call_ayon_tool": "tool_name"}


def resolve_tool_name(name: str, arguments: Any) -> str:
    """Resolve the tool a call is really attributed to.

    Args:
        name: Name of the invoked tool.
        arguments: Arguments the tool was invoked with.

    Returns:
        str: Name of the dispatched tool, or ``name`` if it is not a
            dispatcher.

    """
    argument_key = _DISPATCH_TOOLS.get(name)
    if argument_key is None or not isinstance(arguments, dict):
        return name

    inner = arguments.get(argument_key)

    return inner if isinstance(inner, str) and inner else name


def count_tokens(value: Any) -> int:
    """Count tokens for any serializable value.

    Args:
        value: any serializable value.

    Returns:
        int: approximate token count.

    """
    if not isinstance(value, str):
        with suppress(Exception):
            value = json.dumps(value, default=str)

    return len(_encoding.encode(value))


class TokenMetrics(Middleware):
    """Token metrics middleware."""

    async def on_call_tool(
            self,
            context: MiddlewareContext[mcp_types.CallToolRequestParams],
            call_next: CallNext[mcp_types.CallToolRequestParams, object],
        ) -> ToolResult:
        """Count tokens for tool calls.

        Args:
            context: The middleware context for the current request.
            call_next: The next middleware or tool handler to call.

        Returns:
            The result of the next middleware or tool handler.

        """
        tool_name = resolve_tool_name(
            context.message.name, context.message.arguments
        )
        args_tokens = count_tokens(context.message.arguments or {})
        started = time.perf_counter()
        result = await call_next(context)
        elapsed = (time.perf_counter() - started) * 1000

        blocks = getattr(result, "blocks", result)
        result_tokens = count_tokens(
            [getattr(b, "text", str(b)) for b in blocks]
            if isinstance(blocks, list)
            else blocks
        )

        _tool_tokens.add(
            args_tokens + result_tokens,
            attributes={"tool_name": tool_name},
        )
        _tool_call_duration.record(
            elapsed,
            attributes={"tool_name": tool_name},
        )

        return result
