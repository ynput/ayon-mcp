"""Tests for the compact chuk-backed AYON tool discovery surface."""

from __future__ import annotations

import threading

import pytest

from ayon_mcp.client import get_global_ayon_as_user, set_global_ayon_as_user
from ayon_mcp.tool_discovery import AyonDynamicToolProvider, create_curated_tools


def get_project(project_name: str) -> dict[str, str]:
    """Get a project by name."""
    return {"name": project_name}


def delete_entity(entity_id: str) -> dict[str, str]:
    """Delete an entity."""
    return {"id": entity_id}


async def update_openapi_entity(entity_id: str) -> dict[str, str]:
    """Update an entity.

    Endpoint: PATCH /api/entities/{entity_id}
    """
    return {"id": entity_id}


@pytest.mark.asyncio
async def test_search_and_schema_are_discovered_on_demand():
    provider = AyonDynamicToolProvider(create_curated_tools([get_project]))

    search = await provider.search_tools("get project")
    schema = await provider.get_tool_schema("get_project")

    assert search[0]["name"] == "get_project"
    assert schema["function"]["parameters"]["required"] == ["project_name"]


@pytest.mark.asyncio
async def test_mutating_tool_requires_explicit_confirmation():
    provider = AyonDynamicToolProvider(create_curated_tools([delete_entity]))

    blocked = await provider.call_tool("delete_entity", {"entity_id": "123"})
    executed = await provider.call_tool(
        "delete_entity",
        {"entity_id": "123", "confirm_mutation": True},
    )

    assert blocked["success"] is False
    assert "confirm_mutation=true" in blocked["error"]
    assert executed == {"success": True, "result": {"id": "123"}}


@pytest.mark.asyncio
async def test_openapi_mutation_requires_explicit_confirmation():
    provider = AyonDynamicToolProvider(
        create_curated_tools([update_openapi_entity])
    )

    blocked = await provider.call_tool(
        "update_openapi_entity", {"entity_id": "123"}
    )
    executed = await provider.call_tool(
        "update_openapi_entity",
        {"entity_id": "123", "confirm_mutation": True},
    )

    assert blocked["success"] is False
    assert executed == {"success": True, "result": {"id": "123"}}


@pytest.mark.asyncio
async def test_sync_tools_run_off_the_event_loop_as_the_caller():
    seen = {}

    def get_user_name() -> dict[str, str]:
        """Get the current user's name."""
        seen["thread"] = threading.get_ident()
        return {"name": get_global_ayon_as_user()}

    provider = AyonDynamicToolProvider(create_curated_tools([get_user_name]))

    try:
        set_global_ayon_as_user("alice")
        result = await provider.call_tool("get_user_name", {})
    finally:
        set_global_ayon_as_user(None)

    assert result == {"success": True, "result": {"name": "alice"}}
    assert seen["thread"] != threading.get_ident()
