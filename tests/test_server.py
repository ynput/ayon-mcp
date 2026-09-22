"""Tests for the AYON MCP server."""
from __future__ import annotations

import os
import pytest
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# utils
# ---------------------------------------------------------------------------

class TestCollect:
    def test_returns_all_items_when_under_limit(self):
        from ayon_mcp.utils import collect

        result = collect([{"id": "a"}, {"id": "b"}], limit=10)
        assert result == {"count": 2, "truncated": False, "items": [{"id": "a"}, {"id": "b"}]}

    def test_truncates_and_flags_when_over_limit(self):
        from ayon_mcp.utils import collect

        items = [{"id": str(i)} for i in range(5)]
        result = collect(iter(items), limit=3)
        assert result["count"] == 3
        assert result["truncated"] is True
        assert len(result["items"]) == 3

    def test_clamps_limit_to_max(self):
        from ayon_mcp.utils import collect, MAX_LIMIT

        items = list(range(MAX_LIMIT + 10))
        result = collect(iter(items), limit=MAX_LIMIT + 100)
        assert result["count"] == MAX_LIMIT
        assert result["truncated"] is True

    def test_clamps_limit_to_minimum_of_one(self):
        from ayon_mcp.utils import collect

        result = collect([{"id": "x"}, {"id": "y"}], limit=0)
        assert result["count"] == 1

    def test_empty_input(self):
        from ayon_mcp.utils import collect

        result = collect([], limit=10)
        assert result == {"count": 0, "truncated": False, "items": []}


class TestEntityFields:
    def test_returns_base_fields_when_attrib_excluded(self):
        from ayon_mcp.utils import entity_fields

        base = {"id", "name"}
        result = entity_fields(base, include_attrib=False)
        assert result == {"id", "name"}
        assert "attrib" not in result

    def test_adds_attrib_when_requested(self):
        from ayon_mcp.utils import entity_fields

        result = entity_fields({"id"}, include_attrib=True)
        assert "attrib" in result

    def test_does_not_mutate_base_set(self):
        from ayon_mcp.utils import entity_fields

        base = {"id"}
        entity_fields(base, include_attrib=True)
        assert "attrib" not in base


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class TestGlobalClient:
    def test_raises_before_initialization(self):
        from contextvars import copy_context

        def _run():
            from ayon_mcp import client as client_mod
            client_mod._CLIENT_CV.set(None)
            with pytest.raises(RuntimeError, match="not been initialized"):
                client_mod.get_global_ayon_client()

        copy_context().run(_run)

    def test_returns_set_client(self):
        from ayon_mcp.client import set_global_ayon_client, get_global_ayon_client

        fake = MagicMock()
        set_global_ayon_client(fake)
        assert get_global_ayon_client() is fake

    def test_get_ayon_api_raises_on_invalid_token(self, monkeypatch):
        from ayon_mcp import client as client_mod

        mock_server = MagicMock()
        mock_server.has_valid_token = False
        monkeypatch.setattr(client_mod.ayon_api, "ServerAPI", lambda *a, **kw: mock_server)

        with pytest.raises(RuntimeError, match="Could not authenticate"):
            client_mod.get_ayon_api("http://localhost:5000", "bad-key")

    def test_get_ayon_api_returns_client_on_valid_token(self, monkeypatch):
        from ayon_mcp import client as client_mod

        mock_server = MagicMock()
        mock_server.has_valid_token = True
        monkeypatch.setattr(client_mod.ayon_api, "ServerAPI", lambda *a, **kw: mock_server)

        result = client_mod.get_ayon_api("http://localhost:5000", "valid-key")
        assert result is mock_server


# ---------------------------------------------------------------------------
# server factory
# ---------------------------------------------------------------------------

class TestCreateMcpServer:
    def test_returns_fastmcp_instance(self):
        from fastmcp import FastMCP
        from ayon_mcp.server import create_mcp_server

        mcp = create_mcp_server(
            "http://localhost:5000", 
            os.getenv("AYON_API_KEY", ""))
        assert isinstance(mcp, FastMCP)

    def test_server_has_correct_name(self):
        from ayon_mcp.server import create_mcp_server

        mcp = create_mcp_server(
            "http://localhost:5000",
            os.getenv("AYON_API_KEY", ""))
        assert mcp.name == "AYON MCP Server"

    def test_discovery_tools_are_registered(self):
        import asyncio
        from ayon_mcp.server import create_mcp_server

        mcp = create_mcp_server("http://localhost:5000", os.getenv("AYON_API_KEY", ""))
        registered = {t.name for t in asyncio.run(mcp.list_tools())}
        expected = {
            "list_ayon_tools",
            "search_ayon_tools",
            "get_ayon_tool_schema",
            "get_ayon_tool_schemas",
            "call_ayon_tool",
        }
        assert expected == registered

    def test_direct_exposure_mode_preserves_legacy_tools(self, monkeypatch):
        import asyncio
        from ayon_mcp.server import create_mcp_server
        from ayon_mcp.tools import ALL_TOOLS

        monkeypatch.setenv("AYON_MCP_TOOL_EXPOSURE", "direct")
        mcp = create_mcp_server("http://localhost:5000", os.getenv("AYON_API_KEY", ""))

        registered = {tool.name for tool in asyncio.run(mcp.list_tools())}

        assert registered == {
            getattr(function, "__name__", "") for function in ALL_TOOLS
        }


# ---------------------------------------------------------------------------
# tools/projects
# ---------------------------------------------------------------------------

class TestListProjects:
    def test_returns_project_list(self, mock_api):
        from ayon_mcp.tools.projects import list_projects

        mock_api.get_projects.return_value = [
            {"name": "demo", "code": "DEM", "active": True, "createdAt": "2024-01-01T00:00:00Z"},
        ]
        result = list_projects()
        assert result.count == 1
        assert result.items[0].name == "demo"

    def test_filters_active_by_default(self, mock_api):
        from ayon_mcp.tools.projects import list_projects

        mock_api.get_projects.return_value = []
        list_projects()
        mock_api.get_projects.assert_called_once_with(
            active=True, fields={"name", "code", "active", "createdAt"}
        )

    def test_include_inactive_passes_none(self, mock_api):
        from ayon_mcp.tools.projects import list_projects

        mock_api.get_projects.return_value = []
        list_projects(include_inactive=True)
        mock_api.get_projects.assert_called_once_with(
            active=None, fields={"name", "code", "active", "createdAt"}
        )


class TestGetProject:
    def test_returns_project_dict(self, mock_api):
        from ayon_mcp.tools.projects import get_project

        mock_api.get_project.return_value = {"name": "demo", "code": "DEM"}
        result = get_project("demo")
        assert result["name"] == "demo"

    def test_raises_when_not_found(self, mock_api):
        from ayon_mcp.tools.projects import get_project

        mock_api.get_project.return_value = None
        with pytest.raises(RuntimeError, match="not found"):
            get_project("missing")


class TestGetServerInfo:
    def test_returns_server_info(self, mock_api):
        from ayon_mcp.tools.projects import get_server_info

        mock_api.get_user.return_value = {
            "name": "admin",
            "data": {"isAdmin": True, "isService": False},
        }
        mock_api.base_url = "http://localhost:5000"
        mock_api.get_server_version.return_value = "1.2.3"

        result = get_server_info()
        assert result.server_url == "http://localhost:5000"
        assert result.server_version == "1.2.3"
        assert result.user_name == "admin"
        assert result.user_is_admin is True
        assert result.user_is_service is False


# ---------------------------------------------------------------------------
# tools/entities
# ---------------------------------------------------------------------------

class TestGetFolderHierarchy:
    def test_returns_hierarchy(self, mock_api):
        from ayon_mcp.tools.entities import get_folder_hierarchy

        mock_api.get_folders_hierarchy.return_value = {
            "hierarchy": [
                {
                    "id": "abc",
                    "name": "assets",
                    "label": "Assets",
                    "status": "In Progress",
                    "folderType": "Folder",
                    "hasTasks": False,
                    "taskNames": [],
                    "parents": [],
                    "parentId": None,
                    "children": [],
                }
            ]
        }
        result = get_folder_hierarchy("demo")
        assert len(result.hierarchy) == 1
        assert result.hierarchy[0].name == "assets"

    def test_passes_search_and_types(self, mock_api):
        from ayon_mcp.tools.entities import get_folder_hierarchy

        mock_api.get_folders_hierarchy.return_value = {"hierarchy": []}
        get_folder_hierarchy("demo", search="sq010", folder_types=["Shot"])
        mock_api.get_folders_hierarchy.assert_called_once_with(
            "demo", search_string="sq010", folder_types=["Shot"]
        )


class TestListFolders:
    def test_returns_entity_list(self, mock_api):
        from ayon_mcp.tools.entities import list_folders

        mock_api.get_folders.return_value = iter([
            {
                "id": "f1",
                "name": "shot010",
                "label": "Shot 010",
                "path": "/shots/sq01/shot010",
                "folderType": "Shot",
                "parentId": None,
                "status": "In Progress",
                "tags": [],
                "active": True,
            }
        ])
        result = list_folders("demo")
        assert result.count == 1
        assert result.items[0].name == "shot010"
        assert result.truncated is False

    def test_truncation_flag(self, mock_api):
        from ayon_mcp.tools.entities import list_folders

        def _make_folder(i):
            return {
                "id": f"f{i}", "name": f"shot{i:03}", "label": None,
                "path": f"/shots/shot{i:03}", "folderType": "Shot",
                "parentId": None, "status": "In Progress", "tags": [], "active": True,
            }

        mock_api.get_folders.return_value = iter([_make_folder(i) for i in range(6)])
        result = list_folders("demo", limit=5)
        assert result.truncated is True
        assert result.count == 5


# ---------------------------------------------------------------------------
# tools/write
# ---------------------------------------------------------------------------

class TestCheckEntityType:
    def test_valid_types_do_not_raise(self):
        from ayon_mcp.tools.write import _check_entity_type

        for t in ("folder", "task", "product", "version"):
            _check_entity_type(t)  # should not raise

    def test_invalid_type_raises(self):
        from ayon_mcp.tools.write import _check_entity_type

        with pytest.raises(RuntimeError, match="Unsupported entity_type"):
            _check_entity_type("representation")


class TestCreateEntity:
    def test_returns_response_with_entity_id(self, mock_api):
        from ayon_mcp.tools.write import create_entity

        mock_api.send_batch_operations.return_value = [{"entityId": "new-id-123"}]
        result = create_entity("demo", "folder", {"name": "shots", "folderType": "Folder"})

        assert result.entity_id == "new-id-123"
        assert result.entity_type == "folder"
        assert result.project == "demo"

    def test_raises_on_invalid_entity_type(self, mock_api):
        from ayon_mcp.tools.write import create_entity

        with pytest.raises(RuntimeError):
            create_entity("demo", "scene", {"name": "x"})



class TestUpdateEntity:
    def test_returns_updated_fields(self, mock_api):
        from ayon_mcp.tools.write import update_entity

        mock_api.send_batch_operations.return_value = []
        result = update_entity("demo", "task", "task-id", {"status": "Approved"})

        assert result.entity_id == "task-id"
        assert result.updated == ["status"]

    def test_raises_on_invalid_entity_type(self, mock_api):
        from ayon_mcp.tools.write import update_entity

        with pytest.raises(RuntimeError):
            update_entity("demo", "scene", "id", {})


class TestDeleteEntity:
    def test_returns_deleted_flag(self, mock_api):
        from ayon_mcp.tools.write import delete_entity

        mock_api.send_batch_operations.return_value = []
        result = delete_entity("demo", "folder", "folder-id")

        assert result.deleted is True
        assert result.entity_id == "folder-id"

    def test_sends_correct_operation(self, mock_api):
        from ayon_mcp.tools.write import delete_entity

        mock_api.send_batch_operations.return_value = []
        delete_entity("demo", "version", "ver-id")

        mock_api.send_batch_operations.assert_called_once_with(
            "demo",
            [{"type": "delete", "entityType": "version", "entityId": "ver-id"}],
        )


class TestAddComment:
    def test_returns_comment_response(self, mock_api):
        from ayon_mcp.tools.write import add_comment

        mock_api.create_activity.return_value = "act-id"
        result = add_comment("demo", "task", "task-id", "Looks good!")

        assert result.activity_id == "act-id"
        assert result.entity_id == "task-id"
        assert result.entity_type == "task"

    def test_calls_create_activity_with_correct_args(self, mock_api):
        from ayon_mcp.tools.write import add_comment

        mock_api.create_activity.return_value = "act-id"
        add_comment("demo", "folder", "folder-id", "Check this out")

        mock_api.create_activity.assert_called_once_with(
            "demo", "folder-id", "folder", "comment", body="Check this out"
        )


# ---------------------------------------------------------------------------
# tools/events
# ---------------------------------------------------------------------------

class TestListEvents:
    def test_returns_events(self, mock_api):
        from ayon_mcp.tools.events import list_events

        mock_api.get_events.return_value = iter([
            {"id": "evt1", "topic": "entity.folder.created", "createdAt": "2024-01-01T00:00:00Z"}
        ])
        result = list_events()
        assert result["count"] == 1
        assert result["items"][0]["id"] == "evt1"

    def test_passes_filters_to_api(self, mock_api):
        from ayon_mcp.tools.events import list_events

        mock_api.get_events.return_value = iter([])
        list_events(topics=["entity.folder.*"], users=["admin"])
        mock_api.get_events.assert_called_once()
        call_kwargs = mock_api.get_events.call_args.kwargs
        assert call_kwargs["topics"] == ["entity.folder.*"]
        assert call_kwargs["users"] == ["admin"]


class TestGetEvent:
    def test_returns_event_item(self, mock_api):
        from ayon_mcp.tools.events import get_event

        mock_api.get_event.return_value = {
            "id": "evt1",
            "topic": "entity.folder.created",
            "hash": None,
            "sender": None,
            "senderType": None,
            "project": "demo",
            "user": "admin",
            "dependsOn": None,
            "status": "finished",
            "retries": 0,
            "description": "folder created",
            "summary": {},
            "payload": {},
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-01T00:00:00Z",
        }
        result = get_event("evt1")
        assert result.id == "evt1"
        assert result.topic == "entity.folder.created"

    def test_raises_when_not_found(self, mock_api):
        from ayon_mcp.tools.events import get_event

        mock_api.get_event.return_value = None
        with pytest.raises(RuntimeError, match="not found"):
            get_event("missing-id")


class TestDispatchEvent:
    def test_returns_event_item(self, mock_api):
        from ayon_mcp.tools.events import dispatch_event

        response = MagicMock()
        response.data = {"id": "new-evt"}
        mock_api.dispatch_event.return_value = response

        result = dispatch_event("mytool.sync.finished", project_name="demo")
        assert result.topic == "mytool.sync.finished"
        assert result.project == "demo"
        assert result.status == "finished"

    def test_pending_status_when_not_finished(self, mock_api):
        from ayon_mcp.tools.events import dispatch_event

        response = MagicMock()
        response.data = {"id": "pending-evt"}
        mock_api.dispatch_event.return_value = response

        result = dispatch_event("mytool.job.started", finished=False)
        assert result.status == "pending"


# ---------------------------------------------------------------------------
# tools/settings
# ---------------------------------------------------------------------------

class TestListAddons:
    def test_returns_addon_list(self, mock_api):
        from ayon_mcp.tools.settings import list_addons

        mock_api.get_addons_info.return_value = {
            "addons": [
                {"name": "core", "title": "Core", "versions": {"1.0.0": {}, "1.1.0": {}}},
                {"name": "maya", "title": "Maya", "versions": {"0.9.0": {}}},
            ]
        }
        result = list_addons()
        assert result.count == 2
        names = [a.name for a in result.items]
        assert "core" in names
        assert "maya" in names

    def test_versions_are_sorted(self, mock_api):
        from ayon_mcp.tools.settings import list_addons

        mock_api.get_addons_info.return_value = {
            "addons": [
                {"name": "core", "title": None, "versions": {"1.1.0": {}, "0.9.0": {}, "1.0.0": {}}},
            ]
        }
        result = list_addons()
        assert result.items[0].versions == ["0.9.0", "1.0.0", "1.1.0"]


class TestListBundles:
    def test_returns_bundle_list(self, mock_api):
        from ayon_mcp.tools.settings import list_bundles

        mock_api.get_bundles.return_value = {
            "bundles": [
                {
                    "name": "prod-bundle",
                    "isProduction": True,
                    "isStaging": False,
                    "isDev": False,
                    "addons": {"core": "1.0.0"},
                }
            ]
        }
        result = list_bundles()
        assert result.count == 1
        assert result.items[0].name == "prod-bundle"
        assert result.items[0].is_production is True


@pytest.mark.asyncio
@pytest.mark.server
async def test_mcp_server_tools_list():
    """Connect to a local MCP server via stdio and verify the exposed tool list."""
    import os
    import pathlib
    import sys

    scripts_dir = pathlib.Path(__file__).parent.parent / "scripts"
    if sys.platform == "win32":
        server_command = "powershell"
        server_script = scripts_dir / "start_local.ps1"
    else:
        server_command = "bash"
        server_script = scripts_dir / "start_local.sh"

    # The CLI requires a non-empty API key; use the env value or a placeholder since
    # no actual AYON API calls are made during tool listing.
    env = {**os.environ, "AYON_API_KEY": os.environ.get("AYON_API_KEY", "test-key-for-listing")}

    server_params = StdioServerParameters(
        command=server_command,
        args=[str(server_script)],
        env=env,
    )

    expected_tools = sorted([
        "call_ayon_tool",
        "get_ayon_tool_schema",
        "get_ayon_tool_schemas",
        "list_ayon_tools",
        "search_ayon_tools",
    ])

    async with AsyncExitStack() as stack:
        transport = await stack.enter_async_context(stdio_client(server_params))
        stdio, write = transport
        session = await stack.enter_async_context(ClientSession(stdio, write))
        await session.initialize()

        tools_result = await session.list_tools()
        tool_names = sorted(tool.name for tool in tools_result.tools)

        print("\nMCP Server Tools:")
        for tool in tools_result.tools:
            print(f"  {tool.name}: {tool.description}")

    assert expected_tools == tool_names


# ---------------------------------------------------------------------------
# tools/entities – list_tasks
# ---------------------------------------------------------------------------

class TestListTasks:
    def test_returns_task_list(self, mock_api):
        from ayon_mcp.tools.entities import list_tasks

        mock_api.get_tasks.return_value = iter([
            {
                "id": "t1",
                "name": "model",
                "label": "Modeling",
                "taskType": "Modeling",
                "folderId": "f1",
                "assignees": ["alice"],
                "status": "In Progress",
                "tags": [],
                "active": True,
            }
        ])
        result = list_tasks("demo")
        assert result.count == 1
        assert result.items[0].name == "model"
        assert result.items[0].assignees == ["alice"]

    def test_passes_filters_to_api(self, mock_api):
        from ayon_mcp.tools.entities import list_tasks

        mock_api.get_tasks.return_value = iter([])
        list_tasks(
            "demo",
            folder_id="f1",
            task_names=["rendering"],
            task_types=["Modeling"],
            assignees=["bob"],
        )
        call_kwargs = mock_api.get_tasks.call_args.kwargs
        assert call_kwargs["folder_ids"] == ["f1"]
        assert call_kwargs["task_names"] == ["rendering"]
        assert call_kwargs["task_types"] == ["Modeling"]
        assert call_kwargs["assignees"] == ["bob"]

    def test_truncation(self, mock_api):
        from ayon_mcp.tools.entities import list_tasks

        def _task(i):
            return {
                "id": f"t{i}", "name": f"task{i}", "label": None,
                "taskType": "Compositing", "folderId": "f1",
                "assignees": [], "status": "Todo", "tags": [], "active": True,
            }

        mock_api.get_tasks.return_value = iter([_task(i) for i in range(6)])
        result = list_tasks("demo", limit=3)
        assert result.count == 3
        assert result.truncated is True


# ---------------------------------------------------------------------------
# tools/entities – list_products
# ---------------------------------------------------------------------------

class TestListProducts:
    def test_returns_product_list(self, mock_api):
        from ayon_mcp.tools.entities import list_products

        mock_api.get_products.return_value = iter([
            {
                "id": "p1",
                "name": "modelMain",
                "productType": "model",
                "folderId": "f1",
                "status": "Approved",
                "tags": [],
                "active": True,
            }
        ])
        result = list_products("demo")
        assert result.count == 1
        assert result.items[0].name == "modelMain"
        assert result.items[0].product_type == "model"

    def test_passes_filters_to_api(self, mock_api):
        from ayon_mcp.tools.entities import list_products

        mock_api.get_products.return_value = iter([])
        list_products("demo", folder_id="f1", product_types=["render"], name_regex=".*beauty.*")
        call_kwargs = mock_api.get_products.call_args.kwargs
        assert call_kwargs["folder_ids"] == ["f1"]
        assert call_kwargs["product_types"] == ["render"]
        assert call_kwargs["product_name_regex"] == ".*beauty.*"

    def test_none_folder_id_passes_none(self, mock_api):
        from ayon_mcp.tools.entities import list_products

        mock_api.get_products.return_value = iter([])
        list_products("demo")
        call_kwargs = mock_api.get_products.call_args.kwargs
        assert call_kwargs["folder_ids"] is None


# ---------------------------------------------------------------------------
# tools/entities – list_versions
# ---------------------------------------------------------------------------

class TestListVersions:
    def test_returns_version_list(self, mock_api):
        from ayon_mcp.tools.entities import list_versions

        mock_api.get_versions.return_value = iter([
            {
                "id": "v1",
                "version": 3,
                "productId": "p1",
                "taskId": "t1",
                "author": "alice",
                "status": "Approved",
                "tags": [],
                "active": True,
                "createdAt": "2024-01-01T00:00:00Z",
            }
        ])
        result = list_versions("demo")
        assert result.count == 1
        assert result.items[0].version == 3
        assert result.items[0].author == "alice"

    def test_latest_only_flag(self, mock_api):
        from ayon_mcp.tools.entities import list_versions

        mock_api.get_versions.return_value = iter([])
        list_versions("demo", latest_only=True)
        call_kwargs = mock_api.get_versions.call_args.kwargs
        assert call_kwargs["latest"] is True

    def test_latest_false_when_not_latest_only(self, mock_api):
        from ayon_mcp.tools.entities import list_versions

        mock_api.get_versions.return_value = iter([])
        list_versions("demo", latest_only=False)
        call_kwargs = mock_api.get_versions.call_args.kwargs
        assert call_kwargs["latest"] is None

    def test_filters_by_product_id(self, mock_api):
        from ayon_mcp.tools.entities import list_versions

        mock_api.get_versions.return_value = iter([])
        list_versions("demo", product_id="p1")
        call_kwargs = mock_api.get_versions.call_args.kwargs
        assert call_kwargs["product_ids"] == ["p1"]


# ---------------------------------------------------------------------------
# tools/entities – list_representations
# ---------------------------------------------------------------------------

class TestListRepresentations:
    def test_returns_representation_list(self, mock_api):
        from ayon_mcp.tools.entities import list_representations

        mock_api.get_representations.return_value = iter([
            {
                "id": "r1",
                "name": "exr",
                "versionId": "v1",
                "status": "Approved",
                "tags": [],
                "active": True,
            }
        ])
        result = list_representations("demo")
        assert result.count == 1
        assert result.items[0].name == "exr"

    def test_passes_names_filter(self, mock_api):
        from ayon_mcp.tools.entities import list_representations

        mock_api.get_representations.return_value = iter([])
        list_representations("demo", names=["exr", "mov"])
        call_kwargs = mock_api.get_representations.call_args.kwargs
        assert call_kwargs["representation_names"] == ["exr", "mov"]

    def test_include_files_adds_files_field(self, mock_api):
        from ayon_mcp.tools.entities import list_representations

        mock_api.get_representations.return_value = iter([])
        list_representations("demo", include_files=True)
        call_kwargs = mock_api.get_representations.call_args.kwargs
        assert "files" in call_kwargs["fields"]

    def test_exclude_files_by_default(self, mock_api):
        from ayon_mcp.tools.entities import list_representations

        mock_api.get_representations.return_value = iter([])
        list_representations("demo")
        call_kwargs = mock_api.get_representations.call_args.kwargs
        assert "files" not in call_kwargs["fields"]


# ---------------------------------------------------------------------------
# tools/entities – get_entity
# ---------------------------------------------------------------------------

class TestGetEntity:
    def test_returns_folder_model(self, mock_api):
        from ayon_mcp.tools.entities import get_entity, Folder

        mock_api.get_folder_by_id.return_value = {
            "id": "f1",
            "name": "shots",
            "label": "Shots",
            "path": "/shots",
            "folderType": "Folder",
            "parentId": None,
            "status": "In Progress",
            "tags": [],
            "active": True,
        }
        result = get_entity("demo", "folder", "f1")
        assert isinstance(result, Folder)
        assert result.id == "f1"
        assert result.name == "shots"

    def test_returns_task_model(self, mock_api):
        from ayon_mcp.tools.entities import get_entity, Task

        mock_api.get_task_by_id.return_value = {
            "id": "t1",
            "name": "model",
            "label": None,
            "taskType": "Modeling",
            "folderId": "f1",
            "assignees": [],
            "status": "Todo",
            "tags": [],
            "active": True,
        }
        result = get_entity("demo", "task", "t1")
        assert isinstance(result, Task)
        assert result.task_type == "Modeling"

    def test_raises_on_unknown_entity_type(self, mock_api):
        from ayon_mcp.tools.entities import get_entity

        with pytest.raises(RuntimeError, match="Unknown entity_type"):
            get_entity("demo", "scene", "x")

    def test_raises_when_not_found(self, mock_api):
        from ayon_mcp.tools.entities import get_entity

        mock_api.get_folder_by_id.return_value = None
        with pytest.raises(RuntimeError, match="not found"):
            get_entity("demo", "folder", "missing-id")

    def test_returns_version_model(self, mock_api):
        from ayon_mcp.tools.entities import get_entity, Version

        mock_api.get_version_by_id.return_value = {
            "id": "v1",
            "version": 1,
            "productId": "p1",
            "taskId": None,
            "author": "bob",
            "status": "Approved",
            "tags": [],
            "active": True,
            "createdAt": "2024-01-01T00:00:00Z",
        }
        result = get_entity("demo", "version", "v1")
        assert isinstance(result, Version)
        assert result.version == 1


# ---------------------------------------------------------------------------
# tools/entities – query_graphql
# ---------------------------------------------------------------------------

class TestQueryGraphql:
    def test_returns_data_on_success(self, mock_api):
        from ayon_mcp.tools.entities import query_graphql

        response = MagicMock()
        response.errors = None
        response.data = {"data": {"projects": [{"name": "demo"}]}}
        mock_api.query_graphql.return_value = response

        result = query_graphql("{ projects { name } }")
        assert result == {"projects": [{"name": "demo"}]}

    def test_raises_on_graphql_errors(self, mock_api):
        from ayon_mcp.tools.entities import query_graphql

        response = MagicMock()
        response.errors = [{"message": "Field 'foo' not found"}]
        mock_api.query_graphql.return_value = response

        with pytest.raises(RuntimeError, match="GraphQL query failed"):
            query_graphql("{ foo }")

    def test_passes_variables_to_api(self, mock_api):
        from ayon_mcp.tools.entities import query_graphql

        response = MagicMock()
        response.errors = None
        response.data = {"data": {}}
        mock_api.query_graphql.return_value = response

        query_graphql("query Q($name: String!) { project(name: $name) { code } }", {"name": "demo"})
        mock_api.query_graphql.assert_called_once_with(
            "query Q($name: String!) { project(name: $name) { code } }",
            {"name": "demo"},
        )

    def test_empty_data_key_returns_empty_dict(self, mock_api):
        from ayon_mcp.tools.entities import query_graphql

        response = MagicMock()
        response.errors = None
        response.data = {}
        mock_api.query_graphql.return_value = response

        result = query_graphql("{ version }")
        assert result == {}


# ---------------------------------------------------------------------------
# tools/settings – get_addon_settings / set_addon_settings
# ---------------------------------------------------------------------------

class TestGetAddonSettings:
    def test_returns_settings_dict(self, mock_api):
        from ayon_mcp.tools.settings import get_addon_settings

        mock_api.get_addon_settings.return_value = {"someKey": "someValue"}
        result = get_addon_settings("core", "1.0.0")
        assert result == {"someKey": "someValue"}

    def test_passes_project_name_and_variant(self, mock_api):
        from ayon_mcp.tools.settings import get_addon_settings

        mock_api.get_addon_settings.return_value = {}
        get_addon_settings("maya", "0.9.0", project_name="demo", variant="staging")
        mock_api.get_addon_settings.assert_called_once_with(
            "maya", "0.9.0",
            project_name="demo",
            variant="staging",
            use_site=False,
        )

    def test_studio_level_by_default(self, mock_api):
        from ayon_mcp.tools.settings import get_addon_settings

        mock_api.get_addon_settings.return_value = {}
        get_addon_settings("core", "1.0.0")
        call_kwargs = mock_api.get_addon_settings.call_args.kwargs
        assert call_kwargs["project_name"] is None
        assert call_kwargs["variant"] == "production"


class TestSetAddonSettings:
    def test_returns_saved_response(self, mock_api):
        from ayon_mcp.tools.settings import set_addon_settings

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_api.raw_post.return_value = mock_response

        result = set_addon_settings("core", "1.0.0", {"key": "val"})
        assert result.saved is True
        assert result.addon == "core 1.0.0"
        assert result.level == "studio"
        assert result.variant == "production"

    def test_project_level_endpoint(self, mock_api):
        from ayon_mcp.tools.settings import set_addon_settings

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_api.raw_post.return_value = mock_response

        result = set_addon_settings("maya", "0.9.0", {}, project_name="demo")
        assert result.level == "project:demo"
        endpoint_used = mock_api.raw_post.call_args.args[0]
        assert "demo" in endpoint_used

    def test_staging_variant_in_endpoint(self, mock_api):
        from ayon_mcp.tools.settings import set_addon_settings

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_api.raw_post.return_value = mock_response

        set_addon_settings("core", "1.0.0", {}, variant="staging")
        endpoint_used = mock_api.raw_post.call_args.args[0]
        assert "staging" in endpoint_used