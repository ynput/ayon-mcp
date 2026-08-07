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
    envvar=HOST_ENV_VAR,
    show_envvar=True,
    help="AYON server URL",
)
@click.option(
    "--port", "-p",
    default=DEFAULT_PORT, type=int, help="AYON server port (default 5000)")
@click.option(
    "--api-key",
    default=None,
    envvar=KEY_ENV_VAR,
    show_envvar=True,
    help="AYON API key"
)
def main(
    host: str,
    port: int | None,
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
    # Only treat the last colon as a port separator when it is followed
    # by digits — a scheme URL without a port ("http://localhost") also
    # contains a colon.
    head, sep, tail = host.rpartition(":")
    if sep and tail.isdigit():
        host = head
        port = int(tail)
    elif sep and "/" not in tail:
        msg = (
            f"Invalid port number in host URL: {tail!r}. "
            "Port must be an integer.")
        raise click.UsageError(msg)

    server_url = f"{host}:{port}"
    if not api_key:
        api_key = os.getenv(KEY_ENV_VAR)
        if not api_key and not remote:
            msg = (
                "AYON API key is required in local mode. "
                "Set the AYON_API_KEY environment variable "
                "or pass it as --api-key.")
            raise click.UsageError(msg)

    if remote:
        run_remote_server(server_url)
        return

    # run in local mode
    run_local_server(server_url, api_key)  # ty:ignore[invalid-argument-type]


if __name__ == "__main__":
    main()
