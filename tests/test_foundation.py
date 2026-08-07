"""Tests for pagination, tool annotations and read-only mode."""
from __future__ import annotations

import pytest


@pytest.fixture()
def reset_read_only():
    """Clear the read-only override after the test."""
    from ayon_mcp.utils import set_read_only

    yield
    set_read_only(None)


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------

class TestCollectPagination:
    def test_no_offset_matches_previous_behavior(self):
        from ayon_mcp.utils import collect

        result = collect([{"id": "a"}, {"id": "b"}], limit=10)
        assert result["count"] == 2
        assert result["truncated"] is False
        assert result["next_offset"] is None

    def test_next_offset_present_when_truncated(self):
        from ayon_mcp.utils import collect

        items = [{"id": str(i)} for i in range(10)]
        result = collect(iter(items), limit=3)
        assert result["truncated"] is True
        assert result["next_offset"] == 3

    def test_offset_skips_items(self):
        from ayon_mcp.utils import collect

        items = [{"id": str(i)} for i in range(10)]
        result = collect(iter(items), limit=3, offset=3)
        assert [it["id"] for it in result["items"]] == ["3", "4", "5"]
        assert result["next_offset"] == 6

    def test_paging_walks_the_whole_sequence(self):
        from ayon_mcp.utils import collect

        items = [{"id": str(i)} for i in range(7)]
        seen: list[str] = []
        offset: int | None = 0
        while offset is not None:
            page = collect(iter(items), limit=3, offset=offset)
            seen.extend(it["id"] for it in page["items"])
            offset = page["next_offset"]
        assert seen == [str(i) for i in range(7)]

    def test_last_exact_page_is_not_truncated(self):
        from ayon_mcp.utils import collect

        items = [{"id": str(i)} for i in range(6)]
        result = collect(iter(items), limit=3, offset=3)
        assert result["count"] == 3
        assert result["truncated"] is False
        assert result["next_offset"] is None

    def test_negative_offset_is_clamped(self):
        from ayon_mcp.utils import collect

        result = collect([{"id": "a"}], limit=5, offset=-3)
        assert result["count"] == 1

    def test_list_tool_returns_next_offset(self, mock_api):
        from ayon_mcp.tools.entities import list_folders

        mock_api.get_folders.return_value = iter([
            {
                "id": f"{i:032x}", "name": f"f{i}", "label": None,
                "path": f"/f{i}", "folderType": "Folder",
                "parentId": None, "status": "Unknown", "tags": [],
                "active": True,
            }
            for i in range(5)
        ])
        result = list_folders("proj", limit=2, offset=2)
        assert [f.name for f in result.items] == ["f2", "f3"]
        assert result.truncated is True
        assert result.next_offset == 4


# ---------------------------------------------------------------------------
# tool annotations
# ---------------------------------------------------------------------------

class TestToolAnnotations:
    def test_every_tool_has_annotations(self):
        from ayon_mcp.tools import ALL_TOOLS, TOOL_ANNOTATIONS

        missing = [
            fn.__name__ for fn in ALL_TOOLS
            if fn.__name__ not in TOOL_ANNOTATIONS
        ]
        assert not missing, f"Tools without annotations: {missing}"

    def test_annotations_have_no_stale_entries(self):
        from ayon_mcp.tools import ALL_TOOLS, TOOL_ANNOTATIONS

        tool_names = {fn.__name__ for fn in ALL_TOOLS}
        stale = set(TOOL_ANNOTATIONS) - tool_names
        assert not stale, f"Annotations for unknown tools: {stale}"

    def test_write_tools_are_not_read_only(self):
        from ayon_mcp.tools import TOOL_ANNOTATIONS

        for name in (
            "create_entity", "update_entity", "delete_entity",
            "add_comment", "dispatch_event", "set_addon_settings",
            "call_rest_endpoint",
        ):
            assert TOOL_ANNOTATIONS[name]["readOnlyHint"] is False, name

    def test_delete_is_destructive(self):
        from ayon_mcp.tools import TOOL_ANNOTATIONS

        assert TOOL_ANNOTATIONS["delete_entity"]["destructiveHint"] is True

    @pytest.mark.asyncio()
    async def test_registered_tools_carry_annotations(self):
        from ayon_mcp.server import create_mcp_server

        mcp = create_mcp_server("http://localhost:5000")
        tools = {t.name: t for t in await mcp.list_tools()}
        delete_tool = tools["delete_entity"]
        assert delete_tool.annotations is not None
        assert delete_tool.annotations.readOnlyHint is False
        assert delete_tool.annotations.destructiveHint is True
        list_tool = tools["list_folders"]
        assert list_tool.annotations is not None
        assert list_tool.annotations.readOnlyHint is True


# ---------------------------------------------------------------------------
# read-only mode
# ---------------------------------------------------------------------------

class TestReadOnlyMode:
    def test_env_flag_parsing(self, monkeypatch):
        from ayon_mcp.utils import env_flag

        for value in ("1", "true", "Yes", "ON"):
            monkeypatch.setenv("AYON_MCP_READ_ONLY", value)
            assert env_flag("AYON_MCP_READ_ONLY") is True
        for value in ("", "0", "false", "off"):
            monkeypatch.setenv("AYON_MCP_READ_ONLY", value)
            assert env_flag("AYON_MCP_READ_ONLY") is False

    def test_override_wins_over_env(self, monkeypatch, reset_read_only):
        from ayon_mcp.utils import read_only_enabled, set_read_only

        monkeypatch.setenv("AYON_MCP_READ_ONLY", "true")
        set_read_only(False)
        assert read_only_enabled() is False
        set_read_only(None)
        assert read_only_enabled() is True

    @pytest.mark.asyncio()
    async def test_write_tools_hidden_in_read_only_mode(
        self, monkeypatch, reset_read_only
    ):
        from ayon_mcp.server import create_mcp_server
        from ayon_mcp.tools import TOOL_ANNOTATIONS

        monkeypatch.setenv("AYON_MCP_READ_ONLY", "true")
        mcp = create_mcp_server("http://localhost:5000")
        tools = {t.name: t for t in await mcp.list_tools()}

        for name, annotations in TOOL_ANNOTATIONS.items():
            if name == "call_rest_endpoint":
                continue
            expected = annotations.get("readOnlyHint", False)
            assert (name in tools) is expected, name

    @pytest.mark.asyncio()
    async def test_rest_gateway_registered_read_only(
        self, monkeypatch, reset_read_only
    ):
        from ayon_mcp.server import create_mcp_server

        monkeypatch.setenv("AYON_MCP_READ_ONLY", "true")
        mcp = create_mcp_server("http://localhost:5000")
        tools = {t.name: t for t in await mcp.list_tools()}

        gateway = tools["call_rest_endpoint"]
        assert gateway.annotations is not None
        assert gateway.annotations.readOnlyHint is True

    @pytest.mark.asyncio()
    async def test_call_rest_endpoint_blocks_writes(
        self, monkeypatch, reset_read_only
    ):
        from ayon_mcp.tools.rest import call_rest_endpoint

        monkeypatch.setenv("AYON_MCP_READ_ONLY", "true")
        with pytest.raises(RuntimeError, match="read-only mode"):
            await call_rest_endpoint("POST", "/api/projects")

    @pytest.mark.asyncio()
    async def test_read_only_instructions_note(
        self, monkeypatch, reset_read_only
    ):
        from ayon_mcp.instructions import READ_ONLY_NOTE
        from ayon_mcp.server import create_mcp_server

        monkeypatch.setenv("AYON_MCP_READ_ONLY", "true")
        mcp = create_mcp_server("http://localhost:5000")
        assert READ_ONLY_NOTE.strip() in (mcp.instructions or "")

    def test_remote_settings_fetch_parses_read_only(self, monkeypatch):
        import httpx

        from ayon_mcp.server import _fetch_addon_read_only_setting

        monkeypatch.setenv("AYON_ADDON_VERSION", "0.0.1")
        monkeypatch.setenv("AYON_API_KEY", "key")

        def fake_get(url, **kwargs):
            request = httpx.Request("GET", url)
            return httpx.Response(
                200, json={"read_only": True}, request=request
            )

        monkeypatch.setattr(httpx, "get", fake_get)
        assert _fetch_addon_read_only_setting("http://x") is True

    def test_remote_settings_fetch_survives_errors(self, monkeypatch):
        import httpx

        from ayon_mcp.server import _fetch_addon_read_only_setting

        monkeypatch.setenv("AYON_ADDON_VERSION", "0.0.1")
        monkeypatch.setenv("AYON_API_KEY", "key")

        def fake_get(url, **kwargs):
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(httpx, "get", fake_get)
        assert _fetch_addon_read_only_setting("http://x") is None

    def test_remote_settings_fetch_needs_service_env(self, monkeypatch):
        from ayon_mcp.server import _fetch_addon_read_only_setting

        monkeypatch.delenv("AYON_ADDON_VERSION", raising=False)
        monkeypatch.delenv("AYON_API_KEY", raising=False)
        assert _fetch_addon_read_only_setting("http://x") is None
