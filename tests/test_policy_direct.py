"""Tests for the per-caller MCP policy in direct tool exposure mode."""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP

from ayon_mcp import server as server_module
from ayon_mcp.guardrails import GuardsMode
from ayon_mcp.policy import (
    EffectivePolicy,
    FilterMode,
    ToolFilter,
    get_current_policy,
)
from ayon_mcp.rest_policy import HTTP_METHOD_ATTR, HTTP_PATH_ATTR
from ayon_mcp.server import (
    DirectPolicyMiddleware,
    RemoteApiKeyMiddleware,
    ServiceUserMiddleware,
    register_tools,
)
from ayon_mcp.tool_discovery import create_curated_tools

CEILING = EffectivePolicy(
    guards=GuardsMode.DEFAULT, read_only=False, admin_tools=True
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "AYON_MCP_GUARDS",
        "AYON_MCP_READ_ONLY",
        "AYON_MCP_ADMIN_TOOLS",
    ):
        monkeypatch.delenv(var, raising=False)


def get_project(project_name: str) -> dict[str, str]:
    """Get a project by name."""
    return {"name": project_name}


def create_entity(entity_id: str) -> dict[str, str]:
    """Create an entity."""
    return {"id": entity_id}


def get_secrets() -> list[str]:
    """List secrets."""
    return []


def current_policy() -> dict[str, Any]:
    """Return the policy the middleware set for this call."""
    policy = get_current_policy()
    return {"policy": None if policy is None else policy.summary()}


setattr(get_secrets, HTTP_METHOD_ATTR, "GET")
setattr(get_secrets, HTTP_PATH_ATTR, "/api/secrets")

TOOLS = [get_project, create_entity, get_secrets, current_policy]


class FixedPolicyStore:
    """A policy store whose caller's policy the test sets directly."""

    def __init__(self, policy: EffectivePolicy) -> None:
        self.current = policy

    async def policy(self) -> EffectivePolicy:
        return self.current


def _server(policy: EffectivePolicy) -> FastMCP:
    mcp = FastMCP(name="test")
    register_tools(mcp, TOOLS)
    mcp.add_middleware(
        DirectPolicyMiddleware(
            create_curated_tools(TOOLS),
            FixedPolicyStore(policy),  # ty: ignore[invalid-argument-type]
        )
    )
    return mcp


async def _listed(mcp: FastMCP) -> set[str]:
    async with Client(mcp) as client:
        return {tool.name for tool in await client.list_tools()}


async def _call(mcp: FastMCP, name: str, **arguments: Any) -> tuple[bool, str]:
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False)
    text = result.content[0].text if result.content else ""  # ty: ignore[unresolved-attribute]
    return result.is_error, text


@pytest.mark.asyncio
async def test_ceiling_lists_every_tool() -> None:
    assert await _listed(_server(CEILING)) == {tool.__name__ for tool in TOOLS}


@pytest.mark.asyncio
async def test_read_only_hides_and_refuses_write_tools() -> None:
    policy = EffectivePolicy(
        guards=GuardsMode.DEFAULT,
        read_only=True,
        admin_tools=True,
        matched_rules=("artists",),
    )
    mcp = _server(policy)

    assert "create_entity" not in await _listed(mcp)
    is_error, text = await _call(mcp, "create_entity", entity_id="x")
    assert is_error
    assert "changes AYON data and is blocked" in text
    assert "'artists'" in text
    assert await _call(mcp, "get_project", project_name="demo") == (
        False,
        '{"name":"demo"}',
    )


@pytest.mark.asyncio
async def test_hidden_admin_tool_looks_unknown() -> None:
    policy = EffectivePolicy(
        guards=GuardsMode.DEFAULT, read_only=False, admin_tools=False
    )
    mcp = _server(policy)

    assert "get_secrets" not in await _listed(mcp)
    assert await _call(mcp, "get_secrets") == (
        True,
        "Tool 'get_secrets' was not found.",
    )


@pytest.mark.asyncio
async def test_tool_filters_apply() -> None:
    policy = EffectivePolicy(
        guards=GuardsMode.DEFAULT,
        read_only=False,
        admin_tools=True,
        tool_filters=(
            ToolFilter(FilterMode.ALLOWLIST, ("get_*", "current_policy")),
        ),
    )
    mcp = _server(policy)

    # REST tools use the openapi filters, so get_secrets stays.
    assert await _listed(mcp) == {"get_project", "get_secrets", "current_policy"}
    is_error, text = await _call(mcp, "create_entity", entity_id="x")
    assert is_error
    assert text == "Tool 'create_entity' was not found."


@pytest.mark.asyncio
async def test_filtered_write_tool_looks_unknown_when_read_only() -> None:
    policy = EffectivePolicy(
        guards=GuardsMode.DEFAULT,
        read_only=True,
        admin_tools=True,
        matched_rules=("artists",),
        tool_filters=(ToolFilter(FilterMode.DENYLIST, ("create_*",)),),
    )

    # Read-only alone would not reveal it: the denylist hides it anyway.
    assert await _call(_server(policy), "create_entity", entity_id="x") == (
        True,
        "Tool 'create_entity' was not found.",
    )


@pytest.mark.asyncio
async def test_calls_see_the_caller_policy() -> None:
    policy = EffectivePolicy(
        guards=GuardsMode.DEFAULT,
        read_only=True,
        admin_tools=True,
        matched_rules=("artists",),
    )
    is_error, text = await _call(_server(policy), "current_policy")

    assert not is_error
    assert '"matched_rules":["artists"]' in text.replace(" ", "")


@pytest.mark.asyncio
async def test_without_store_the_env_policy_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AYON_MCP_ADMIN_TOOLS", "false")
    mcp = FastMCP(name="test")
    register_tools(mcp, TOOLS)
    mcp.add_middleware(DirectPolicyMiddleware(create_curated_tools(TOOLS), None))

    assert "get_secrets" not in await _listed(mcp)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth",
    [
        RemoteApiKeyMiddleware("http://ayon.test"),
        ServiceUserMiddleware("http://ayon.test", "service-key"),
    ],
)
async def test_listing_without_credentials_does_not_fail(auth: Any) -> None:
    mcp = FastMCP(name="test")
    register_tools(mcp, [get_project])
    mcp.add_middleware(auth)

    assert await _listed(mcp) == {"get_project"}


@pytest.mark.asyncio
async def test_calls_without_credentials_still_fail() -> None:
    mcp = FastMCP(name="test")
    register_tools(mcp, [get_project])
    mcp.add_middleware(RemoteApiKeyMiddleware("http://ayon.test"))

    is_error, text = await _call(mcp, "get_project", project_name="demo")
    assert is_error
    assert "x-api-key" in text


@pytest.mark.parametrize(
    ("exposure", "has_policy_middleware"),
    [("direct", True), ("discovery", False)],
)
def test_create_mcp_server_adds_policy_after_auth(
    monkeypatch: pytest.MonkeyPatch,
    exposure: str,
    has_policy_middleware: bool,
) -> None:
    monkeypatch.setenv("AYON_MCP_TOOL_EXPOSURE", exposure)
    monkeypatch.setenv("AYON_MCP_ENABLE_ADDON_TOOLS", "false")
    monkeypatch.setattr(
        server_module, "collect_tools", lambda *_args: [get_project]
    )
    auth = RemoteApiKeyMiddleware("http://ayon.test")

    mcp = server_module.create_mcp_server("http://ayon.test", "key", auth=auth)

    types = [type(middleware) for middleware in mcp.middleware]
    assert RemoteApiKeyMiddleware in types
    assert (DirectPolicyMiddleware in types) is has_policy_middleware
    if has_policy_middleware:
        assert types.index(DirectPolicyMiddleware) > types.index(
            RemoteApiKeyMiddleware
        )
