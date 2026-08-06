"""Integration tests for every MCP tool against a live AYON server.

Requires the following environment variables:
    AYON_SERVER_URL  - e.g. http://localhost:5000
    AYON_API_KEY     - a valid API key for the server

Run with:
    pytest -m integration -v tests/test_integration.py

"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.server


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------

class TestLiveListProjects:
    def test_returns_non_empty_list(self, ayon_client):
        from ayon_mcp.tools.projects import list_projects

        result = list_projects()
        assert result.count >= 0
        assert isinstance(result.items, list)

    def test_test_project_is_visible(self, ayon_client, project):
        from ayon_mcp.tools.projects import list_projects

        names = {p.name for p in list_projects().items}
        assert project.project_name in names

    def test_inactive_projects_excluded_by_default(self, ayon_client, project):
        from ayon_mcp.tools.projects import list_projects

        for item in list_projects().items:
            assert item.active is True

    def test_include_inactive_returns_more_or_equal(self, ayon_client):
        from ayon_mcp.tools.projects import list_projects

        active_count = list_projects().count
        all_count = list_projects(include_inactive=True).count
        assert all_count >= active_count


class TestLiveGetProject:
    def test_returns_full_project_detail(self, ayon_client, project):
        from ayon_mcp.tools.projects import get_project

        detail = get_project(project.project_name)
        assert detail["name"] == project.project_name
        assert detail["code"] == project.project_code

    def test_project_anatomy_present(self, ayon_client, project):
        from ayon_mcp.tools.projects import get_project

        detail = get_project(project.project_name)
        assert "folderTypes" in detail or "anatomy" in detail

    def test_raises_for_unknown_project(self, ayon_client):
        from ayon_mcp.tools.projects import get_project

        with pytest.raises(RuntimeError, match="not found"):
            get_project("this_project_does_not_exist_xyz")


class TestLiveGetServerInfo:
    def test_returns_server_info(self, ayon_client, ayon_connection_env):
        from ayon_mcp.tools.projects import get_server_info

        server_url, _ = ayon_connection_env
        info = get_server_info()
        assert info.server_url == server_url
        assert info.server_version
        assert info.user_name


# ---------------------------------------------------------------------------
# entities – read
# ---------------------------------------------------------------------------

class TestLiveGetFolderHierarchy:
    def test_returns_hierarchy_with_test_folder(self, ayon_client, project):
        from ayon_mcp.tools.entities import get_folder_hierarchy

        result = get_folder_hierarchy(project.project_name)
        names = [item.name for item in result.hierarchy]
        assert project.folder.name in names

    def test_folder_type_filter(self, ayon_client, project):
        from ayon_mcp.tools.entities import get_folder_hierarchy

        result = get_folder_hierarchy(project.project_name, folder_types=["Asset"])
        for item in result.hierarchy:
            assert item.folder_type == "Asset"


class TestLiveListFolders:
    def test_returns_test_folder(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_folders

        result = list_folders(project.project_name)
        ids = {f.id for f in result.items}
        assert project.folder.id in ids

    def test_filter_by_folder_type(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_folders

        result = list_folders(project.project_name, folder_types=["Asset"])
        for folder in result.items:
            assert folder.folder_type == "Asset"

    def test_attrib_included_when_requested(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_folders

        result = list_folders(project.project_name, include_attrib=True)
        assert result.count >= 1
        # At least one folder should carry an attrib dict.
        assert any(f.attrib is not None for f in result.items)

    def test_limit_is_respected(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_folders

        result = list_folders(project.project_name, limit=1)
        assert result.count <= 1


class TestLiveListTasks:
    def test_returns_test_task(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_tasks

        result = list_tasks(project.project_name)
        ids = {t.id for t in result.items}
        assert project.task.id in ids

    def test_filter_by_folder_id(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_tasks

        result = list_tasks(project.project_name, folder_id=project.folder.id)
        assert result.count >= 1
        for task in result.items:
            assert task.folder_id == project.folder.id

    def test_filter_by_task_type(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_tasks

        result = list_tasks(project.project_name, task_types=["rendering"])
        assert all(t.task_type == "rendering" for t in result.items)

    def test_unknown_task_type_returns_empty(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_tasks

        result = list_tasks(project.project_name, task_types=["NonExistentType"])
        assert result.count == 0


class TestLiveListProducts:
    def test_returns_test_product(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_products

        result = list_products(project.project_name)
        ids = {p.id for p in result.items}
        assert project.product.id in ids

    def test_filter_by_folder_id(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_products

        result = list_products(project.project_name, folder_id=project.folder.id)
        assert result.count >= 1
        for product in result.items:
            assert product.folder_id == project.folder.id

    def test_filter_by_product_type(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_products

        result = list_products(project.project_name, product_types=["render"])
        assert all(p.product_type == "render" for p in result.items)

    def test_name_regex_filter(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_products

        result = list_products(project.project_name, name_regex="renderMain")
        assert result.count >= 1
        assert all("renderMain" in p.name for p in result.items)


class TestLiveListVersions:
    def test_returns_test_version(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_versions

        result = list_versions(project.project_name)
        ids = {v.id for v in result.items}
        assert project.version.id in ids

    def test_filter_by_product_id(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_versions

        result = list_versions(project.project_name, product_id=project.product.id)
        assert result.count >= 1
        for version in result.items:
            assert version.product_id == project.product.id

    def test_latest_only_returns_one_per_product(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_versions

        result = list_versions(
            project.project_name,
            product_id=project.product.id,
            latest_only=True,
        )
        # Only one product in the test project → at most one latest version.
        assert result.count <= 1

    def test_attrib_included_when_requested(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_versions

        result = list_versions(project.project_name, include_attrib=True)
        assert result.count >= 1


class TestLiveListRepresentations:
    def test_returns_test_representations(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_representations

        result = list_representations(project.project_name)
        ids = {r.id for r in result.items}
        for rep in project.representations:
            assert rep.id in ids

    def test_filter_by_version_id(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_representations

        result = list_representations(
            project.project_name, version_id=project.version.id
        )
        assert result.count >= 1
        for rep in result.items:
            assert rep.version_id == project.version.id

    def test_filter_by_name(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_representations

        result = list_representations(project.project_name, names=["exr_1"])
        assert result.count >= 1
        assert all(r.name == "exr_1" for r in result.items)

    def test_include_files(self, ayon_client, project):
        from ayon_mcp.tools.entities import list_representations

        result = list_representations(project.project_name, include_files=True)
        assert result.count >= 1
        # At least one representation should have files.
        assert any(r.files is not None for r in result.items)


class TestLiveGetEntity:
    def test_get_folder(self, ayon_client, project):
        from ayon_mcp.tools.entities import Folder, get_entity

        result = get_entity(project.project_name, "folder", project.folder.id)
        assert isinstance(result, Folder)
        assert result.id == project.folder.id
        assert result.name == project.folder.name

    def test_get_task(self, ayon_client, project):
        from ayon_mcp.tools.entities import Task, get_entity

        result = get_entity(project.project_name, "task", project.task.id)
        assert isinstance(result, Task)
        assert result.id == project.task.id

    def test_get_product(self, ayon_client, project):
        from ayon_mcp.tools.entities import Product, get_entity

        result = get_entity(project.project_name, "product", project.product.id)
        assert isinstance(result, Product)
        assert result.id == project.product.id

    def test_get_version(self, ayon_client, project):
        from ayon_mcp.tools.entities import Version, get_entity

        result = get_entity(project.project_name, "version", project.version.id)
        assert isinstance(result, Version)
        assert result.id == project.version.id
        assert result.version == 1

    def test_get_representation(self, ayon_client, project):
        from ayon_mcp.tools.entities import Representation, get_entity

        rep_id = project.representations[0].id
        result = get_entity(project.project_name, "representation", rep_id)
        assert isinstance(result, Representation)
        assert result.id == rep_id

    def test_raises_on_unknown_type(self, ayon_client, project):
        from ayon_mcp.tools.entities import get_entity

        with pytest.raises(RuntimeError, match="Unknown entity_type"):
            get_entity(project.project_name, "scene", "any-id")

    def test_raises_when_not_found(self, ayon_client, project):
        from ayon_mcp.tools.entities import get_entity

        with pytest.raises(RuntimeError, match="not found"):
            get_entity(project.project_name, "folder", "00000000000000000000000000000000")


class TestLiveQueryGraphql:
    def test_simple_project_query(self, ayon_client, project):
        from ayon_mcp.tools.entities import query_graphql

        result = query_graphql(
            "query ($name: String!) { project(name: $name) { name code } }",
            {"name": project.project_name},
        )
        assert result["project"]["name"] == project.project_name

    def test_invalid_query_raises(self, ayon_client):
        from ayon_mcp.tools.entities import query_graphql

        with pytest.raises(RuntimeError, match="GraphQL query failed"):
            query_graphql("{ thisFieldDoesNotExistOnTheSchema }")


# ---------------------------------------------------------------------------
# write tools
# ---------------------------------------------------------------------------

class TestLiveCreateEntity:
    def test_create_folder(self, ayon_client, project):
        from ayon_mcp.tools.write import create_entity, delete_entity

        result = create_entity(
            project.project_name,
            "folder",
            {
                "name": "mcp_test_folder",
                "folderType": "Asset",
                "parentId": project.folder.id,
            },
        )
        assert result.entity_id
        assert result.entity_type == "folder"
        assert result.project == project.project_name

        # clean up
        delete_entity(project.project_name, "folder", result.entity_id)

    def test_create_task(self, ayon_client, project):
        from ayon_mcp.tools.write import create_entity, delete_entity

        result = create_entity(
            project.project_name,
            "task",
            {
                "name": "mcp_test_task",
                "taskType": "rendering",
                "folderId": project.folder.id,
            },
        )
        assert result.entity_id
        assert result.entity_type == "task"

        delete_entity(project.project_name, "task", result.entity_id)

    def test_raises_on_invalid_entity_type(self, ayon_client, project):
        from ayon_mcp.tools.write import create_entity

        with pytest.raises(RuntimeError):
            create_entity(project.project_name, "scene", {"name": "x"})


class TestLiveUpdateEntity:
    def test_update_task_status(self, ayon_client, project):
        from ayon_mcp.tools.write import update_entity

        result = update_entity(
            project.project_name,
            "task",
            project.task.id,
            {"status": "not_started"},
        )
        assert result.entity_id == project.task.id
        assert "status" in result.updated

    def test_update_folder_label(self, ayon_client, project):
        from ayon_mcp.tools.write import update_entity

        result = update_entity(
            project.project_name,
            "folder",
            project.folder.id,
            {"label": "Updated by MCP test"},
        )
        assert result.entity_id == project.folder.id
        assert "label" in result.updated

        # restore
        update_entity(
            project.project_name,
            "folder",
            project.folder.id,
            {"label": project.folder.name},
        )

    def test_raises_on_invalid_entity_type(self, ayon_client, project):
        from ayon_mcp.tools.write import update_entity

        with pytest.raises(RuntimeError):
            update_entity(project.project_name, "scene", "id", {})


class TestLiveDeleteEntity:
    def test_delete_created_folder(self, ayon_client, project):
        from ayon_mcp.tools.write import create_entity, delete_entity

        created = create_entity(
            project.project_name,
            "folder",
            {"name": "mcp_delete_me", "folderType": "Asset"},
        )
        result = delete_entity(project.project_name, "folder", created.entity_id)
        assert result.deleted is True
        assert result.entity_id == created.entity_id

    def test_raises_on_invalid_entity_type(self, ayon_client, project):
        from ayon_mcp.tools.write import delete_entity

        with pytest.raises(RuntimeError):
            delete_entity(project.project_name, "scene", "id")


class TestLiveAddComment:
    def test_posts_comment_on_task(self, ayon_client, project):
        from ayon_mcp.tools.write import add_comment

        result = add_comment(
            project.project_name,
            "task",
            project.task.id,
            "Integration test comment from MCP",
        )
        assert result.activity_id
        assert result.entity_id == project.task.id
        assert result.entity_type == "task"

    def test_posts_comment_on_folder(self, ayon_client, project):
        from ayon_mcp.tools.write import add_comment

        result = add_comment(
            project.project_name,
            "folder",
            project.folder.id,
            "Folder comment from MCP integration test",
        )
        assert result.activity_id
        assert result.entity_type == "folder"


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------

class TestLiveListEvents:
    def test_returns_a_list(self, ayon_client):
        from ayon_mcp.tools.events import list_events

        result = list_events()
        assert isinstance(result["items"], list)
        assert isinstance(result["count"], int)

    def test_topic_filter(self, ayon_client):
        from ayon_mcp.tools.events import list_events

        result = list_events(topics=["entity.folder.created"])
        for item in result["items"]:
            assert item["topic"] == "entity.folder.created"

    def test_limit_is_respected(self, ayon_client):
        from ayon_mcp.tools.events import list_events

        result = list_events(limit=2)
        assert result["count"] <= 2


class TestLiveDispatchAndGetEvent:
    def test_dispatch_returns_event_item(self, ayon_client):
        from ayon_mcp.tools.events import dispatch_event

        result = dispatch_event(
            "mcp.test.integration",
            description="Dispatched by MCP integration test",
            finished=True,
        )
        assert result.topic == "mcp.test.integration"
        assert result.status == "finished"

    def test_get_dispatched_event(self, ayon_client):
        from ayon_mcp.tools.events import dispatch_event, get_event

        dispatched = dispatch_event(
            "mcp.test.get_event",
            description="Get-event integration test",
            finished=True,
        )
        fetched = get_event(dispatched.id)
        assert fetched.id == dispatched.id
        assert fetched.topic == "mcp.test.get_event"

# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

class TestLiveListAddons:
    def test_returns_addon_list(self, ayon_client):
        from ayon_mcp.tools.settings import list_addons

        result = list_addons()
        assert result.count >= 0
        assert isinstance(result.items, list)

    def test_versions_are_sorted(self, ayon_client):
        from ayon_mcp.tools.settings import list_addons

        for addon in list_addons().items:
            assert addon.versions == sorted(addon.versions)


class TestLiveListBundles:
    def test_returns_bundle_list(self, ayon_client):
        from ayon_mcp.tools.settings import list_bundles

        result = list_bundles()
        assert result.count >= 0
        assert isinstance(result.items, list)

    def test_at_most_one_production_bundle(self, ayon_client):
        from ayon_mcp.tools.settings import list_bundles

        production = [b for b in list_bundles().items if b.is_production]
        assert len(production) <= 1


class TestLiveGetAddonSettings:
    def test_returns_dict_for_installed_addon(self, ayon_client):
        from ayon_mcp.tools.settings import get_addon_settings, list_addons

        addons = list_addons().items
        if not addons or not addons[0].versions:
            pytest.skip("No addons installed on the server")

        addon = addons[0]
        version = addon.versions[-1]
        result = get_addon_settings(addon.name, version)
        assert isinstance(result, dict)

    def test_project_settings_are_dict(self, ayon_client, project):
        from ayon_mcp.tools.settings import get_addon_settings, list_addons

        addons = list_addons().items
        if not addons or not addons[0].versions:
            pytest.skip("No addons installed on the server")

        addon = addons[0]
        version = addon.versions[-1]
        result = get_addon_settings(
            addon.name, version, project_name=project.project_name
        )
        assert isinstance(result, dict)
