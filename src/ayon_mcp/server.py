"""Command line entrypoint for the AYON MCP server."""

from __future__ import annotations

import argparse
import os

from ayon_mcp.app import mcp
import ayon_mcp.tools  # noqa: F401  (registers all tools)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ayon-mcp",
        description=(
            "MCP server for the AYON pipeline platform. Requires "
            "AYON_SERVER_URL and AYON_API_KEY (environment variables "
            "or CLI options)."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio for local clients (default), http for streamable HTTP",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8021, help="HTTP bind port")
    parser.add_argument("--server-url", help="AYON server URL (overrides AYON_SERVER_URL)")
    parser.add_argument("--api-key", help="AYON API key (overrides AYON_API_KEY)")
    args = parser.parse_args()

    if args.server_url:
        os.environ["AYON_SERVER_URL"] = args.server_url
    if args.api_key:
        os.environ["AYON_API_KEY"] = args.api_key

    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
