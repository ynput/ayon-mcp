"""Tests for the per-caller MCP policy from the addon settings."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from ayon_mcp.client import (
    set_global_ayon_api_key,
    set_global_ayon_as_user,
    set_global_ayon_client,
)
from ayon_mcp.guardrails import GuardsMode
from ayon_mcp.policy import (
    EffectivePolicy,
    Identity,
    PolicyEffect,
    PolicySettings,
    PolicyStore,
    resolve,
)
from ayon_mcp.rest_policy import HTTP_METHOD_ATTR, HTTP_PATH_ATTR
from ayon_mcp.tool_discovery import (
    AyonDynamicToolProvider,
    create_curated_tools,
)

BASE_URL = "http://ayon.test"

ARTIST = Identity("joe", "user", frozenset({"artist"}))
SUPERVISOR = Identity("ann", "manager", frozenset({"supervisor"}))
SERVICE = Identity("bot", "service")

CEILING = EffectivePolicy(
    guards=GuardsMode.DEFAULT, read_only=False, admin_tools=True
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "AYON_MCP_GUARDS",
        "AYON_MCP_READ_ONLY",
        "AYON_MCP_ADMIN_TOOLS",
        "AYON_MCP_ADDON_VARIANT",
        "AYON_DEFAULT_SETTINGS_VARIANT",
        "AYON_ADDON_NAME",
        "AYON_ADDON_VERSION",
    ):
        monkeypatch.delenv(var, raising=False)


def _rule(name: str, **values: Any) -> dict[str, Any]:
    return {"name": name, "enabled": True, **values}


def _settings(*rules: dict[str, Any], **default: Any) -> PolicySettings:
    return PolicySettings.parse(
        {"default_policy": default, "rules": list(rules)}
    )


# --- Identity -------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "level"),
    [
        ({}, "user"),
        ({"isManager": True}, "manager"),
        ({"isAdmin": True, "isManager": True}, "admin"),
        ({"isService": True, "isAdmin": True}, "service"),
        ({"isGuest": True, "isAdmin": True}, "guest"),
    ],
)
def test_identity_level_is_the_highest_role(
    data: dict[str, Any], level: str
) -> None:
    identity = Identity.from_user({"name": "joe", "data": data})

    assert identity.level == level


def test_identity_uses_default_access_groups_only() -> None:
    identity = Identity.from_user({
        "name": "joe",
        "data": {
            "defaultAccessGroups": ["artist"],
            "accessGroups": {"demo": ["supervisor"]},
        },
    })

    assert identity.access_groups == {"artist"}


# --- Settings parsing -----------------------------------------------------


def test_empty_settings_have_no_opinion() -> None:
    assert PolicySettings.parse(None) == PolicySettings()
    assert PolicySettings.parse({}) == PolicySettings()


def test_unknown_values_inherit() -> None:
    settings = PolicySettings.parse({
        "default_policy": {"guards": "paranoid", "read_only": "maybe"},
        "rules": [_rule("r", users=["joe"], admin_tools=1), "garbage"],
    })

    assert settings.default == PolicyEffect()
    assert len(settings.rules) == 1
    assert settings.rules[0].effect == PolicyEffect()


def test_refresh_interval_has_a_floor() -> None:
    settings = PolicySettings.parse({"policy_refresh_seconds": 0})

    assert settings.refresh_seconds == 5


# --- Resolution -----------------------------------------------------------


def test_empty_settings_leave_the_env_ceiling() -> None:
    assert resolve(ARTIST, PolicySettings(), CEILING) == CEILING


def test_default_policy_applies_when_no_rule_matches() -> None:
    settings = _settings(
        _rule("supervisors", access_groups=["supervisor"], guards="strict"),
        read_only="on",
    )

    policy = resolve(ARTIST, settings, CEILING)

    assert policy.read_only is True
    assert policy.guards is GuardsMode.DEFAULT
    assert policy.matched_rules == ()


def test_default_policy_does_not_apply_when_a_rule_matches() -> None:
    settings = _settings(
        _rule("supervisors", access_groups=["supervisor"]),
        read_only="on",
    )

    policy = resolve(SUPERVISOR, settings, CEILING)

    assert policy.read_only is False
    assert policy.matched_rules == ("supervisors",)


def test_strictest_value_of_every_matching_rule_wins() -> None:
    settings = _settings(
        _rule("by name", users=["joe"], guards="strict", admin_tools="on"),
        _rule("by group", access_groups=["artist"], admin_tools="off"),
        _rule("by level", user_levels=["user"], read_only="on", guards="off"),
    )

    policy = resolve(ARTIST, settings, CEILING)

    assert policy == EffectivePolicy(
        guards=GuardsMode.STRICT,
        read_only=True,
        admin_tools=False,
        matched_rules=("by name", "by group", "by level"),
    )


def test_settings_never_loosen_the_env_ceiling() -> None:
    ceiling = EffectivePolicy(
        guards=GuardsMode.STRICT, read_only=True, admin_tools=False
    )
    settings = _settings(
        _rule(
            "everyone",
            user_levels=["user"],
            guards="off",
            read_only="off",
            admin_tools="on",
        )
    )

    policy = resolve(ARTIST, settings, ceiling)

    assert (policy.guards, policy.read_only, policy.admin_tools) == (
        GuardsMode.STRICT,
        True,
        False,
    )


def test_guards_off_needs_env_guards_off() -> None:
    settings = _settings(_rule("r", users=["joe"], guards="off"))
    env_off = EffectivePolicy(
        guards=GuardsMode.OFF, read_only=False, admin_tools=False
    )

    assert resolve(ARTIST, settings, CEILING).guards is GuardsMode.DEFAULT
    assert resolve(ARTIST, settings, env_off).guards is GuardsMode.OFF


def test_rule_without_criteria_matches_nobody() -> None:
    settings = _settings(_rule("nobody", read_only="on"))

    assert resolve(ARTIST, settings, CEILING).read_only is False


def test_disabled_rule_is_ignored() -> None:
    settings = _settings(
        {**_rule("off", users=["joe"], read_only="on"), "enabled": False}
    )

    assert resolve(ARTIST, settings, CEILING).read_only is False


def test_service_users_match_by_level() -> None:
    settings = _settings(_rule("services", user_levels=["service"], read_only="on"))

    assert resolve(SERVICE, settings, CEILING).read_only is True
    assert resolve(ARTIST, settings, CEILING).read_only is False


def test_unknown_caller_gets_the_default_policy() -> None:
    settings = _settings(
        _rule("everyone", user_levels=["user"], guards="strict"),
        read_only="on",
    )

    policy = resolve(None, settings, CEILING)

    assert policy.read_only is True
    assert policy.guards is GuardsMode.DEFAULT


# --- PolicyStore ----------------------------------------------------------


class FakeServer:
    """AYON endpoints the store reads, recording requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bundles: list[dict[str, Any]] = [
            {"name": "prod", "isProduction": True, "addons": {"mcp": "1.0.0"}},
            {"name": "stage", "isStaging": True, "addons": {"mcp": "1.1.0"}},
        ]
        self.settings: dict[str, Any] = {}
        self.fail = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(500)
        if request.url.path == "/api/bundles":
            return httpx.Response(200, json={"bundles": self.bundles})
        if request.url.path.startswith("/api/addons/mcp/"):
            return httpx.Response(200, json=self.settings)
        return httpx.Response(404)

    @property
    def paths(self) -> list[str]:
        return [
            f"{request.url.path}?{request.url.query.decode()}".rstrip("?")
            for request in self.requests
        ]


def _store(server: FakeServer, variant: str = "production") -> PolicyStore:
    return PolicyStore(
        BASE_URL,
        "service-key",
        variant=variant,
        transport=httpx.MockTransport(server.handle),
    )


@pytest.mark.asyncio
async def test_store_reads_settings_of_the_variant_bundle() -> None:
    server = FakeServer()
    server.settings = {"default_policy": {"read_only": "on"}}

    settings = await _store(server, "staging").settings()

    assert settings.default.read_only is True
    assert server.paths == [
        "/api/bundles",
        "/api/addons/mcp/1.1.0/settings?variant=staging",
    ]
    assert all(
        request.headers["x-api-key"] == "service-key"
        for request in server.requests
    )


@pytest.mark.asyncio
async def test_store_uses_the_ash_addon_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AYON_ADDON_NAME", "mcp")
    monkeypatch.setenv("AYON_ADDON_VERSION", "2.0.0")
    server = FakeServer()

    await _store(server).settings()

    assert server.paths == ["/api/addons/mcp/2.0.0/settings?variant=production"]


@pytest.mark.asyncio
async def test_store_without_mcp_addon_has_empty_settings() -> None:
    server = FakeServer()
    server.bundles = [{"name": "prod", "isProduction": True, "addons": {}}]

    assert await _store(server).settings() == PolicySettings()


@pytest.mark.asyncio
async def test_store_caches_settings_until_they_expire() -> None:
    server = FakeServer()
    store = _store(server)

    await store.settings()
    await store.settings()

    assert len(server.requests) == 2  # one bundles + one settings request

    store._loaded_at -= 61
    await store.settings()

    assert len(server.requests) == 4


@pytest.mark.asyncio
async def test_failed_refresh_keeps_previous_settings() -> None:
    server = FakeServer()
    server.settings = {"default_policy": {"read_only": "on"}}
    store = _store(server)
    await store.settings()

    server.fail = True
    store._loaded_at -= 61
    settings = await store.settings()

    assert settings.default.read_only is True


@dataclass
class FakeResponse:
    status: int
    data: Any


class FakeAyonClient:
    """Answers ``users/me`` like ayon_api's ServerAPI.get."""

    def __init__(self, user: dict[str, Any] | None) -> None:
        self.user = user
        self.calls = 0

    def get(self, path: str) -> FakeResponse:
        assert path == "users/me"
        self.calls += 1
        if self.user is None:
            return FakeResponse(401, {"detail": "nope"})
        return FakeResponse(200, self.user)


def _act_as(client: FakeAyonClient, user_name: str) -> None:
    set_global_ayon_api_key("service-key")
    set_global_ayon_as_user(user_name)
    set_global_ayon_client(client)  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_store_resolves_the_caller_policy() -> None:
    server = FakeServer()
    server.settings = {
        "rules": [_rule("artists", access_groups=["artist"], read_only="on")]
    }
    store = _store(server)
    client = FakeAyonClient(
        {"name": "joe", "data": {"defaultAccessGroups": ["artist"]}}
    )
    _act_as(client, "joe")

    policy = await store.policy()
    await store.policy()

    assert policy.read_only is True
    assert policy.matched_rules == ("artists",)
    assert client.calls == 1  # identity cached


@pytest.mark.asyncio
async def test_store_skips_the_user_lookup_for_empty_settings() -> None:
    store = _store(FakeServer())
    client = FakeAyonClient({"name": "joe", "data": {}})
    _act_as(client, "joe")

    assert await store.policy() == EffectivePolicy.from_env()
    assert client.calls == 0


@pytest.mark.asyncio
async def test_failed_user_lookup_gets_the_default_policy() -> None:
    server = FakeServer()
    server.settings = {
        "default_policy": {"read_only": "on"},
        "rules": [_rule("everyone", user_levels=["user"], guards="strict")],
    }
    store = _store(server)
    _act_as(FakeAyonClient(None), "joe")

    policy = await store.policy()

    assert policy.read_only is True
    assert policy.guards is GuardsMode.DEFAULT


# --- Provider -------------------------------------------------------------


def get_project(project_name: str) -> dict[str, str]:
    """Get a project by name."""
    return {"name": project_name}


def create_entity(entity_id: str) -> dict[str, str]:
    """Create an entity."""
    return {"id": entity_id}


def delete_entity(entity_id: str) -> dict[str, str]:
    """Delete an entity."""
    return {"id": entity_id}


def get_secrets() -> list[str]:
    """List secrets."""
    return []


setattr(get_secrets, HTTP_METHOD_ATTR, "GET")
setattr(get_secrets, HTTP_PATH_ATTR, "/api/secrets")


class FixedPolicyStore:
    """A policy store whose caller's policy the test sets directly."""

    def __init__(self, policy: EffectivePolicy) -> None:
        self.current = policy

    async def policy(self) -> EffectivePolicy:
        return self.current


@pytest.fixture
def store() -> FixedPolicyStore:
    return FixedPolicyStore(CEILING)


@pytest.fixture
def provider(store: FixedPolicyStore) -> AyonDynamicToolProvider:
    tools = create_curated_tools(
        [get_project, create_entity, delete_entity, get_secrets]
    )
    return AyonDynamicToolProvider(
        tools,
        policy_store=store,  # ty: ignore[invalid-argument-type]
    )


def _read_only(rule: str = "artists") -> EffectivePolicy:
    return EffectivePolicy(
        guards=GuardsMode.DEFAULT,
        read_only=True,
        admin_tools=True,
        matched_rules=(rule,),
    )


async def _names(provider: AyonDynamicToolProvider) -> set[str]:
    return {tool["name"] for tool in await provider.list_tools(limit=100)}


def test_admin_tools_are_flagged() -> None:
    tools = {
        tool.name: tool
        for tool in create_curated_tools([get_project, get_secrets])
    }

    assert tools["get_secrets"].admin is True
    assert tools["get_project"].admin is False


@pytest.mark.asyncio
async def test_listing_follows_the_caller_policy(
    provider: AyonDynamicToolProvider, store: FixedPolicyStore
) -> None:
    assert await _names(provider) == {
        "get_project", "create_entity", "delete_entity", "get_secrets"
    }

    store.current = _read_only()
    assert await _names(provider) == {"get_project", "get_secrets"}

    store.current = EffectivePolicy(
        guards=GuardsMode.DEFAULT, read_only=False, admin_tools=False
    )
    assert await _names(provider) == {
        "get_project", "create_entity", "delete_entity"
    }


@pytest.mark.asyncio
async def test_search_hides_tools_from_the_caller(
    provider: AyonDynamicToolProvider, store: FixedPolicyStore
) -> None:
    store.current = _read_only()

    results = await provider.search_tools("entity", limit=10)

    assert {result["name"] for result in results}.isdisjoint(
        {"create_entity", "delete_entity"}
    )


@pytest.mark.asyncio
async def test_read_only_caller_is_told_why(
    provider: AyonDynamicToolProvider, store: FixedPolicyStore
) -> None:
    store.current = _read_only()

    result = await provider.call_tool(
        "create_entity", {"entity_id": "1", "confirm_mutation": True}
    )

    assert result["success"] is False
    assert "read-only" in result["error"]
    assert "'artists'" in result["error"]


@pytest.mark.asyncio
async def test_hidden_admin_tool_looks_unknown(
    provider: AyonDynamicToolProvider, store: FixedPolicyStore
) -> None:
    store.current = EffectivePolicy(
        guards=GuardsMode.DEFAULT, read_only=False, admin_tools=False
    )

    result = await provider.call_tool("get_secrets", {})
    schema = await provider.get_tool_schema("get_secrets")

    assert result == {
        "success": False, "error": "Tool 'get_secrets' was not found."
    }
    assert schema["success"] is False


def put_secret(secret_name: str) -> dict[str, str]:
    """Save a secret."""
    return {"name": secret_name}


setattr(put_secret, HTTP_METHOD_ATTR, "PUT")
setattr(put_secret, HTTP_PATH_ATTR, "/api/secrets/{secret_name}")


@pytest.mark.asyncio
async def test_hidden_admin_write_tool_looks_unknown_when_read_only(
    store: FixedPolicyStore,
) -> None:
    provider = AyonDynamicToolProvider(
        create_curated_tools([get_project, put_secret]),
        policy_store=store,  # ty: ignore[invalid-argument-type]
    )
    store.current = EffectivePolicy(
        guards=GuardsMode.DEFAULT,
        read_only=True,
        admin_tools=False,
        matched_rules=("artists",),
    )

    result = await provider.call_tool(
        "put_secret", {"secret_name": "x", "confirm_mutation": True}
    )
    schema = await provider.get_tool_schema("put_secret")

    expected = "Tool 'put_secret' was not found."
    assert result == {"success": False, "error": expected}
    assert schema == {"success": False, "error": expected}


@pytest.mark.asyncio
async def test_search_follows_equal_sized_visible_sets(
    store: FixedPolicyStore,
) -> None:
    provider = AyonDynamicToolProvider(
        create_curated_tools([get_project, create_entity, get_secrets]),
        policy_store=store,  # ty: ignore[invalid-argument-type]
    )
    no_admin = EffectivePolicy(
        guards=GuardsMode.DEFAULT, read_only=False, admin_tools=False
    )

    async def found() -> set[str]:
        return {
            tool["name"]
            for query in ("secrets", "create entity", "project")
            for tool in await provider.search_tools(query, limit=10)
        }

    store.current = no_admin
    assert await found() == {"get_project", "create_entity"}
    store.current = _read_only()
    assert await found() == {"get_project", "get_secrets"}
    store.current = no_admin
    assert await found() == {"get_project", "create_entity"}


@pytest.mark.asyncio
async def test_schema_cache_does_not_leak_hidden_tools(
    provider: AyonDynamicToolProvider, store: FixedPolicyStore
) -> None:
    schema = await provider.get_tool_schema("create_entity")
    assert schema["function"]["name"] == "create_entity"

    store.current = _read_only()
    schema = await provider.get_tool_schema("create_entity")

    assert schema["success"] is False
    assert "read-only" in schema["error"]


@pytest.mark.asyncio
async def test_guard_profile_follows_the_caller_policy(
    provider: AyonDynamicToolProvider, store: FixedPolicyStore
) -> None:
    arguments = {"entity_id": "1", "confirm_mutation": True}

    result = await provider.call_tool("delete_entity", dict(arguments))
    assert result["success"] is True

    store.current = EffectivePolicy(
        guards=GuardsMode.STRICT, read_only=False, admin_tools=True
    )
    result = await provider.call_tool("delete_entity", dict(arguments))
    assert result["success"] is False

    store.current = CEILING
    result = await provider.call_tool("delete_entity", dict(arguments))
    assert result["success"] is True


def test_strict_chains_share_one_retry_guard(
    provider: AyonDynamicToolProvider,
) -> None:
    strict = provider._chain_for(
        EffectivePolicy(GuardsMode.STRICT, read_only=False, admin_tools=True)
    )
    strict_read_only = provider._chain_for(
        EffectivePolicy(GuardsMode.STRICT, read_only=True, admin_tools=True)
    )

    assert strict is not None and strict_read_only is not None
    assert strict.get("retry_safety") is strict_read_only.get("retry_safety")
    assert provider._chain_for(
        EffectivePolicy(GuardsMode.OFF, read_only=False, admin_tools=True)
    ) is None


def test_settings_json_round_trip() -> None:
    raw = {
        "default_policy": {
            "guards": "inherit", "read_only": "on", "admin_tools": "inherit"
        },
        "rules": [
            {
                "name": "admins",
                "enabled": True,
                "users": [],
                "user_levels": ["admin"],
                "access_groups": [],
                "guards": "strict",
                "read_only": "inherit",
                "admin_tools": "off",
            }
        ],
        "policy_refresh_seconds": 30,
    }

    settings = PolicySettings.parse(json.loads(json.dumps(raw)))

    assert settings.default.read_only is True
    assert settings.rules[0].user_levels == {"admin"}
    assert settings.rules[0].effect == PolicyEffect(
        guards=GuardsMode.STRICT, admin_tools=False
    )
    assert settings.refresh_seconds == 30

