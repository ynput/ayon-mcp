"""Tests for side-effect and admin classification of generated REST tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from chuk_tool_processor.guards import SideEffectClass

from ayon_mcp import rest_policy
from ayon_mcp.openapi_codegen import HTTP_METHODS
from ayon_mcp.rest_policy import (
    ADMIN_PATTERNS,
    SIDE_EFFECT_OVERRIDES,
    endpoint_of,
    is_admin,
    side_effect_for,
)
from ayon_mcp.tool_discovery import (
    AyonDynamicToolProvider,
    create_curated_tools,
    drop_admin_tools,
    tool_side_effect,
)

from conftest import requires_generated_tools

SPEC_PATH = (
    Path(__file__).parent.parent / "services" / "mcp" / "ayon_openapi.json"
)


def _spec_operations() -> set[tuple[str, str]]:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    return {
        (method.upper(), path)
        for path, item in spec["paths"].items()
        for method in item
        if method in HTTP_METHODS
    }


def _rest_tool(method: str, path: str, name: str = "rest_tool"):
    async def function() -> dict[str, str]:
        return {"ok": "yes"}

    function.__name__ = name
    setattr(function, rest_policy.HTTP_METHOD_ATTR, method)
    setattr(function, rest_policy.HTTP_PATH_ATTR, path)
    return function


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("GET", SideEffectClass.READ_ONLY),
        ("POST", SideEffectClass.WRITE),
        ("PUT", SideEffectClass.WRITE),
        ("PATCH", SideEffectClass.WRITE),
        ("DELETE", SideEffectClass.DESTRUCTIVE),
        ("TRACE", SideEffectClass.WRITE),  # unknown method fails closed
    ],
)
def test_method_defaults(method: str, expected: SideEffectClass) -> None:
    assert side_effect_for(method, "/api/not-overridden") is expected


def test_overrides_take_precedence_over_method() -> None:
    assert side_effect_for("POST", "/api/query") is SideEffectClass.READ_ONLY
    assert (
        side_effect_for("POST", "/api/system/restart")
        is SideEffectClass.DESTRUCTIVE
    )
    assert (
        side_effect_for("POST", "/api/users/passwordReset")
        is SideEffectClass.DESTRUCTIVE
    )
    assert (
        side_effect_for("POST", "/api/users/acceptInvite")
        is SideEffectClass.DESTRUCTIVE
    )


@pytest.mark.skipif(not SPEC_PATH.exists(), reason="no local OpenAPI spec")
def test_every_override_matches_an_operation_in_the_spec() -> None:
    operations = _spec_operations()
    stale = sorted(set(SIDE_EFFECT_OVERRIDES) - operations)
    assert not stale, f"overrides for operations not in the spec: {stale}"


@pytest.mark.skipif(not SPEC_PATH.exists(), reason="no local OpenAPI spec")
def test_every_admin_pattern_matches_an_operation_in_the_spec() -> None:
    operations = _spec_operations()
    stale = [
        pattern.pattern
        for methods, pattern in ADMIN_PATTERNS
        if not any(
            (methods is None or method in methods) and pattern.search(path)
            for method, path in operations
        )
    ]
    assert not stale, f"admin patterns matching nothing in the spec: {stale}"


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/api/secrets/{secret_name}", True),
        ("GET", "/api/users/{user_name}/apikeys", True),
        ("POST", "/api/system/restart", True),
        ("PATCH", "/api/users/{user_name}", True),
        ("POST", "/api/users/passwordReset", True),
        ("POST", "/api/users/acceptInvite", True),
        ("GET", "/api/users/{user_name}", False),
        ("GET", "/api/accessGroups/{project_name}", False),
        ("PUT", "/api/accessGroups/{access_group_name}/{project_name}", True),
        ("DELETE", "/api/addons/{addon_name}/{addon_version}", True),
        ("DELETE", "/api/addons/{addon_name}/{addon_version}/overrides", False),
        ("GET", "/api/projects/{project_name}", False),
    ],
)
def test_is_admin(method: str, path: str, expected: bool) -> None:
    assert is_admin(method, path) is expected


def test_endpoint_of_reads_attributes_and_legacy_docstring() -> None:
    assert endpoint_of(_rest_tool("delete", "/api/x")) == ("DELETE", "/api/x")

    def legacy() -> None:
        """Legacy.

Endpoint: PATCH /api/y/{id}

More text."""

    assert endpoint_of(legacy) == ("PATCH", "/api/y/{id}")

    def curated() -> None:
        """Curated tool without an endpoint."""

    assert endpoint_of(curated) is None


def test_tool_side_effect_for_curated_and_rest_tools() -> None:
    def delete_entity() -> None: ...
    def create_entity() -> None: ...
    def get_project() -> None: ...

    assert tool_side_effect(delete_entity) is SideEffectClass.DESTRUCTIVE
    assert tool_side_effect(create_entity) is SideEffectClass.WRITE
    assert tool_side_effect(get_project) is SideEffectClass.READ_ONLY
    assert (
        tool_side_effect(_rest_tool("POST", "/api/query"))
        is SideEffectClass.READ_ONLY
    )
    assert (
        tool_side_effect(_rest_tool("DELETE", "/api/projects/{p}"))
        is SideEffectClass.DESTRUCTIVE
    )


def test_drop_admin_tools() -> None:
    def get_project() -> None: ...

    secret = _rest_tool("GET", "/api/secrets", name="list_secrets")
    folders = _rest_tool("GET", "/api/projects/{p}/folders", name="folders")

    kept = drop_admin_tools([get_project, secret, folders])

    assert kept == [get_project, folders]


@pytest.mark.asyncio
async def test_strict_mode_blocks_rest_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AYON_MCP_GUARDS", "strict")
    monkeypatch.delenv("AYON_MCP_READ_ONLY", raising=False)
    monkeypatch.delenv("AYON_SERVER_URL", raising=False)
    tool = _rest_tool("DELETE", "/api/projects/{p}", name="delete_project")
    provider = AyonDynamicToolProvider(create_curated_tools([tool]))

    result = await provider.execute_tool(
        "delete_project", {"confirm_mutation": True}
    )

    assert result["success"] is False


@requires_generated_tools
def test_every_generated_tool_carries_its_endpoint() -> None:
    from ayon_mcp.tools.openapi_generated import ALL_OPENAPI_TOOLS

    missing = [
        tool.__name__
        for tool in ALL_OPENAPI_TOOLS
        if getattr(tool, rest_policy.HTTP_METHOD_ATTR, None) is None
    ]
    assert not missing, (
        "generated tools without @endpoint - regenerate them: "
        f"{missing[:5]}"
    )
