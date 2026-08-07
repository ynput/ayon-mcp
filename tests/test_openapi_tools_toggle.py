"""Tests for optional OpenAPI generated tool registration."""
from __future__ import annotations

import importlib
import os

import pytest

def _reload_tools_module():
    import ayon_mcp.tools as tools_module

    return importlib.reload(tools_module)


@pytest.fixture()
def restore_tools_module():
    """Re-reload ``ayon_mcp.tools`` in a clean env after the test.

    The reload-based tests below would otherwise leave a polluted
    ``ALL_TOOLS`` behind for the rest of the test session.
    """
    yield
    os.environ.pop("AYON_MCP_ENABLE_OPENAPI_TOOLS", None)
    _reload_tools_module()


def test_openapi_tools_enabled_by_default(monkeypatch, restore_tools_module):
    """Generated OpenAPI tools are registered when env flag is not set."""
    monkeypatch.delenv("AYON_MCP_ENABLE_OPENAPI_TOOLS", raising=False)

    try:
        import ayon_mcp.tools.openapi_generated  # noqa: F401
    except ImportError:
        pytest.skip(
            "openapi_generated tools are not present; run the generator before "
            "reloading tools"
        )

    tools_module = _reload_tools_module()
    names = {fn.__name__ for fn in tools_module.ALL_TOOLS}

    assert "get_access_group_schema" in names


@pytest.mark.usefixtures("restore_tools_module")
def test_openapi_tools_not_registered_when_disabled(
    monkeypatch,
    restore_tools_module,
):
    """Generated OpenAPI tools are not registered when env flag is falsey."""
    monkeypatch.delenv("AYON_MCP_ENABLE_OPENAPI_TOOLS", raising=False)
    base_tools_module = _reload_tools_module()
    base_names = {fn.__name__ for fn in base_tools_module.ALL_TOOLS}

    monkeypatch.setenv("AYON_MCP_ENABLE_OPENAPI_TOOLS", "false")

    try:
        import ayon_mcp.tools.openapi_generated  # noqa: F401
    except ImportError:
        pytest.skip(
            "openapi_generated tools are not present; run the generator before "
            "reloading tools"
        )


    disabled_tools_module = _reload_tools_module()
    disabled_names = {fn.__name__ for fn in disabled_tools_module.ALL_TOOLS}

    assert "get_access_group_schema" not in disabled_names
    assert len(disabled_names) < len(base_names)
