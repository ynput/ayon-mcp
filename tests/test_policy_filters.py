"""Tests for the tool and REST tool filters of the per-caller MCP policy."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from chuk_tool_processor.guards import SideEffectClass
from click.testing import CliRunner

from ayon_mcp import cli
from ayon_mcp.client import set_global_ayon_client
from ayon_mcp.guardrails import GuardsMode
from ayon_mcp.policy import (
    EffectivePolicy,
    FilterMode,
    PolicyEffect,
    PolicySettings,
    PolicyStore,
    ToolFilter,
    merge,
    resolve,
)
from ayon_mcp.rest_policy import HTTP_METHOD_ATTR, HTTP_PATH_ATTR
from ayon_mcp.tool_discovery import (
    AyonDynamicToolProvider,
    create_curated_tools,
)
from ayon_mcp.tools import get_server_info

CEILING = EffectivePolicy(
    guards=GuardsMode.DEFAULT, read_only=False, admin_tools=True
)
CANDIDATES = ("get_folders", "ayon.entities", "ayon.entities.get_folders")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "AYON_MCP_GUARDS",
        "AYON_MCP_READ_ONLY",
        "AYON_MCP_ADMIN_TOOLS",
    ):
        monkeypatch.delenv(var, raising=False)


def _allow(*patterns: str) -> ToolFilter:
    return ToolFilter(FilterMode.ALLOWLIST, patterns)


def _deny(*patterns: str) -> ToolFilter:
    return ToolFilter(FilterMode.DENYLIST, patterns)


# --- Patterns and filters -------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "matched"),
    [
        ("get_folders", True),
        ("get_*", True),
        ("Get_*", False),  # case-sensitive
        ("ayon.entities", True),  # namespace
        ("ayon.entities.*", True),  # qualified name
        ("ayon.events.*", False),
        ("re:get_.*", True),
        ("re:^(get|list)_folders$", True),
        ("re:folders", False),  # whole value, not search
        ("re:.*folders", True),
    ],
)
def test_pattern_matching(pattern: str, matched: bool) -> None:
    assert ToolFilter(patterns=(pattern,)).matches(CANDIDATES) is matched


def test_regex_does_not_match_inside_a_longer_name() -> None:
    assert not ToolFilter(patterns=("re:get_.*",)).matches(["forget_x"])


def test_all_mode_allows_everything() -> None:
    assert ToolFilter(FilterMode.ALL, ("nothing",)).allows(CANDIDATES)


def test_allowlist_and_denylist() -> None:
    assert _allow("get_*").allows(CANDIDATES)
    assert not _allow("list_*").allows(CANDIDATES)
    assert not _deny("get_*").allows(CANDIDATES)
    assert _deny("list_*").allows(CANDIDATES)


def test_empty_allowlist_allows_nothing() -> None:
    assert not _allow().allows(CANDIDATES)


def test_invalid_regex_matches_no_tool() -> None:
    assert not _allow("re:(", "get_*").allows(CANDIDATES)
    assert _deny("re:(", "get_*").allows(CANDIDATES)


# --- Parsing --------------------------------------------------------------


def test_filters_are_parsed() -> None:
    settings = PolicySettings.parse({
        "default_policy": {
            "tools": {"mode": "denylist", "patterns": ["set_*"]},
            "openapi": {
                "rest_tools": "off",
                "max_side_effect": "read_only",
                "mode": "allowlist",
                "patterns": ["GET /api/projects*"],
            },
        }
    })

    effect = settings.default
    assert effect.tools == _deny("set_*")
    assert effect.openapi == _allow("GET /api/projects*")
    assert effect.openapi_enabled is False
    assert effect.openapi_max_side_effect is SideEffectClass.READ_ONLY


@pytest.mark.parametrize(
    "tools",
    [None, "garbage", {"mode": "paranoid", "patterns": ["x"]}, {"patterns": 1}],
)
def test_malformed_filters_hide_nothing(tools: object) -> None:
    settings = PolicySettings.parse({"default_policy": {"tools": tools}})

    assert settings.default.tools.allows(CANDIDATES)


def test_inherit_openapi_settings() -> None:
    settings = PolicySettings.parse({
        "default_policy": {
            "openapi": {"rest_tools": "inherit", "max_side_effect": "inherit"}
        }
    })

    assert settings.default == PolicyEffect()


# --- Merging --------------------------------------------------------------


def test_filters_of_every_effect_add_up() -> None:
    policy = merge(
        CEILING,
        [
            PolicyEffect(tools=_allow("get_*", "list_*")),
            PolicyEffect(tools=_deny("list_*")),
            PolicyEffect(tools=ToolFilter()),
        ],
    )

    assert policy.tool_filters == (_allow("get_*", "list_*"), _deny("list_*"))
    assert policy.allows_tool(
        "get_folders", "ayon.entities", SideEffectClass.READ_ONLY
    )
    assert not policy.allows_tool(
        "list_folders", "ayon.entities", SideEffectClass.READ_ONLY
    )
    assert not policy.allows_tool(
        "create_entity", "ayon.write", SideEffectClass.WRITE
    )


def test_strictest_openapi_settings_win() -> None:
    policy = merge(
        CEILING,
        [
            PolicyEffect(
                openapi_enabled=True,
                openapi_max_side_effect=SideEffectClass.WRITE,
            ),
            PolicyEffect(openapi_max_side_effect=SideEffectClass.READ_ONLY),
            PolicyEffect(openapi_max_side_effect=SideEffectClass.DESTRUCTIVE),
        ],
    )

    assert policy.openapi_enabled is True
    assert policy.openapi_max_side_effect is SideEffectClass.READ_ONLY

    disabled = merge(CEILING, [PolicyEffect(openapi_enabled=False)])
    assert disabled.openapi_enabled is False


def test_default_policy_filters_only_without_matching_rules() -> None:
    settings = PolicySettings.parse({
        "default_policy": {"tools": {"mode": "allowlist", "patterns": []}},
        "rules": [{"name": "joe", "users": ["joe"]}],
    })

    from ayon_mcp.policy import Identity

    joe = resolve(Identity("joe", "user"), settings, CEILING)
    ann = resolve(Identity("ann", "user"), settings, CEILING)

    assert joe.allows_tool("get_folders", "ayon.entities", SideEffectClass.READ_ONLY)
    assert not ann.allows_tool(
        "get_folders", "ayon.entities", SideEffectClass.READ_ONLY
    )


# --- Visibility -----------------------------------------------------------


REST_GET = ("GET", "/api/projects/{project_name}")
REST_POST = ("POST", "/api/projects/{project_name}/folders")


def _rest(policy: EffectivePolicy, name: str, endpoint: tuple[str, str]) -> bool:
    side_effect = (
        SideEffectClass.READ_ONLY if endpoint[0] == "GET"
        else SideEffectClass.WRITE
    )
    return policy.allows_tool(
        name, "ayon.openapi.projects", side_effect, endpoint=endpoint
    )


def test_rest_tools_use_the_openapi_filters() -> None:
    policy = EffectivePolicy(
        GuardsMode.DEFAULT,
        read_only=False,
        admin_tools=True,
        tool_filters=(_allow("nothing"),),
        openapi_filters=(_allow("GET /api/projects/*"),),
    )

    assert _rest(policy, "get_project", REST_GET)
    assert not _rest(policy, "create_folder", REST_POST)
    # The curated filter applies to curated tools only.
    assert not policy.allows_tool(
        "get_project", "ayon.projects", SideEffectClass.READ_ONLY
    )


def test_rest_endpoint_patterns_match_any_method() -> None:
    policy = EffectivePolicy(
        GuardsMode.DEFAULT,
        read_only=False,
        admin_tools=True,
        openapi_filters=(_deny("* /api/projects/*/folders"),),
    )

    assert _rest(policy, "get_project", REST_GET)
    assert not _rest(policy, "create_folder", REST_POST)


def test_max_side_effect_limits_rest_tools_only() -> None:
    policy = EffectivePolicy(
        GuardsMode.DEFAULT,
        read_only=False,
        admin_tools=True,
        openapi_max_side_effect=SideEffectClass.READ_ONLY,
    )

    assert _rest(policy, "get_project", REST_GET)
    assert not _rest(policy, "create_folder", REST_POST)
    assert policy.allows_tool("create_entity", "ayon.write", SideEffectClass.WRITE)


def test_openapi_disabled_hides_rest_tools_only() -> None:
    policy = EffectivePolicy(
        GuardsMode.DEFAULT,
        read_only=False,
        admin_tools=True,
        openapi_enabled=False,
    )

    assert not _rest(policy, "get_project", REST_GET)
    assert policy.allows_tool(
        "get_project", "ayon.projects", SideEffectClass.READ_ONLY
    )


# --- Provider -------------------------------------------------------------


def get_project(project_name: str) -> dict[str, str]:
    """Get a project by name."""
    return {"name": project_name}


def list_projects() -> list[str]:
    """List projects."""
    return []


def rest_get_project(project_name: str) -> dict[str, str]:
    """GET a project through REST."""
    return {"name": project_name}


setattr(rest_get_project, HTTP_METHOD_ATTR, REST_GET[0])
setattr(rest_get_project, HTTP_PATH_ATTR, REST_GET[1])


class FixedPolicyStore:
    def __init__(self, policy: EffectivePolicy) -> None:
        self.current = policy

    async def policy(self) -> EffectivePolicy:
        return self.current


def _provider(policy: EffectivePolicy) -> AyonDynamicToolProvider:
    return AyonDynamicToolProvider(
        create_curated_tools([get_project, list_projects, rest_get_project]),
        policy_store=FixedPolicyStore(policy),  # ty: ignore[invalid-argument-type]
    )


def test_rest_tools_carry_their_endpoint() -> None:
    tools = {
        tool.name: tool
        for tool in create_curated_tools([get_project, rest_get_project])
    }

    assert tools["rest_get_project"].endpoint == REST_GET
    assert tools["get_project"].endpoint is None


@pytest.mark.asyncio
async def test_provider_applies_filters() -> None:
    provider = _provider(
        EffectivePolicy(
            GuardsMode.DEFAULT,
            read_only=False,
            admin_tools=True,
            tool_filters=(_deny("list_*"),),
            openapi_filters=(_allow("re:POST .*"),),
        )
    )

    names = {tool["name"] for tool in await provider.list_tools(limit=10)}
    result = await provider.call_tool("list_projects", {})

    assert names == {"get_project"}
    assert result == {
        "success": False, "error": "Tool 'list_projects' was not found."
    }


class FakeServerApi:
    base_url = "http://ayon.test"

    @staticmethod
    def get_user() -> dict[str, Any]:
        return {"name": "joe", "data": {}}

    @staticmethod
    def get_server_version() -> str:
        return "1.17.4"


@pytest.mark.asyncio
async def test_server_info_reports_the_caller_policy() -> None:
    policy = EffectivePolicy(
        GuardsMode.STRICT,
        read_only=True,
        admin_tools=False,
        matched_rules=("artists",),
        tool_filters=(_deny("set_*"),),
    )
    provider = AyonDynamicToolProvider(
        create_curated_tools([get_server_info]),
        policy_store=FixedPolicyStore(policy),  # ty: ignore[invalid-argument-type]
    )
    set_global_ayon_client(FakeServerApi())  # ty: ignore[invalid-argument-type]

    result = await provider.call_tool("get_server_info", {})

    info = result["result"].model_dump()
    assert info["mcp_policy"] == {
        "source": "MCP policy rules 'artists'",
        "matched_rules": ["artists"],
        "guards": "strict",
        "read_only": True,
        "admin_tools": False,
        "tool_filters": [{"mode": "denylist", "patterns": ["set_*"]}],
        "openapi_enabled": True,
        "openapi_max_side_effect": "destructive",
        "openapi_filters": [],
    }


def test_server_info_outside_the_provider_has_no_policy() -> None:
    set_global_ayon_client(FakeServerApi())  # ty: ignore[invalid-argument-type]

    assert get_server_info().mcp_policy is None


# --- Explaining -----------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_of_reads_any_user_with_the_service_key() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"name": "joe", "data": {"defaultAccessGroups": ["artist"]}},
        )

    store = PolicyStore(
        "http://ayon.test", "service-key", transport=httpx.MockTransport(handle)
    )

    identity = await store.identity_of("joe")

    assert identity is not None
    assert identity.access_groups == {"artist"}
    assert requests[0].url.path == "/api/users/joe"
    assert requests[0].headers["x-api-key"] == "service-key"


@pytest.mark.asyncio
async def test_identity_of_unknown_user_is_none() -> None:
    store = PolicyStore(
        "http://ayon.test",
        "service-key",
        transport=httpx.MockTransport(lambda _: httpx.Response(404)),
    )

    assert await store.identity_of("nobody") is None


def test_cli_explains_a_user_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def explain(base_url: str, api_key: str, user: str) -> dict:
        calls.append((base_url, api_key, user))
        return {"user": user, "visible_tools": 3}

    monkeypatch.setattr(cli, "explain_policy", explain)

    result = CliRunner().invoke(
        cli.main,
        ["--host", "https://ayon.test", "--api-key", "key", "--explain-policy", "joe"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"user": "joe", "visible_tools": 3}
    assert calls == [("https://ayon.test", "key", "joe")]


@pytest.mark.asyncio
async def test_explain_policy_covers_addon_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ayon_mcp import server
    from ayon_mcp.addon_discovery import AddonTool, AddonToolEndpoint
    from ayon_mcp.policy import Identity

    def addon_tool(name: str, side_effect: SideEffectClass) -> AddonTool:
        return AddonTool(
            name=name,
            description=f"{name} from the reports addon.",
            parameters={"type": "object", "properties": {}},
            endpoint=AddonToolEndpoint("GET", f"/{name}"),
            addon_name="reports",
            addon_version="1.0.0",
            namespace="reports",
            side_effect=side_effect,
        )

    class FakeCatalog:
        closed = False

        def __init__(self, *_: object, **__: object) -> None:
            self.tools: list[AddonTool] = []

        async def refresh(self) -> None:
            self.tools = [
                addon_tool("summary", SideEffectClass.READ_ONLY),
                addon_tool("rebuild", SideEffectClass.WRITE),
            ]

        async def close(self) -> None:
            FakeCatalog.closed = True

    class FakeStore:
        def __init__(self, *_: object) -> None:
            pass

        async def settings(self) -> PolicySettings:
            return PolicySettings.parse({
                "rules": [{
                    "name": "artists",
                    "access_groups": ["artist"],
                    "tools": {
                        "mode": "denylist",
                        "patterns": ["reports_rebuild"],
                    },
                }]
            })

        async def identity_of(self, user_name: str) -> Identity:
            return Identity(user_name, "user", frozenset({"artist"}))

    monkeypatch.setattr(server, "AddonCatalog", FakeCatalog)
    monkeypatch.setattr(server, "PolicyStore", FakeStore)
    monkeypatch.setenv("AYON_MCP_ENABLE_ADDON_TOOLS", "true")

    explanation = await server.explain_policy("http://ayon.test", "key", "joe")

    assert explanation["tools"]["ayon.addon.reports"] == ["reports_summary"]
    assert "list_projects" in explanation["tools"]["ayon.projects"]
    assert explanation["policy"]["matched_rules"] == ["artists"]
    assert FakeCatalog.closed


@pytest.mark.asyncio
async def test_explain_policy_skips_disabled_addon_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ayon_mcp import server

    def no_catalog(*_: object, **__: object) -> None:
        raise AssertionError("addon tools are disabled")

    monkeypatch.setattr(server, "AddonCatalog", no_catalog)
    monkeypatch.setenv("AYON_MCP_ENABLE_ADDON_TOOLS", "false")

    assert await server._load_addon_tools("http://ayon.test", "key", set()) == []
