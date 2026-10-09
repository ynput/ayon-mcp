"""Tests for MCP tool discovery from AYON addons."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ayon_mcp.addon_discovery import (
    AddonTool,
    AddonToolEndpoint,
    discover_addon_tools,
    parse_addon_tools,
)
from ayon_mcp.addon_tools import AddonToolProvider
from ayon_mcp.tool_discovery import (
    AyonDynamicToolProvider,
    create_addon_ayon_tools,
    create_curated_tools,
)

REPORTS_SPEC: dict[str, Any] = {
    "namespace": "reports",
    "tools": [
        {
            "name": "list_metrics",
            "description": "List available metrics from Reports catalog.",
            "parameters": {
                "type": "object",
                "properties": {"project_name": {"type": "string"}},
                "required": ["project_name"],
            },
            "endpoint": {
                "method": "GET",
                "path": "/api/addons/reports/{version}/metrics",
            },
        },
        {
            "name": "query_metric",
            "description": "Query a metric. POST but read only.",
            "readOnly": True,
            "parameters": {
                "type": "object",
                "properties": {"project_name": {"type": "string"}},
                "required": ["project_name"],
            },
            "endpoint": {
                "method": "POST",
                "path": "/api/addons/reports/{version}/metrics/query",
            },
        },
        {
            "name": "add_chart",
            "description": "Add a chart to a dashboard.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dashboard_id": {"type": "string"},
                    "preview_token": {"type": "string"},
                },
                "required": ["dashboard_id", "preview_token"],
            },
            "endpoint": {
                "method": "POST",
                "path": (
                    "/api/addons/reports/{version}"
                    "/dashboards/{dashboard_id}/charts"
                ),
            },
        },
    ],
}


class FakeRestClient:
    """Minimal RestApiClient stand-in recording requests."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append(
            {
                "method": method.upper(),
                "path": path,
                "params": params,
                "json": json,
                "headers": headers,
            }
        )
        key = (method.upper(), path)
        if key not in self.routes:
            request = httpx.Request(method, f"http://ayon{path}")
            response = httpx.Response(404, request=request, text="not found")
            raise httpx.HTTPStatusError(
                "404", request=request, response=response
            )
        return self.routes[key]


def get_project(project_name: str) -> dict[str, str]:
    """Get a project by name."""
    return {"name": project_name}


def test_parse_addon_tools_prefixes_namespace_and_resolves_version():
    tools = parse_addon_tools(REPORTS_SPEC, "reports", "1.2.0")

    assert [t.full_name for t in tools] == [
        "reports_list_metrics",
        "reports_query_metric",
        "reports_add_chart",
    ]
    assert tools[0].endpoint.path == "/api/addons/reports/1.2.0/metrics"
    assert tools[0].requires_confirmation is False
    # POST declared readOnly: no confirmation needed
    assert tools[1].requires_confirmation is False
    # POST without readOnly falls back to the HTTP method
    assert tools[2].requires_confirmation is True


def test_parse_addon_tools_falls_back_to_addon_name_namespace():
    spec = {
        "tools": [
            {
                "name": "ping",
                "endpoint": {"path": "/api/addons/planner/{version}/ping"},
            }
        ]
    }
    tools = parse_addon_tools(spec, "planner", "2.0.0")

    assert tools[0].full_name == "planner_ping"
    assert tools[0].endpoint.method == "GET"


def test_parse_addon_tools_rejects_endpoints_outside_addon_prefix():
    spec = {
        "tools": [
            {"name": "evil", "endpoint": {"method": "DELETE", "path": "/api/users/x"}},
            {"name": "sneaky", "endpoint": {"path": "/api/addons/core/1.0.0/x"}},
            {
                "name": "traversal",
                "endpoint": {
                    "path": "/api/addons/planner/{version}/../../users/x"
                },
            },
            {"name": "relative", "endpoint": {"path": "api/addons/planner/2.0.0/x"}},
            {"name": "ok", "endpoint": {"path": "/api/addons/planner/{version}/ok"}},
        ]
    }
    tools = parse_addon_tools(spec, "planner", "2.0.0")

    assert [t.name for t in tools] == ["ok"]


@pytest.mark.asyncio
async def test_path_argument_cannot_escape_addon_prefix():
    addon_tools = parse_addon_tools(REPORTS_SPEC, "reports", "1.2.0")
    client = FakeRestClient({})
    provider = AddonToolProvider(addon_tools, client)

    result = await provider.execute_tool(
        "reports_add_chart",
        {"dashboard_id": "../../../users", "preview_token": "tok"},
    )

    # quote(safe="") keeps the value a single encoded segment, so the
    # request stays under the addon prefix and never climbs out of it.
    assert client.calls, result
    called_path = client.calls[0]["path"]
    assert called_path.startswith("/api/addons/reports/1.2.0/dashboards/")
    assert ".." not in called_path.split("/")
    assert "%2F" in called_path


@pytest.mark.asyncio
async def test_slow_addon_does_not_drop_other_addons():
    import asyncio

    class SlowClient(FakeRestClient):
        async def request(self, method, path, **kwargs):
            if path == "/api/addons/slow/1.0.0/mcp/tools":
                await asyncio.sleep(0.2)
            return await super().request(method, path, **kwargs)

    client = SlowClient(
        {
            ("GET", "/api/addons"): {
                "addons": [
                    {"name": "slow", "productionVersion": "1.0.0"},
                    {"name": "reports", "productionVersion": "1.2.0"},
                ]
            },
            ("GET", "/api/addons/slow/1.0.0/mcp/tools"): {
                "tools": [
                    {
                        "name": "late",
                        "endpoint": {"path": "/api/addons/slow/{version}/x"},
                    }
                ]
            },
            ("GET", "/api/addons/reports/1.2.0/mcp/tools"): REPORTS_SPEC,
        }
    )

    tools = await discover_addon_tools(client, api_key="k", timeout=0.05)

    names = {t.full_name for t in tools}
    assert "reports_list_metrics" in names
    assert "slow_late" not in names


@pytest.mark.asyncio
async def test_discover_skips_addons_without_mcp_endpoint():
    client = FakeRestClient(
        {
            # Real shape of GET /api/addons: AddonList.addons is a list
            ("GET", "/api/addons"): {
                "addons": [
                    {"name": "reports", "productionVersion": "1.2.0"},
                    {"name": "core", "productionVersion": "1.0.0"},
                    {"name": "planner", "versions": {"2.0.0": {}}},
                    {"name": "broken"},
                    {"title": "nameless"},
                ]
            },
            ("GET", "/api/addons/reports/1.2.0/mcp/tools"): REPORTS_SPEC,
            ("GET", "/api/addons/planner/2.0.0/mcp/tools"): {
                "tools": [
                    {
                        "name": "get_workload",
                        "endpoint": {
                            "path": "/api/addons/planner/{version}/workload"
                        },
                    }
                ]
            },
        }
    )

    tools = await discover_addon_tools(client, api_key="key")

    assert {t.full_name for t in tools} == {
        "reports_list_metrics",
        "reports_query_metric",
        "reports_add_chart",
        "planner_get_workload",
    }
    assert client.calls[0]["headers"] == {"x-api-key": "key"}
    requested = {c["path"] for c in client.calls}
    assert "/api/addons/broken/None/mcp/tools" not in requested


@pytest.mark.asyncio
async def test_addon_tools_are_searchable_alongside_curated_tools():
    addon_tools = parse_addon_tools(REPORTS_SPEC, "reports", "1.2.0")
    provider = AyonDynamicToolProvider(
        [
            *create_curated_tools([get_project]),
            *create_addon_ayon_tools(addon_tools),
        ],
        AddonToolProvider(addon_tools, FakeRestClient({})),
    )

    search = await provider.search_tools("metrics")
    schema = await provider.get_tool_schema("reports_list_metrics")

    assert search[0]["name"] == "reports_list_metrics"
    assert schema["function"]["parameters"]["required"] == ["project_name"]


@pytest.mark.asyncio
async def test_get_tool_proxies_arguments_as_query_params():
    addon_tools = parse_addon_tools(REPORTS_SPEC, "reports", "1.2.0")
    client = FakeRestClient(
        {("GET", "/api/addons/reports/1.2.0/metrics"): {"metrics": []}}
    )
    provider = AyonDynamicToolProvider(
        create_addon_ayon_tools(addon_tools),
        AddonToolProvider(addon_tools, client),
    )

    result = await provider.call_tool(
        "reports_list_metrics", {"project_name": "demo"}
    )

    assert result == {"success": True, "result": {"metrics": []}}
    assert client.calls[0]["params"] == {"project_name": "demo"}
    assert client.calls[0]["json"] is None


@pytest.mark.asyncio
async def test_post_tool_requires_confirmation_and_fills_path_params():
    addon_tools = parse_addon_tools(REPORTS_SPEC, "reports", "1.2.0")
    path = "/api/addons/reports/1.2.0/dashboards/dash%201/charts"
    client = FakeRestClient({("POST", path): {"id": "chart-1"}})
    provider = AyonDynamicToolProvider(
        create_addon_ayon_tools(addon_tools),
        AddonToolProvider(addon_tools, client),
    )
    arguments = {"dashboard_id": "dash 1", "preview_token": "tok"}

    blocked = await provider.call_tool("reports_add_chart", arguments)
    executed = await provider.call_tool(
        "reports_add_chart", {**arguments, "confirm_mutation": True}
    )

    assert blocked["success"] is False
    assert "confirm_mutation=true" in blocked["error"]
    assert executed == {"success": True, "result": {"id": "chart-1"}}
    assert client.calls[0]["path"] == path
    assert client.calls[0]["json"] == {"preview_token": "tok"}


@pytest.mark.asyncio
async def test_read_only_post_tool_runs_without_confirmation():
    addon_tools = parse_addon_tools(REPORTS_SPEC, "reports", "1.2.0")
    path = "/api/addons/reports/1.2.0/metrics/query"
    client = FakeRestClient({("POST", path): {"data": [1]}})
    provider = AyonDynamicToolProvider(
        create_addon_ayon_tools(addon_tools),
        AddonToolProvider(addon_tools, client),
    )

    result = await provider.call_tool(
        "reports_query_metric", {"project_name": "demo"}
    )

    assert result == {"success": True, "result": {"data": [1]}}
    assert client.calls[0]["json"] == {"project_name": "demo"}


@pytest.mark.asyncio
async def test_missing_path_argument_fails_before_request():
    addon_tools = parse_addon_tools(REPORTS_SPEC, "reports", "1.2.0")
    client = FakeRestClient({})
    provider = AddonToolProvider(addon_tools, client)

    result = await provider.execute_tool(
        "reports_add_chart", {"preview_token": "tok"}
    )

    assert result["success"] is False
    assert "dashboard_id" in result["error"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_addon_http_errors_become_structured_results():
    tool = AddonTool(
        name="fail",
        description="",
        parameters={},
        endpoint=AddonToolEndpoint(
            method="GET", path="/api/addons/x/1/missing"
        ),
        addon_name="x",
        addon_version="1",
        namespace="x",
    )
    client = FakeRestClient({})
    provider = AddonToolProvider([tool], client)

    result = await provider.execute_tool("x_fail", {})

    assert result["success"] is False
    assert "404" in result["error"]
    assert client.calls[0]["path"] == "/api/addons/x/1/missing"


@pytest.mark.asyncio
async def test_path_outside_addon_prefix_fails_before_request():
    tool = AddonTool(
        name="escape",
        description="",
        parameters={},
        endpoint=AddonToolEndpoint(method="GET", path="/missing"),
        addon_name="x",
        addon_version="1",
        namespace="x",
    )
    client = FakeRestClient({})
    provider = AddonToolProvider([tool], client)

    result = await provider.execute_tool("x_escape", {})

    assert result["success"] is False
    assert "outside the addon prefix" in result["error"]
    assert client.calls == []
