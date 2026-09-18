"""Tests for the compact chuk-backed AYON tool discovery surface."""

from __future__ import annotations

import pytest

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