"""Tests for optional OpenAPI generated tool registration."""
from __future__ import annotations

import importlib
import pytest


def _reload_tools_module():
    import ayon_mcp.tools as tools_module

    return importlib.reload(tools_module)


def test_openapi_tools_disabled_by_default(monkeypatch):
    """Generated OpenAPI tools are not registered unless env flag is enabled."""
    monkeypatch.delenv("AYON_MCP_ENABLE_OPENAPI_TOOLS", raising=False)

    tools_module = _reload_tools_module()
    names = {fn.__name__ for fn in tools_module.ALL_TOOLS}

    assert "get_access_group_schema" not in names


def test_openapi_tools_registered_when_enabled(monkeypatch):
    """Generated OpenAPI tools are appended when env flag is truthy."""
    monkeypatch.delenv("AYON_MCP_ENABLE_OPENAPI_TOOLS", raising=False)
    base_tools_module = _reload_tools_module()
    base_names = {fn.__name__ for fn in base_tools_module.ALL_TOOLS}

    monkeypatch.setenv("AYON_MCP_ENABLE_OPENAPI_TOOLS", "true")

    try:
        import ayon_mcp.tools.openapi_generated  # noqa: F401
    except ImportError:
        pytest.skip("openapi_generated tools are not present; run the generator before enabling")


    enabled_tools_module = _reload_tools_module()
    enabled_names = {fn.__name__ for fn in enabled_tools_module.ALL_TOOLS}

    assert "get_access_group_schema" in enabled_names
    assert len(enabled_names) > len(base_names)
