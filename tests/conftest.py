"""Test configuration and shared fixtures."""
from __future__ import annotations

import dotenv
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make `ayon_mcp` importable from the services sub-project.
sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "mcp"))
dotenv.load_dotenv(dotenv.find_dotenv(), override=True)



def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require a live AYON server "
        "(set AYON_SERVER_URL and AYON_API_KEY to enable)",
    )


@pytest.fixture()
def mock_api():
    """Return a MagicMock wired in as the global AYON API client."""
    from ayon_mcp.client import set_global_ayon_client

    client = MagicMock()
    set_global_ayon_client(client)
    return client


@pytest.fixture(scope="session")
def ayon_connection_env() -> tuple[str, str]:
    """Return (server_url, api_key) or skip when env vars are absent."""
    server_url = os.environ.get("AYON_SERVER_URL", "").strip()
    api_key = os.environ.get("AYON_API_KEY", "").strip()
    if not server_url or not api_key:
        pytest.skip(
            "Integration tests require AYON_SERVER_URL and AYON_API_KEY env vars"
        )
    return server_url, api_key


@pytest.fixture()
def ayon_client(ayon_connection_env: tuple[str, str]):
    """Return a live AYON client set as the global client for this test."""
    from ayon_mcp.client import get_ayon_api, set_global_ayon_client

    server_url, api_key = ayon_connection_env
    client = get_ayon_api(server_url, api_key)
    set_global_ayon_client(client)
    return client
