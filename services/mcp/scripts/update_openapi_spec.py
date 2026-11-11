"""Refresh the tracked ``ayon_openapi.json`` from an AYON server.

The MCP server fetches the spec itself at startup, and only reads the tracked
file as a fallback. Run this deliberately, then review the changed endpoints
against ``ayon_mcp/rest_policy.py``; ``tests/test_rest_policy.py`` fails on
overrides and admin patterns that no longer match an endpoint.
"""

from __future__ import annotations

import argparse
import os

from ayon_mcp.openapi_tools import (
    SPEC_PATH,
    fetch_openapi_spec,
    write_tracked_spec,
)


def main() -> None:
    """Fetch the spec from the given server and overwrite the tracked file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server-url",
        default=os.getenv("AYON_SERVER_URL", ""),
        help="AYON server URL (default: AYON_SERVER_URL)",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("AYON_API_KEY", ""),
        help="AYON API key (default: AYON_API_KEY)",
    )
    args = parser.parse_args()
    if not args.server_url:
        parser.error("set --server-url or AYON_SERVER_URL")

    spec = fetch_openapi_spec(args.server_url, args.api_key)
    write_tracked_spec(spec)
    print(  # ruff: ignore[print]
        f"Wrote {SPEC_PATH} from AYON {spec.get('info', {}).get('version')}"
    )


if __name__ == "__main__":
    main()
