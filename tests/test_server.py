"""Offline tests: tool registration and connection error behavior."""

import asyncio

import pytest

from ayon_mcp import connection
from ayon_mcp.app import mcp
import ayon_mcp.tools  # noqa: F401
from ayon_mcp.utils import collect

EXPECTED_TOOLS = {
    # projects
    "list_projects", "get_project", "get_server_info",
    # entities (read)
    "get_folder_hierarchy", "list_folders", "list_tasks", "list_products",
    "list_versions", "list_representations", "get_entity", "query_graphql",
    # write
    "create_entity", "update_entity", "delete_entity", "add_comment",
    # events
    "list_events", "get_event", "dispatch_event",
    # settings
    "list_addons", "list_bundles", "get_addon_settings", "set_addon_settings",
}


def test_all_tools_registered():
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert names == EXPECTED_TOOLS


def test_every_tool_has_description():
    tools = asyncio.run(mcp.list_tools())
    for tool in tools:
        assert tool.description, f"tool {tool.name} has no description"


def test_missing_credentials_is_clear_error(monkeypatch):
    monkeypatch.delenv("AYON_SERVER_URL", raising=False)
    monkeypatch.delenv("AYON_API_KEY", raising=False)
    connection.reset()
    with pytest.raises(RuntimeError, match="AYON_SERVER_URL"):
        connection.api()


def test_collect_truncates():
    result = collect(iter({"i": i} for i in range(10)), limit=3)
    assert result["count"] == 3
    assert result["truncated"] is True

    result = collect(iter({"i": i} for i in range(2)), limit=3)
    assert result["count"] == 2
    assert result["truncated"] is False
