"""Lazy connection to the AYON server.

The connection is created on first use so the MCP server can start
(and list its tools) even before credentials are configured. Any tool
call without valid credentials returns a clear, actionable error.
"""

from __future__ import annotations

import os

from ayon_api import ServerAPI
from ayon_api.exceptions import UrlError

_api: ServerAPI | None = None


def api() -> ServerAPI:
    """Return an authenticated :class:`ServerAPI`, creating it on first use."""
    global _api
    if _api is not None:
        return _api

    url = os.environ.get("AYON_SERVER_URL", "").strip()
    key = os.environ.get("AYON_API_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "AYON connection is not configured. Set the AYON_SERVER_URL and "
            "AYON_API_KEY environment variables (an API key can be created in "
            "AYON under user profile, or as a service user key)."
        )

    try:
        server = ServerAPI(url, token=key)
    except UrlError as exc:
        raise RuntimeError(f"Invalid AYON server URL {url!r}: {exc}") from exc

    if not server.has_valid_token:
        raise RuntimeError(
            f"Could not authenticate against AYON server at {url!r}. "
            "Check that AYON_API_KEY is a valid, non-expired API key "
            "and that the server is reachable."
        )

    _api = server
    return _api


def reset() -> None:
    """Drop the cached connection (used by tests)."""
    global _api
    _api = None
