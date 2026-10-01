"""Tests for the chuk-tool-processor guard chain wired into tool_discovery."""

from __future__ import annotations

import pytest

from ayon_mcp.guardrails import GuardsMode, guards_mode
from ayon_mcp.tool_discovery import (
    AyonDynamicToolProvider,
    create_curated_tools,
    drop_mutating_tools,
)


def get_project(project_name: str) -> dict[str, str]:
    """Get a project by name."""
    return {"name": project_name}


def delete_entity(entity_id: str) -> dict[str, str]:
    """Delete an entity."""
    return {"id": entity_id}


def create_entity(entity_id: str) -> dict[str, str]:
    """Create an entity."""
    return {"id": entity_id}


@pytest.fixture(autouse=True)
def _clean_guard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "AYON_MCP_GUARDS",
        "AYON_MCP_READ_ONLY",
        "AYON_SERVER_URL",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
async def test_read_only_blocks_writes_even_with_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AYON_MCP_READ_ONLY", "true")
    provider = AyonDynamicToolProvider(create_curated_tools([create_entity]))

    result = await provider.call_tool(
        "create_entity", {"entity_id": "123", "confirm_mutation": True}
    )

    assert result["success"] is False
    assert "read-only" in result["error"].lower()


@pytest.mark.asyncio
async def test_read_only_still_allows_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AYON_MCP_READ_ONLY", "true")
    provider = AyonDynamicToolProvider(create_curated_tools([get_project]))

    result = await provider.call_tool("get_project", {"project_name": "demo"})

    assert result == {"success": True, "result": {"name": "demo"}}


@pytest.mark.asyncio
async def test_read_only_is_enforced_with_guards_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AYON_MCP_READ_ONLY", "true")
    monkeypatch.setenv("AYON_MCP_GUARDS", "off")
    provider = AyonDynamicToolProvider(create_curated_tools([create_entity]))

    result = await provider.call_tool(
        "create_entity", {"entity_id": "123", "confirm_mutation": True}
    )

    assert result["success"] is False
    assert "read-only" in result["error"].lower()


def test_drop_mutating_tools_keeps_only_reads() -> None:
    assert drop_mutating_tools(
        [get_project, create_entity, delete_entity]
    ) == [get_project]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, GuardsMode.DEFAULT),
        ("default", GuardsMode.DEFAULT),
        ("true", GuardsMode.DEFAULT),
        ("STRICT", GuardsMode.STRICT),
        ("off", GuardsMode.OFF),
        ("0", GuardsMode.OFF),
        ("bogus", GuardsMode.DEFAULT),
    ],
)
def test_guards_mode_parsing(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    expected: GuardsMode,
) -> None:
    if value is not None:
        monkeypatch.setenv("AYON_MCP_GUARDS", value)

    assert guards_mode() is expected


@pytest.mark.asyncio
async def test_default_mode_allows_destructive_with_confirmation() -> None:
    provider = AyonDynamicToolProvider(create_curated_tools([delete_entity]))

    result = await provider.call_tool(
        "delete_entity", {"entity_id": "123", "confirm_mutation": True}
    )

    assert result == {"success": True, "result": {"id": "123"}}


@pytest.mark.asyncio
async def test_strict_mode_blocks_destructive_regardless_of_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AYON_MCP_GUARDS", "strict")
    provider = AyonDynamicToolProvider(create_curated_tools([delete_entity]))

    result = await provider.call_tool(
        "delete_entity", {"entity_id": "123", "confirm_mutation": True}
    )

    assert result["success"] is False
    assert "destructive" in result["error"].lower()


@pytest.mark.asyncio
async def test_strict_mode_still_allows_writes_with_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AYON_MCP_GUARDS", "strict")
    provider = AyonDynamicToolProvider(create_curated_tools([create_entity]))

    result = await provider.call_tool(
        "create_entity", {"entity_id": "123", "confirm_mutation": True}
    )

    assert result == {"success": True, "result": {"id": "123"}}


@pytest.mark.asyncio
async def test_strict_mode_blocks_repeated_identical_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AYON_MCP_GUARDS", "strict")
    calls = []

    def get_event(event_id: str) -> dict[str, str]:
        """Get an event."""
        calls.append(event_id)
        msg = "boom"
        raise RuntimeError(msg)

    provider = AyonDynamicToolProvider(create_curated_tools([get_event]))

    for _ in range(3):
        result = await provider.call_tool("get_event", {"event_id": "1"})
        assert result == {"success": False, "error": "boom"}

    result = await provider.call_tool("get_event", {"event_id": "1"})

    assert result["success"] is False
    assert "retries" in result["error"].lower()
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_guards_off_skips_the_chain_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AYON_MCP_GUARDS", "off")
    provider = AyonDynamicToolProvider(create_curated_tools([get_project]))

    result = await provider.call_tool(
        "get_project", {"project_name": "demo", "extra_bogus_arg": "x"}
    )

    # Without the schema guard the unknown argument reaches the function.
    assert result["success"] is False
    assert "extra_bogus_arg" in result["error"]
    assert "unexpected keyword" in result["error"]


@pytest.mark.asyncio
async def test_unknown_argument_is_rejected_by_schema_guard() -> None:
    provider = AyonDynamicToolProvider(create_curated_tools([get_project]))

    result = await provider.call_tool(
        "get_project", {"project_name": "demo", "extra_bogus_arg": "x"}
    )

    assert result["success"] is False
    assert "extra_bogus_arg" in result["error"]
