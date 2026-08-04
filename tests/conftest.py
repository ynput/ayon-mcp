"""Test configuration and shared fixtures."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make `ayon_mcp` importable from the services sub-project.
sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "mcp"))


@pytest.fixture()
def mock_api():
    """Return a MagicMock wired in as the global AYON API client."""
    from ayon_mcp.client import set_global_ayon_client

    client = MagicMock()
    set_global_ayon_client(client)
    return client
