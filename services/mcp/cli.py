"""CLI interface for the AYON MCP server."""
from __future__ import annotations

import os

import click

from .server import run_local as run_local_server
from .server import run_remote as run_remote_server

# define env var names for server URL and API key
HOST_ENV_VAR = "AYON_SERVER_URL"
KEY_ENV_VAR = "AYON_API_KEY"

DEFAULT_HOST = "http://localhost"
DEFAULT_PORT = 5000


@click.command()
@click.option(
    "--remote", "-r",
    is_flag=True,
    help="Run the server in remote mode (as a service).")
@click.option(
    "--host", "-h",
    default=DEFAULT_HOST,
    help="AYON server URL (or set AYON_SERVER_URL)")
@click.option(
    "--port", "-p",
    default=DEFAULT_PORT, type=int, help="AYON server port (default 5000)")
@click.option(
    "--api-key",
    default=None, help="AYON API key (or set AYON_API_KEY)")
def main(
    host: str,
    port: int,
    api_key: str | None,
    *,
    remote: bool = False) -> None:
    """Run the MCP server.

    Args:
        host: AYON server URL (or set AYON_SERVER_URL)
        port: AYON server port (default 5000)
        api_key: AYON API key (or set AYON_API_KEY)
        remote: Run the server in remote mode (as a service).

    Raises:
        click.UsageError: If required arguments are missing or invalid.

    """
    server_url = f"{host}:{port}"
    if not api_key:
        api_key = os.getenv(KEY_ENV_VAR)
        if not api_key:
            msg = (
                "AYON API key is required in remote mode. "
                "Set the AYON_API_KEY environment variable "
                "or pass it as --api-key.")
            raise click.UsageError(msg)

    if remote:
        run_remote_server(server_url, api_key)
    else:
        # run in local mode
        run_local_server(server_url, api_key)


if __name__ == "__main__":
    main()
