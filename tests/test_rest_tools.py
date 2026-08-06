"""Tests for the REST gateway tools (list / get / call endpoints)."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

FAKE_SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "paths": {
        "/api/projects": {
            "get": {
                "operationId": "list_projects_endpoint",
                "summary": "List Projects",
                "tags": ["Projects"],
            },
            "post": {
                "operationId": "create_project",
                "summary": "Create Project",
                "tags": ["Projects"],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": "#/components/schemas/ProjectModel"
                            }
                        }
                    },
                    "required": True,
                },
            },
        },
        "/api/projects/{project_name}": {
            "parameters": [
                {
                    "name": "project_name",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            ],
            "get": {
                "operationId": "get_project_endpoint",
                "summary": "Get Project",
                "description": "Full project detail",
                "tags": ["Projects"],
            },
            "delete": {
                "operationId": "delete_project_endpoint",
                "summary": "Delete Project",
                "tags": ["Projects"],
                "deprecated": True,
            },
        },
        "/api/system/info": {
            "get": {
                "operationId": "get_site_info",
                "summary": "Get Site Info",
                "tags": ["System"],
            },
        },
    },
    "components": {
        "schemas": {
            "ProjectModel": {
                "type": "object",
                "title": "ProjectModel",
                "properties": {
                    "name": {"type": "string"},
                    "folder": {
                        "$ref": "#/components/schemas/FolderNode",
                        "description": "Root folder",
                    },
                },
            },
            "FolderNode": {
                "type": "object",
                "title": "FolderNode",
                "properties": {
                    "children": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/FolderNode"},
                    },
                },
            },
        }
    },
}


class FakeRestClient:
    """Records request kwargs and returns a canned response."""

    def __init__(self, response: Any = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture()
def fake_spec():
    """Install FAKE_SPEC as the cached OpenAPI spec for the test."""
    from ayon_mcp.openapi_spec import set_spec_cache

    set_spec_cache(FAKE_SPEC)
    yield FAKE_SPEC
    set_spec_cache(None)


@pytest.fixture()
def rest_client():
    """Install a FakeRestClient as the global REST client."""
    from ayon_mcp.rest_client import set_global_rest_client

    client = FakeRestClient()
    set_global_rest_client(client)  # type: ignore[arg-type]
    yield client
    set_global_rest_client(None)


# ---------------------------------------------------------------------------
# list_rest_endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_endpoints_unfiltered_returns_tag_overview(fake_spec):
    from ayon_mcp.tools.rest import list_rest_endpoints

    result = await list_rest_endpoints()

    assert result.count == 5
    assert result.items == []
    tags = {t.name: t.endpoint_count for t in result.tags}
    assert tags == {"Projects": 4, "System": 1}
    assert result.note


@pytest.mark.asyncio
async def test_list_endpoints_filter_by_tag(fake_spec):
    from ayon_mcp.tools.rest import list_rest_endpoints

    result = await list_rest_endpoints(tag="projects")

    assert result.count == 4
    assert all(item.tag == "Projects" for item in result.items)
    deprecated = [item for item in result.items if item.deprecated]
    assert [item.method for item in deprecated] == ["DELETE"]


@pytest.mark.asyncio
async def test_list_endpoints_filter_by_method_and_search(fake_spec):
    from ayon_mcp.tools.rest import list_rest_endpoints

    by_method = await list_rest_endpoints(method="POST")
    assert [item.path for item in by_method.items] == ["/api/projects"]

    by_search = await list_rest_endpoints(search="site info")
    assert [item.path for item in by_search.items] == ["/api/system/info"]

    no_match = await list_rest_endpoints(search="nonexistent-thing")
    assert no_match.count == 0


# ---------------------------------------------------------------------------
# get_rest_endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_endpoint_merges_path_level_parameters(fake_spec):
    from ayon_mcp.tools.rest import get_rest_endpoint

    detail = await get_rest_endpoint("GET", "/api/projects/{project_name}")

    assert detail["operationId"] == "get_project_endpoint"
    parameter_names = [p["name"] for p in detail["parameters"]]
    assert parameter_names == ["project_name"]


@pytest.mark.asyncio
async def test_get_endpoint_resolves_refs_in_request_body(fake_spec):
    from ayon_mcp.tools.rest import get_rest_endpoint

    detail = await get_rest_endpoint("POST", "/api/projects")

    schema = detail["requestBody"]["content"]["application/json"]["schema"]
    assert schema["title"] == "ProjectModel"
    folder = schema["properties"]["folder"]
    # Sibling description next to the $ref is preserved on the result.
    assert folder["description"] == "Root folder"
    assert folder["title"] == "FolderNode"
    # The recursive FolderNode reference stops as a raw $ref.
    children = folder["properties"]["children"]["items"]
    assert children == {"$ref": "#/components/schemas/FolderNode"}


@pytest.mark.asyncio
async def test_get_endpoint_unknown_path_raises(fake_spec):
    from ayon_mcp.tools.rest import get_rest_endpoint

    with pytest.raises(RuntimeError, match="not found in the OpenAPI spec"):
        await get_rest_endpoint("GET", "/api/nope")


# ---------------------------------------------------------------------------
# call_rest_endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_endpoint_fills_path_template(rest_client):
    from ayon_mcp.tools.rest import call_rest_endpoint

    rest_client.response = {"name": "demo"}
    result = await call_rest_endpoint(
        "get",
        "/api/projects/{project_name}",
        path_params={"project_name": "My Project"},
        query_params={"foo": "bar", "skip": None},
    )

    assert result == {"name": "demo"}
    call = rest_client.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/projects/My%20Project"
    assert call["params"] == {"foo": "bar"}


@pytest.mark.asyncio
async def test_call_endpoint_sends_json_body(rest_client):
    from ayon_mcp.tools.rest import call_rest_endpoint

    await call_rest_endpoint(
        "POST", "/api/projects", body={"name": "new_project"}
    )

    call = rest_client.calls[0]
    assert call["method"] == "POST"
    assert call["json"] == {"name": "new_project"}


@pytest.mark.asyncio
async def test_call_endpoint_missing_path_param_raises(rest_client):
    from ayon_mcp.tools.rest import call_rest_endpoint

    with pytest.raises(RuntimeError, match="path_params"):
        await call_rest_endpoint("GET", "/api/projects/{project_name}")
    assert rest_client.calls == []


@pytest.mark.asyncio
async def test_call_endpoint_rejects_unsupported_method(rest_client):
    from ayon_mcp.tools.rest import call_rest_endpoint

    with pytest.raises(RuntimeError, match="Unsupported HTTP method"):
        await call_rest_endpoint("TRACE", "/api/projects")


@pytest.mark.asyncio
async def test_call_endpoint_wraps_http_errors(rest_client):
    from ayon_mcp.tools.rest import call_rest_endpoint

    request = httpx.Request("GET", "http://server/api/projects/missing")
    response = httpx.Response(404, request=request, text='{"detail": "no"}')
    rest_client.error = httpx.HTTPStatusError(
        "not found", request=request, response=response
    )

    with pytest.raises(RuntimeError, match="status 404") as exc_info:
        await call_rest_endpoint(
            "GET",
            "/api/projects/{project_name}",
            path_params={"project_name": "missing"},
        )
    assert '{"detail": "no"}' in str(exc_info.value)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_rest_tools_are_registered():
    from ayon_mcp.tools import ALL_TOOLS

    names = {fn.__name__ for fn in ALL_TOOLS}
    assert {
        "list_rest_endpoints",
        "get_rest_endpoint",
        "call_rest_endpoint",
    } <= names
