"""CLI interface for the AYON MCP server."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import click

from .server import run_local as run_local_server
from .server import run_remote as run_remote_server
from .server import run_tunnel as run_tunnel_server

# define env var names for server URL and API key
HOST_ENV_VAR = "AYON_SERVER_URL"
KEY_ENV_VAR = "AYON_API_KEY"

DEFAULT_HOST = "http://localhost"
DEFAULT_PORT = 5000


def build_server_url(host: str, port: int) -> str:
    """Join a ``--host`` value and ``--port`` into the AYON server URL.

    ``host`` may be a bare host, ``host:port`` or a full URL; a port inside
    ``host`` wins over ``port``. A host without a scheme gets ``http://``.

    Args:
        host: Host name or URL, with or without scheme and port.
        port: Port used when ``host`` carries none.

    Returns:
        The server URL as ``scheme://host:port[path]``.

    Raises:
        click.UsageError: If the port in ``host`` is not valid.

    """
    parts = urlsplit(host if "//" in host else f"//{host}")
    try:
        url_port = parts.port
    except ValueError as e:
        msg = (
            f"Invalid port number in host URL: {host!r}. "
            "Port must be an integer.")
        raise click.UsageError(msg) from e

    scheme = f"{parts.scheme or 'http'}://"
    hostname = parts.netloc.rpartition("@")[2]
    if url_port is not None:
        hostname = hostname.rpartition(":")[0]
    return f"{scheme}{hostname}:{port if url_port is None else url_port}{parts.path.rstrip('/')}"


@click.command()
@click.option(
    "--remote", "-r",
    is_flag=True,
    help="Run the server in remote mode (as a service).")
@click.option(
    "--tunnel", "-t",
    is_flag=True,
    help=(
        "Run as a service reached through the MCP addon on the AYON "
        "server (connects out over a WebSocket, opens no port)."
    ))
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
    remote: bool = False,
    tunnel: bool = False) -> None:
    """Run the MCP server.

    Args:
        host: AYON server URL (or set AYON_SERVER_URL)
        port: AYON server port (default 5000)
        api_key: AYON API key (or set AYON_API_KEY)
        remote: Run the server in remote mode (as a service).
        tunnel: Run as a service behind the AYON server's MCP tunnel.

    Raises:
        click.UsageError: If required arguments are missing or invalid.

    """
    server_url = build_server_url(host, port)
    if not api_key:
        api_key = os.getenv(KEY_ENV_VAR)
        if not api_key:
            msg = (
                "AYON API key is required in local mode. "
                "Set the AYON_API_KEY environment variable "
                "or pass it as --api-key.")
            raise click.UsageError(msg)

    if tunnel:
        run_tunnel_server(server_url, api_key)
        return

    if remote:
        run_remote_server(server_url, api_key)
        return

    # run in local mode
    run_local_server(server_url, api_key)


if __name__ == "__main__":
    main()
