"""Tests for REST tools built from the OpenAPI spec."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.server.http import _current_http_request
from fastmcp.server.middleware import Middleware
from starlette.requests import Request

from ayon_mcp import openapi_tools, server as server_module
from ayon_mcp.client import (
    set_global_ayon_api_key,
    set_global_ayon_as_user,
)
from ayon_mcp.openapi_tools import (
    SPEC_PATH,
    build_rest_tools,
    fetch_openapi_spec,
    load_rest_tools,
    rest_tool_of,
)
from ayon_mcp.rest_client import RestApiClient, set_global_rest_client
from ayon_mcp.rest_policy import endpoint_of, is_admin, side_effect_for
from ayon_mcp.tool_discovery import (
    AyonDynamicToolProvider,
    create_curated_tools,
)
from chuk_tool_processor.guards import SideEffectClass

BASE_URL = "http://ayon.test"
TRACKED_SPEC = json.loads(SPEC_PATH.read_text(encoding="utf-8"))

MINIMAL_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "AYON", "version": "1.17.3"},
    "paths": {
        "/api/ping": {
            "get": {
                "operationId": "ping",
                "summary": "Ping",
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}


@pytest.fixture(scope="module")
def tracked_tools() -> dict[tuple[str, str], Any]:
    return {
        endpoint_of(function): function
        for function in build_rest_tools(TRACKED_SPEC, BASE_URL)
    }


class FakeAyon:
    """A REST client for the tools with a recording fake AYON behind it."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(200, json={"ok": True})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self.response.status_code,
            headers=self.response.headers,
            content=self.response.content,
        )

    def client(self, base_url: str = BASE_URL) -> RestApiClient:
        client = RestApiClient(base_url)
        client.http_client = httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
            transport=httpx.MockTransport(self.handle),
        )
        return client


@pytest.fixture
def ayon() -> Iterator[FakeAyon]:
    fake = FakeAyon()
    set_global_rest_client(fake.client())
    set_global_ayon_api_key("service-key")
    set_global_ayon_as_user(None)
    yield fake
    set_global_rest_client(None)
    set_global_ayon_as_user(None)


@contextmanager
def caller_headers(headers: dict[str, str]) -> Iterator[None]:
    """Run as an MCP request whose HTTP headers the caller controls."""
    token = _current_http_request.set(
        Request({
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [
                (key.encode(), value.encode())
                for key, value in headers.items()
            ],
        })
    )
    try:
        yield
    finally:
        _current_http_request.reset(token)


def _spec_response(spec: Any) -> Any:
    def get(url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200, json=spec, request=httpx.Request("GET", url)
        )

    return get


def _names(functions: list[Any]) -> set[str]:
    return {function.__name__ for function in functions}


# --- Building -------------------------------------------------------------


def test_every_operation_gets_a_tool_with_its_endpoint(tracked_tools) -> None:
    operations = {
        (method.upper(), path)
        for path, item in TRACKED_SPEC["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete", "head"}
    }
    assert set(tracked_tools) == operations


def test_tool_names_are_the_operation_ids(tracked_tools) -> None:
    operation_ids = {
        operation["operationId"]
        for item in TRACKED_SPEC["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert {f.__name__ for f in tracked_tools.values()} == operation_ids


def test_name_used_by_curated_tool_gets_prefix() -> None:
    names = _names(
        build_rest_tools(MINIMAL_SPEC, BASE_URL, reserved_names={"ping"})
    )
    assert names == {"openapi_ping"}


def test_tools_have_typed_schemas_and_namespaces(tracked_tools) -> None:
    tool = rest_tool_of(
        tracked_tools["POST", "/api/projects/{project_name}/folders"]
    )
    assert tool.parameters["properties"]["project_name"]["type"] == "string"
    assert "body" not in tool.parameters["properties"]
    assert "folderType" in tool.parameters["properties"]
    assert tool.namespace == "ayon.openapi.projects"
    assert (
        "Endpoint: POST /api/projects/{project_name}/folders"
        in tool.description
    )


def test_spec_without_operations_is_rejected() -> None:
    with pytest.raises(ValueError, match="no operations"):
        build_rest_tools({**MINIMAL_SPEC, "paths": {}}, BASE_URL)


# --- Loading --------------------------------------------------------------


def test_live_spec_is_preferred(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "get", _spec_response(MINIMAL_SPEC))

    functions = load_rest_tools(BASE_URL, "key")

    assert _names(functions) == {"ping"}


def test_fetch_sends_api_key_and_ignores_trailing_slash(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def get(url: str, **kwargs: Any) -> httpx.Response:
        seen.update(url=url, **kwargs)
        return httpx.Response(
            200, json=MINIMAL_SPEC, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx, "get", get)

    fetch_openapi_spec(f"{BASE_URL}/", "key")

    assert seen["url"] == f"{BASE_URL}/openapi.json"
    assert seen["headers"] == {"x-api-key": "key"}


@pytest.mark.parametrize(
    "bad_response",
    [
        pytest.param(
            {"openapi": "3.1.0", "paths": {}}, id="invalid-schema"
        ),
        pytest.param({**MINIMAL_SPEC, "paths": {}}, id="no-operations"),
        pytest.param(["not", "an", "object"], id="not-an-object"),
    ],
)
def test_unusable_live_spec_falls_back_to_tracked(
    monkeypatch, caplog, bad_response
) -> None:
    monkeypatch.setattr(httpx, "get", _spec_response(bad_response))

    with caplog.at_level(logging.WARNING, logger="ayon_mcp.openapi_tools"):
        functions = load_rest_tools(BASE_URL, "key")

    assert "ayon_openapi.json" in caplog.text
    assert len(functions) == sum(
        1
        for item in TRACKED_SPEC["paths"].values()
        for key in item
        if key in {"get", "post", "put", "patch", "delete", "head"}
    )


def test_unreachable_server_falls_back_to_tracked(monkeypatch, caplog) -> None:
    def get(url: str, **_kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", get)

    with caplog.at_level(logging.WARNING, logger="ayon_mcp.openapi_tools"):
        assert load_rest_tools(BASE_URL, "key")

    assert "using the tracked" in caplog.text


def test_no_usable_spec_leaves_no_tools(monkeypatch, tmp_path, caplog) -> None:
    monkeypatch.setattr(httpx, "get", _spec_response([]))
    monkeypatch.setattr(openapi_tools, "SPEC_PATH", tmp_path / "missing.json")

    with caplog.at_level(logging.ERROR, logger="ayon_mcp.openapi_tools"):
        assert load_rest_tools(BASE_URL, "key") == []

    assert "REST tools are not available" in caplog.text


def test_loading_does_not_touch_tracked_spec(monkeypatch) -> None:
    before = SPEC_PATH.read_bytes()
    monkeypatch.setattr(httpx, "get", _spec_response(MINIMAL_SPEC))

    load_rest_tools(BASE_URL, "key")

    assert SPEC_PATH.read_bytes() == before


@pytest.mark.parametrize(
    ("version", "warned"),
    [
        ("1.16.11", True),
        ("0.9.0", True),
        ("1.17.0", False),
        ("1.17.0-dev", False),
        ("2.0.0", False),
        ("unknown", False),
    ],
)
def test_older_server_is_reported(
    monkeypatch, caplog, version, warned
) -> None:
    spec = {**MINIMAL_SPEC, "info": {"title": "AYON", "version": version}}
    monkeypatch.setattr(httpx, "get", _spec_response(spec))

    with caplog.at_level(logging.WARNING, logger="ayon_mcp.openapi_tools"):
        assert _names(load_rest_tools(BASE_URL)) == {"ping"}

    assert ("this MCP server needs 1.17" in caplog.text) is warned


# --- Requests and results -------------------------------------------------


@pytest.mark.asyncio
async def test_path_parameters_are_encoded(ayon, tracked_tools) -> None:
    await tracked_tools["GET", "/api/projects/{project_name}"](
        project_name="a/b c"
    )

    assert ayon.requests[-1].url.raw_path == b"/api/projects/a%2Fb%20c"


@pytest.mark.asyncio
async def test_flattened_body_is_sent_as_json(ayon, tracked_tools) -> None:
    create_folder = tracked_tools["POST", "/api/projects/{project_name}/folders"]

    await create_folder(
        project_name="demo", name="shots", folderType="Folder"
    )

    request = ayon.requests[-1]
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "name": "shots",
        "folderType": "Folder",
    }
    assert "project_name" not in json.loads(request.content)


@pytest.mark.asyncio
async def test_requests_get_the_client_timeout(ayon, tracked_tools) -> None:
    await tracked_tools["GET", "/api/projects/{project_name}"](
        project_name="demo"
    )

    assert ayon.requests[-1].extensions["timeout"]["read"] == 30.0


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param(httpx.Response(200, json={"a": 1}), {"a": 1}, id="object"),
        pytest.param(httpx.Response(200, json=[1, 2]), [1, 2], id="list"),
        pytest.param(httpx.Response(200, json=7), 7, id="number"),
        # FastMCP wraps non-object bodies as {"result": value}; objects with
        # only a "result" key must not be unwrapped as if FastMCP made them.
        pytest.param(
            httpx.Response(200, json={"result": [1, 2]}),
            {"result": [1, 2]},
            id="object-with-result-list",
        ),
        pytest.param(
            httpx.Response(200, json={"result": False}),
            {"result": False},
            id="object-with-result-bool",
        ),
        pytest.param(
            httpx.Response(200, json={"result": {"a": 1}}),
            {"result": {"a": 1}},
            id="object-with-result-object",
        ),
        pytest.param(httpx.Response(200, text="a,b\n"), "a,b\n", id="text"),
        pytest.param(httpx.Response(204), None, id="empty"),
    ],
)
@pytest.mark.asyncio
async def test_response_bodies_are_returned_as_they_are(
    ayon, tracked_tools, response, expected
) -> None:
    ayon.response = response

    result = await tracked_tools["GET", "/api/projects/{project_name}"](
        project_name="demo"
    )

    assert result == expected


@pytest.mark.asyncio
async def test_http_error_includes_response_detail(
    ayon, tracked_tools
) -> None:
    ayon.response = httpx.Response(404, json={"detail": "no such project"})

    with pytest.raises(ValueError, match="404.*no such project"):
        await tracked_tools["GET", "/api/projects/{project_name}"](
            project_name="demo"
        )


# --- Headers --------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_caller_headers_do_not_reach_ayon(
    ayon, tracked_tools
) -> None:
    with caller_headers({
        "x-api-key": "spoofed-key",
        "x-as-user": "admin",
        "x-ayon-mcp-user": "admin",
        "x-forwarded-for": "10.0.0.1",
        "x-sender": "spoofed",
        "cookie": "accessToken=secret",
    }):
        await tracked_tools["GET", "/api/projects/{project_name}"](
            project_name="demo"
        )

    headers = ayon.requests[-1].headers
    assert headers["x-api-key"] == "service-key"
    for name in (
        "x-as-user",
        "x-ayon-mcp-user",
        "x-forwarded-for",
        "x-sender",
        "cookie",
    ):
        assert name not in headers


@pytest.mark.asyncio
async def test_tunnel_acts_as_context_user_not_header_user(
    ayon, tracked_tools
) -> None:
    set_global_ayon_as_user("alice")

    with caller_headers({"x-as-user": "admin", "x-ayon-mcp-user": "alice"}):
        await tracked_tools["GET", "/api/projects/{project_name}"](
            project_name="demo"
        )

    headers = ayon.requests[-1].headers
    assert headers["x-api-key"] == "service-key"
    assert headers["x-as-user"] == "alice"
    assert "x-ayon-mcp-user" not in headers


@pytest.mark.asyncio
async def test_declared_header_argument_reaches_ayon(
    ayon, tracked_tools
) -> None:
    with caller_headers({"x-sender-type": "spoofed"}):
        await tracked_tools["POST", "/api/actions/execute"](**{
            "addonName": "core",
            "addonVersion": "1.0.0",
            "identifier": "x",
            "x-sender": "from-the-call",
        })

    headers = ayon.requests[-1].headers
    assert headers["x-sender"] == "from-the-call"
    assert "x-sender-type" not in headers


@pytest.mark.asyncio
async def test_call_headers_do_not_outlive_the_call(
    ayon, tracked_tools
) -> None:
    await tracked_tools["POST", "/api/actions/execute"](**{
        "addonName": "core",
        "addonVersion": "1.0.0",
        "identifier": "x",
        "x-sender": "from-the-call",
    })

    with caller_headers({"x-sender": "spoofed"}):
        await tracked_tools["GET", "/api/projects/{project_name}"](
            project_name="demo"
        )

    assert "x-sender" not in ayon.requests[-1].headers


# --- Policy ---------------------------------------------------------------


@pytest.fixture
def provider(tracked_tools, monkeypatch) -> AyonDynamicToolProvider:
    monkeypatch.delenv("AYON_MCP_READ_ONLY", raising=False)
    return AyonDynamicToolProvider(
        create_curated_tools(list(tracked_tools.values()))
    )


@pytest.mark.asyncio
async def test_side_effects_come_from_rest_policy(
    tracked_tools, provider
) -> None:
    tools = await provider.get_all_tools()

    assert len(tools) == len(tracked_tools)
    for tool in tools:
        assert tool.side_effect is side_effect_for(*endpoint_of(tool.function))


@pytest.mark.asyncio
async def test_delete_needs_confirmation(ayon, provider) -> None:
    result = await provider.call_tool(
        "delete_project", {"project_name": "demo", "confirm_mutation": False}
    )

    assert result["success"] is False
    assert "confirm_mutation" in result["error"]
    assert not ayon.requests


@pytest.mark.asyncio
async def test_confirmed_delete_runs(ayon, provider) -> None:
    result = await provider.call_tool(
        "delete_project", {"project_name": "demo", "confirm_mutation": True}
    )

    assert result == {"success": True, "result": {"ok": True}}
    assert ayon.requests[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_read_only_override_needs_no_confirmation(
    ayon, provider
) -> None:
    assert (
        side_effect_for(
            "POST", "/api/projects/{project_name}/folders/search"
        )
        is SideEffectClass.READ_ONLY
    )

    result = await provider.call_tool(
        "search_folders", {"project_name": "demo", "confirm_mutation": False}
    )

    assert result["success"] is True


@pytest.mark.asyncio
async def test_guards_read_refs_in_body_schemas(ayon, provider) -> None:
    missing = await provider.call_tool(
        "create_folder",
        {"project_name": "demo", "confirm_mutation": True},
    )
    assert missing["success"] is False
    assert not ayon.requests

    valid = await provider.call_tool(
        "create_folder",
        {
            "project_name": "demo",
            "name": "shots",
            "folderType": "Folder",
            "attrib": {"fps": 25},
            "confirm_mutation": True,
        },
    )
    assert valid["success"] is True, valid
    assert json.loads(ayon.requests[-1].content)["attrib"] == {"fps": 25}


@pytest.mark.asyncio
async def test_http_error_becomes_tool_error(ayon, provider) -> None:
    ayon.response = httpx.Response(403, json={"detail": "forbidden"})

    result = await provider.call_tool(
        "get_project", {"project_name": "demo", "confirm_mutation": False}
    )

    assert result["success"] is False
    assert "403" in result["error"]


# --- Server ---------------------------------------------------------------


class _Key(Middleware):
    """Set the caller's key the way the server's auth middlewares do."""

    async def on_call_tool(self, context, call_next):
        set_global_ayon_api_key("service-key")
        set_global_ayon_as_user(None)
        return await call_next(context)


@pytest.fixture
def rest_server_env(monkeypatch) -> FakeAyon:
    """Enable REST tools on a server whose spec is the tracked one."""
    fake = FakeAyon()
    monkeypatch.setenv("AYON_MCP_ENABLE_OPENAPI_TOOLS", "true")
    monkeypatch.setenv("AYON_MCP_ENABLE_ADDON_TOOLS", "false")
    monkeypatch.delenv("AYON_MCP_READ_ONLY", raising=False)
    monkeypatch.delenv("AYON_MCP_ADMIN_TOOLS", raising=False)
    monkeypatch.setattr(httpx, "get", _spec_response(TRACKED_SPEC))
    monkeypatch.setattr(
        server_module,
        "RestApiClient",
        lambda base_url: fake.client(base_url),
    )
    return fake


def _create_server() -> Any:
    mcp = server_module.create_mcp_server(BASE_URL, "key")
    mcp.add_middleware(_Key())
    return mcp


def _text(result: Any) -> str:
    return result.content[0].text


def test_rest_tools_are_off_when_disabled(
    rest_server_env, monkeypatch
) -> None:
    monkeypatch.setenv("AYON_MCP_ENABLE_OPENAPI_TOOLS", "false")
    monkeypatch.setenv("AYON_MCP_TOOL_EXPOSURE", "direct")

    def fail(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("the spec must not be fetched")

    monkeypatch.setattr(httpx, "get", fail)

    names = {t.name for t in asyncio.run(_create_server().list_tools())}

    assert "get_access_group_schema" not in names
    assert "get_project" in names


def test_direct_mode_filters_admin_and_prefixes_collisions(
    rest_server_env, monkeypatch, tracked_tools
) -> None:
    monkeypatch.setenv("AYON_MCP_TOOL_EXPOSURE", "direct")

    tools = asyncio.run(_create_server().list_tools())
    names = {tool.name for tool in tools}

    # Curated and REST tool of the same name are both available.
    assert {"get_project", "openapi_get_project"} <= names
    admin = {
        function.__name__
        for endpoint, function in tracked_tools.items()
        if is_admin(*endpoint)
    }
    assert admin
    assert not names & admin


def test_admin_tools_can_be_enabled(
    rest_server_env, monkeypatch, tracked_tools
) -> None:
    monkeypatch.setenv("AYON_MCP_TOOL_EXPOSURE", "direct")
    monkeypatch.setenv("AYON_MCP_ADMIN_TOOLS", "true")

    names = {t.name for t in asyncio.run(_create_server().list_tools())}

    assert tracked_tools["POST", "/api/system/restart"].__name__ in names


def test_direct_read_only_mode_keeps_only_reads(
    rest_server_env, monkeypatch, tracked_tools
) -> None:
    monkeypatch.setenv("AYON_MCP_TOOL_EXPOSURE", "direct")
    monkeypatch.setenv("AYON_MCP_READ_ONLY", "true")

    names = {t.name for t in asyncio.run(_create_server().list_tools())}

    assert "search_folders" in names
    assert "delete_project" not in names
    assert not names & {
        function.__name__
        for endpoint, function in tracked_tools.items()
        if side_effect_for(*endpoint) is not SideEffectClass.READ_ONLY
    }


@pytest.mark.asyncio
async def test_direct_mode_calls_rest_tools_across_lifespans(
    rest_server_env, monkeypatch
) -> None:
    monkeypatch.setenv("AYON_MCP_TOOL_EXPOSURE", "direct")
    mcp = _create_server()

    for _ in range(2):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "openapi_get_project", {"project_name": "demo"}
            )
            assert json.loads(_text(result)) == {"ok": True}

    requests = rest_server_env.requests
    assert len(requests) == 2
    assert all(r.headers["x-api-key"] == "service-key" for r in requests)


@pytest.mark.asyncio
async def test_discovery_mode_runs_rest_tool_through_policy(
    rest_server_env, monkeypatch
) -> None:
    monkeypatch.setenv("AYON_MCP_TOOL_EXPOSURE", "discovery")

    async with Client(_create_server()) as client:
        schema = await client.call_tool(
            "get_ayon_tool_schema", {"tool_name": "delete_project"}
        )
        assert "confirm_mutation" in _text(schema)

        refused = await client.call_tool(
            "call_ayon_tool",
            {
                "tool_name": "delete_project",
                "arguments": {"project_name": "demo"},
            },
        )
        assert '"success":false' in _text(refused).replace(" ", "")
        assert not rest_server_env.requests

        allowed = await client.call_tool(
            "call_ayon_tool",
            {
                "tool_name": "get_access_group_schema",
                "arguments": {},
            },
        )
        assert json.loads(_text(allowed))["success"] is True

    assert rest_server_env.requests[-1].url.path.endswith("_schema")
