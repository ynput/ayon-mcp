"""Tests for the knowledge-layer tools (phase 2)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# get_project_anatomy
# ---------------------------------------------------------------------------

PROJECT_PAYLOAD = {
    "name": "demo",
    "folderTypes": [{"name": "Folder"}, {"name": "Shot"}, {"name": "Asset"}],
    "taskTypes": [{"name": "Modeling"}, {"name": "Compositing"}],
    "statuses": [
        {"name": "Not ready", "state": "not_started", "scope": ["task"]},
        {"name": "In progress", "state": "in_progress"},
    ],
    "tags": [{"name": "hero", "color": "#ff0000"}],
    "linkTypes": [
        {
            "name": "breakdown|folder|folder",
            "linkType": "breakdown",
            "inputType": "folder",
            "outputType": "folder",
        },
    ],
}


class TestGetProjectAnatomy:
    def test_returns_compact_vocabulary(self, mock_api):
        from ayon_mcp.tools.projects import get_project_anatomy

        mock_api.get_project.return_value = PROJECT_PAYLOAD
        result = get_project_anatomy("demo")
        assert result.folder_types == ["Folder", "Shot", "Asset"]
        assert result.task_types == ["Modeling", "Compositing"]
        assert result.statuses[0].name == "Not ready"
        assert result.statuses[0].state == "not_started"
        assert result.statuses[0].scope == ["task"]
        assert result.statuses[1].scope is None
        assert result.tags == ["hero"]
        assert result.link_types[0].link_type == "breakdown"

    def test_missing_project_raises(self, mock_api):
        from ayon_mcp.tools.projects import get_project_anatomy

        mock_api.get_project.return_value = None
        with pytest.raises(RuntimeError, match="not found"):
            get_project_anatomy("nope")

    def test_empty_anatomy_lists(self, mock_api):
        from ayon_mcp.tools.projects import get_project_anatomy

        mock_api.get_project.return_value = {"name": "bare"}
        result = get_project_anatomy("bare")
        assert result.folder_types == []
        assert result.statuses == []


# ---------------------------------------------------------------------------
# list_attributes
# ---------------------------------------------------------------------------

ATTRIBUTES_PAYLOAD = {
    "attributes": [
        {
            "name": "fps",
            "scope": ["project", "folder", "task", "version"],
            "builtin": True,
            "data": {"type": "float", "title": "FPS", "inherit": True},
        },
        {
            "name": "resolutionWidth",
            "scope": ["project", "folder"],
            "builtin": True,
            "data": {
                "type": "integer",
                "title": "Width",
                "description": "Horizontal resolution",
            },
        },
        {
            "name": "priority",
            "scope": ["task"],
            "builtin": False,
            "data": {
                "type": "string",
                "title": "Priority",
                "enum": [
                    {"value": "low", "label": "Low"},
                    {"value": "high", "label": "High"},
                ],
            },
        },
    ],
}


class TestListAttributes:
    def test_flattens_attribute_data(self, mock_api):
        from ayon_mcp.tools.schema import list_attributes

        mock_api.get_attributes_schema.return_value = ATTRIBUTES_PAYLOAD
        result = list_attributes()
        assert result.count == 3
        fps = result.items[0]
        assert fps.name == "fps"
        assert fps.type == "float"
        assert fps.builtin is True
        assert fps.inherit is True
        priority = result.items[2]
        assert priority.enum[0]["value"] == "low"
        assert priority.inherit is None

    def test_scope_filter(self, mock_api):
        from ayon_mcp.tools.schema import list_attributes

        mock_api.get_attributes_schema.return_value = ATTRIBUTES_PAYLOAD
        result = list_attributes(scope="task")
        assert {a.name for a in result.items} == {"fps", "priority"}


# ---------------------------------------------------------------------------
# get_addon_settings_schema
# ---------------------------------------------------------------------------

SETTINGS_SCHEMA = {
    "title": "Core settings",
    "type": "object",
    "properties": {
        "studio_name": {"type": "string", "title": "Studio name"},
        "publish": {"$ref": "#/definitions/Publish"},
    },
    "definitions": {
        "Publish": {
            "title": "Publish plugins",
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "default": True},
                "profiles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"host": {"type": "string"}},
                    },
                },
            },
        },
    },
}


class TestGetAddonSettingsSchema:
    def test_summary_lists_top_level_properties(self, mock_api):
        from ayon_mcp.tools.schema import get_addon_settings_schema

        mock_api.get_addon_settings_schema.return_value = SETTINGS_SCHEMA
        result = get_addon_settings_schema("core", "1.0.0")
        assert result["title"] == "Core settings"
        assert "studio_name" in result["properties"]
        publish = result["properties"]["publish"]
        assert publish["properties"] == ["enabled", "profiles"]
        assert "drill down" in publish["note"]

    def test_path_drills_into_resolved_refs(self, mock_api):
        from ayon_mcp.tools.schema import get_addon_settings_schema

        mock_api.get_addon_settings_schema.return_value = SETTINGS_SCHEMA
        result = get_addon_settings_schema("core", "1.0.0", path=["publish"])
        assert result["title"] == "Publish plugins"
        assert result["properties"]["enabled"]["type"] == "boolean"
        assert result["properties"]["enabled"]["default"] is True
        assert result["properties"]["profiles"]["items"]["type"] == "object"

    def test_full_returns_raw_schema_node(self, mock_api):
        from ayon_mcp.tools.schema import get_addon_settings_schema

        mock_api.get_addon_settings_schema.return_value = SETTINGS_SCHEMA
        result = get_addon_settings_schema(
            "core", "1.0.0", path=["publish"], full=True
        )
        assert result["properties"]["profiles"]["items"]["properties"][
            "host"
        ] == {"type": "string"}

    def test_bad_path_lists_available_keys(self, mock_api):
        from ayon_mcp.tools.schema import get_addon_settings_schema

        mock_api.get_addon_settings_schema.return_value = SETTINGS_SCHEMA
        with pytest.raises(RuntimeError, match="publish, studio_name"):
            get_addon_settings_schema("core", "1.0.0", path=["nope"])

    def test_project_name_is_forwarded(self, mock_api):
        from ayon_mcp.tools.schema import get_addon_settings_schema

        mock_api.get_addon_settings_schema.return_value = SETTINGS_SCHEMA
        get_addon_settings_schema("core", "1.0.0", project_name="demo")
        mock_api.get_addon_settings_schema.assert_called_once_with(
            "core", "1.0.0", "demo"
        )


# ---------------------------------------------------------------------------
# get_graphql_schema
# ---------------------------------------------------------------------------

INTROSPECTION = {
    "queryType": {"name": "Query"},
    "types": [
        {
            "kind": "OBJECT",
            "name": "Query",
            "fields": [
                {
                    "name": "project",
                    "args": [
                        {
                            "name": "name",
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {
                                    "kind": "SCALAR", "name": "String",
                                },
                            },
                        },
                    ],
                    "type": {
                        "kind": "NON_NULL",
                        "name": None,
                        "ofType": {"kind": "OBJECT", "name": "ProjectNode"},
                    },
                },
            ],
        },
        {
            "kind": "OBJECT",
            "name": "ProjectNode",
            "fields": [
                {
                    "name": "folders",
                    "args": [],
                    "type": {
                        "kind": "LIST",
                        "name": None,
                        "ofType": {"kind": "OBJECT", "name": "FolderNode"},
                    },
                },
            ],
        },
        {
            "kind": "ENUM",
            "name": "SortOrder",
            "fields": None,
            "enumValues": [{"name": "ASC"}, {"name": "DESC"}],
        },
        {
            "kind": "OBJECT",
            "name": "__Schema",
            "fields": [],
        },
    ],
}


@pytest.fixture()
def graphql_schema(mock_api):
    """Wire a fake introspection response and clear the schema cache."""
    from ayon_mcp.tools.schema import set_graphql_schema_cache

    set_graphql_schema_cache(None)
    mock_api.query_graphql.return_value = SimpleNamespace(
        errors=None,
        data={"data": {"__schema": INTROSPECTION}},
    )
    yield mock_api
    set_graphql_schema_cache(None)


class TestGetGraphqlSchema:
    def test_overview_renders_query_root(self, graphql_schema):
        from ayon_mcp.tools.schema import get_graphql_schema

        result = get_graphql_schema()
        assert "type Query {" in result
        assert "project(name: String!): ProjectNode!" in result
        assert "SortOrder" in result
        assert "__Schema" not in result

    def test_type_name_renders_sdl(self, graphql_schema):
        from ayon_mcp.tools.schema import get_graphql_schema

        result = get_graphql_schema(type_name="projectnode")
        assert result.startswith("type ProjectNode {")
        assert "folders: [FolderNode]" in result

    def test_enum_rendering(self, graphql_schema):
        from ayon_mcp.tools.schema import get_graphql_schema

        assert get_graphql_schema(
            type_name="SortOrder"
        ) == "enum SortOrder { ASC, DESC }"

    def test_unknown_type_suggests_matches(self, graphql_schema):
        from ayon_mcp.tools.schema import get_graphql_schema

        with pytest.raises(RuntimeError, match="ProjectNode"):
            get_graphql_schema(type_name="project")

    def test_search_finds_fields(self, graphql_schema):
        from ayon_mcp.tools.schema import get_graphql_schema

        result = get_graphql_schema(search="folder")
        assert "ProjectNode.folders: [FolderNode]" in result

    def test_search_without_matches(self, graphql_schema):
        from ayon_mcp.tools.schema import get_graphql_schema

        assert "No types or fields match" in get_graphql_schema(
            search="zzz"
        )

    def test_introspection_error_raises(self, mock_api):
        from ayon_mcp.tools.schema import (
            get_graphql_schema,
            set_graphql_schema_cache,
        )

        set_graphql_schema_cache(None)
        mock_api.query_graphql.return_value = SimpleNamespace(
            errors=[{"message": "nope"}], data={},
        )
        with pytest.raises(RuntimeError, match="introspection failed"):
            get_graphql_schema()

    def test_introspection_is_cached(self, graphql_schema):
        from ayon_mcp.tools.schema import get_graphql_schema

        get_graphql_schema()
        get_graphql_schema()
        assert graphql_schema.query_graphql.call_count == 1


# ---------------------------------------------------------------------------
# get_documentation
# ---------------------------------------------------------------------------

class TestGetDocumentation:
    def test_lists_topics_without_argument(self):
        from ayon_mcp.tools.docs import TOPICS, get_documentation

        result = get_documentation()
        for topic in TOPICS:
            assert topic in result

    def test_returns_topic_content(self):
        from ayon_mcp.tools.docs import get_documentation

        result = get_documentation("writing")
        assert "get_project_anatomy" in result

    def test_topic_is_case_insensitive(self):
        from ayon_mcp.tools.docs import get_documentation

        assert get_documentation("Writing") == get_documentation("writing")

    def test_unknown_topic_lists_valid_ones(self):
        from ayon_mcp.tools.docs import get_documentation

        with pytest.raises(RuntimeError, match="concepts"):
            get_documentation("nope")

    def test_every_topic_has_content(self):
        from ayon_mcp.tools.docs import TOPICS, get_documentation

        for topic in TOPICS:
            assert len(get_documentation(topic)) > 100

    @pytest.mark.asyncio()
    async def test_docs_exposed_as_mcp_resources(self):
        from fastmcp import Client

        from ayon_mcp.server import create_mcp_server

        mcp = create_mcp_server("http://localhost:5000")
        templates = await mcp.list_resource_templates()
        assert "ayon://docs/{topic}" in [t.uri_template for t in templates]
        async with Client(mcp) as client:
            result = await client.read_resource("ayon://docs/concepts")
            assert "Folder" in result[0].text
