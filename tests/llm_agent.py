"""Ollama-driven MCP agent loop and metrics collection for LLM tests.

Not a test module itself.
"""
from __future__ import annotations

import json
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp import ClientSession, StdioServerParameters


def mcp_tools_to_ollama(tools: list[Any]) -> list[dict[str, Any]]:
    """Convert MCP tool definitions to Ollama's function-tool format.

    Returns:
        A list of Ollama ``{"type": "function", "function": {...}}`` specs.

    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }
        for tool in tools
    ]


def mcp_tools_to_claude(tools: list[Any]) -> list[dict[str, Any]]:
    """Convert MCP tool definitions to Anthropic's tool-use format.

    Returns:
        A list of Anthropic ``{"name", "description", "input_schema"}`` specs.

    """
    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.inputSchema,
        }
        for tool in tools
    ]


def _parse_tool_result(result: Any) -> Any:
    """Extract a JSON-serializable payload from a CallToolResult.

    Returns:
        ``structuredContent`` when present, otherwise the parsed (or raw)
        text of the first content block.

    """
    if getattr(result, "structuredContent", None) is not None:
        return result.structuredContent

    blocks = getattr(result, "content", None) or []
    if not blocks:
        return None
    text = getattr(blocks[0], "text", str(blocks[0]))
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


@dataclass
class ToolCallRecord:
    """One MCP tool call made during an agent run."""

    turn: int
    name: str
    resolved_name: str
    arguments: dict[str, Any]
    result: Any
    elapsed_ms: float


@dataclass
class AgentRunResult:
    """Outcome and metrics for one scenario run against the MCP server."""

    model: str
    user_prompt: str
    final_text: str = ""
    turns: int = 0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_seconds: float = 0.0
    hit_max_turns: bool = False

    def resolved_tool_names(self) -> list[str]:
        """Return the underlying AYON tool names invoked, in call order.

        Returns:
            The resolved tool names for every call made during the run.

        """
        return [call.resolved_name for call in self.tool_calls]


def _resolve_tool_name(name: str, arguments: Any) -> str:
    """Resolve the AYON tool a discovery ``call_ayon_tool`` call targets.

    Returns:
        The dispatched tool name, or ``name`` unchanged when not a
        discovery dispatcher call.

    """
    if name != "call_ayon_tool" or not isinstance(arguments, dict):
        return name
    inner = arguments.get("tool_name")
    return inner if isinstance(inner, str) and inner else name


async def _ollama_chat(
    client: httpx.AsyncClient,
    *,
    host: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    response = await client.post(
        f"{host}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {"temperature": 0.0},
        },
        timeout=300,
    )
    response.raise_for_status()
    return response.json()


async def run_agent(
    session: ClientSession,
    *,
    host: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_turns: int = 8,
) -> AgentRunResult:
    """Drive an Ollama tool-calling loop against a live MCP session.

    Returns:
        The final answer, every tool call made, and timing/token metrics.

    """
    tools_result = await session.list_tools()
    ollama_tools = mcp_tools_to_ollama(tools_result.tools)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    result = AgentRunResult(model=model, user_prompt=user_prompt)
    started = time.perf_counter()

    async with httpx.AsyncClient() as client:
        for turn in range(1, max_turns + 1):
            result.turns = turn
            response = await _ollama_chat(
                client,
                host=host,
                model=model,
                messages=messages,
                tools=ollama_tools,
            )
            result.prompt_tokens += response.get("prompt_eval_count", 0)
            result.completion_tokens += response.get("eval_count", 0)

            message = response.get("message", {})
            messages.append(message)
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                result.final_text = message.get("content", "") or ""
                break

            for tool_call in tool_calls:
                function = tool_call.get("function", {})
                name = function.get("name", "")
                arguments = function.get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {}

                call_started = time.perf_counter()
                tool_result = await session.call_tool(name, arguments)
                elapsed_ms = (time.perf_counter() - call_started) * 1000
                payload = _parse_tool_result(tool_result)

                result.tool_calls.append(
                    ToolCallRecord(
                        turn=turn,
                        name=name,
                        resolved_name=_resolve_tool_name(name, arguments),
                        arguments=arguments,
                        result=payload,
                        elapsed_ms=elapsed_ms,
                    )
                )
                messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps(payload, default=str),
                        "name": name,
                    }
                )
        else:
            result.hit_max_turns = True
            result.final_text = messages[-1].get("content", "") or ""

    result.wall_seconds = time.perf_counter() - started
    return result


def _last_assistant_text(messages: list[dict[str, Any]]) -> str:
    """Find the most recent assistant text in a Claude message history.

    Returns:
        The concatenated text blocks of the last assistant turn, or "".

    """
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        texts = [
            block.text for block in content if getattr(block, "type", None) == "text"
        ]
        if texts:
            return "".join(texts)
    return ""


async def run_agent_claude(
    session: ClientSession,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_turns: int = 8,
) -> AgentRunResult:
    """Drive a Claude tool-calling loop against a live MCP session.

    Returns:
        The final answer, every tool call made, and timing/token metrics.

    """
    import anthropic

    tools_result = await session.list_tools()
    claude_tools = mcp_tools_to_claude(tools_result.tools)

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]

    result = AgentRunResult(model=model, user_prompt=user_prompt)
    started = time.perf_counter()

    async with anthropic.AsyncAnthropic() as client:
        for turn in range(1, max_turns + 1):
            result.turns = turn
            response = await client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
                tools=claude_tools,
                temperature=0,
            )
            result.prompt_tokens += response.usage.input_tokens
            result.completion_tokens += response.usage.output_tokens

            messages.append({"role": "assistant", "content": response.content})
            tool_use_blocks = [
                block for block in response.content if block.type == "tool_use"
            ]

            if not tool_use_blocks:
                result.final_text = "".join(
                    block.text
                    for block in response.content
                    if block.type == "text"
                )
                break

            tool_results = []
            for block in tool_use_blocks:
                name = block.name
                arguments = block.input or {}

                call_started = time.perf_counter()
                tool_result = await session.call_tool(name, arguments)
                elapsed_ms = (time.perf_counter() - call_started) * 1000
                payload = _parse_tool_result(tool_result)

                result.tool_calls.append(
                    ToolCallRecord(
                        turn=turn,
                        name=name,
                        resolved_name=_resolve_tool_name(name, arguments),
                        arguments=arguments,
                        result=payload,
                        elapsed_ms=elapsed_ms,
                    )
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(payload, default=str),
                    }
                )
            messages.append({"role": "user", "content": tool_results})
        else:
            result.hit_max_turns = True
            result.final_text = _last_assistant_text(messages)

    result.wall_seconds = time.perf_counter() - started
    return result


# pylance repots this as deprecated but it isn't actually deprecated?
@asynccontextmanager  
async def open_mcp_session(
    server_params: StdioServerParameters,
) -> AsyncIterator[ClientSession]:
    """Spawn the MCP server over stdio and yield an initialized session.

    Yields:
        An initialized ``ClientSession`` connected to the spawned server.

    """
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with AsyncExitStack() as stack:
        stdio, write = await stack.enter_async_context(
            stdio_client(server_params)
        )
        session = await stack.enter_async_context(
            ClientSession(stdio, write)
        )
        await session.initialize()
        yield session


DISCOVERY_SYSTEM_PROMPT = """\
You are an assistant that operates the AYON pipeline platform through a \
small set of discovery tools. You do not know the AYON tools in advance - \
find them first:

1. Call `search_ayon_tools` (or `list_ayon_tools`) to find the operation \
you need.
2. Call `get_ayon_tool_schema` to learn its exact argument names before \
using it.
3. Call `call_ayon_tool` with `tool_name` set to that AYON tool's name and \
`arguments` set to a dict of its parameters. The tool's own parameters \
always go nested inside `arguments`, never as top-level keys alongside \
`tool_name`. For example, to call the `list_folders` AYON tool:
    call_ayon_tool(
        tool_name="list_folders",
        arguments={"project_name": "my_project"},
        confirm_mutation=false
    )
This is wrong - do not put `project_name` next to `tool_name`:
    call_ayon_tool(tool_name="list_folders", project_name="my_project")

Write operations (create/update/delete/comment) require \
`confirm_mutation=true` on the `call_ayon_tool` call - only set it when the \
user's request explicitly authorizes the change being made. `confirm_mutation` \
is always a top-level argument of `call_ayon_tool` itself, alongside \
`tool_name` and `arguments` - never put it inside `arguments`, even though \
it is not one of the underlying AYON tool's own parameters. For example, \
to call `add_comment` once authorized:
    call_ayon_tool(
        tool_name="add_comment",
        arguments={
            "project_name": "my_project",
            "entity_type": "task",
            "entity_id": "abc123",
            "body": "Looks good"
        },
        confirm_mutation=true
    )
This is wrong - `confirm_mutation` must not be inside `arguments`:
    call_ayon_tool(
        tool_name="add_comment",
        arguments={..., "confirm_mutation": true}
    )

Prefer read operations to resolve project/entity ids before mutating \
anything.

Never describe or announce a tool call instead of making it - if you know \
which tool to call next, call it in this same turn rather than saying you \
will. Only reply with plain text, with no further tool calls, once you \
actually have the answer.
"""


@dataclass
class ScenarioOutcome:
    """One scenario's result plus the pass/fail checks applied to it."""

    scenario: str
    checks: dict[str, bool]
    run: AgentRunResult

    @property
    def passed(self) -> bool:
        """Return True when every check for this scenario passed.

        Returns:
            Whether all recorded checks succeeded.

        """
        return all(self.checks.values())

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable summary of this outcome.

        Returns:
            A dict with the scenario name, checks, and run metrics.

        """
        return {
            "scenario": self.scenario,
            "passed": self.passed,
            "checks": self.checks,
            "model": self.run.model,
            "turns": self.run.turns,
            "hit_max_turns": self.run.hit_max_turns,
            "wall_seconds": round(self.run.wall_seconds, 3),
            "prompt_tokens": self.run.prompt_tokens,
            "completion_tokens": self.run.completion_tokens,
            "tool_calls": [
                {
                    "turn": call.turn,
                    "name": call.name,
                    "resolved_name": call.resolved_name,
                    "arguments": call.arguments,
                    "elapsed_ms": round(call.elapsed_ms, 2),
                }
                for call in self.run.tool_calls
            ],
            "final_text": self.run.final_text,
        }


class ScenarioReport:
    """Accumulates scenario outcomes and writes them out as JSON."""

    def __init__(self, *, model: str, exposure_mode: str, telemetry_enabled: bool):
        self._outcomes: list[ScenarioOutcome] = []
        self._model = model
        self._exposure_mode = exposure_mode
        self._telemetry_enabled = telemetry_enabled

    def add(self, outcome: ScenarioOutcome) -> None:
        """Record one scenario's outcome."""
        self._outcomes.append(outcome)

    def write(self, directory: Path) -> Path:
        """Write the collected outcomes to a timestamped JSON file.

        Returns:
            Path to the written report file.

        """
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"llm_agent_report_{timestamp}.json"

        passed = sum(1 for o in self._outcomes if o.passed)
        report = {
            "generated_at": timestamp,
            "model": self._model,
            "exposure_mode": self._exposure_mode,
            "telemetry_enabled": self._telemetry_enabled,
            "scenario_count": len(self._outcomes),
            "passed": passed,
            "failed": len(self._outcomes) - passed,
            "total_prompt_tokens": sum(
                o.run.prompt_tokens for o in self._outcomes
            ),
            "total_completion_tokens": sum(
                o.run.completion_tokens for o in self._outcomes
            ),
            "total_wall_seconds": round(
                sum(o.run.wall_seconds for o in self._outcomes), 3
            ),
            "scenarios": [o.to_dict() for o in self._outcomes],
        }
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return path
