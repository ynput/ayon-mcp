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
    ``host`` wins over ``port``. A host without a scheme gets ``http://``;
    an ``https://`` host without a port keeps the scheme's default (443).

    Args:
        host: Host name or URL, with or without scheme and port.
        port: Port used when ``host`` carries none.

    Returns:
        The server URL as ``scheme://host[:port][path]``.

    Raises:
        click.UsageError: If ``host`` has no host name, a scheme other
            than http or https, or an invalid port.

    """
    host = host.strip()
    # Only "://" marks a scheme: "//" alone may be repeated path slashes.
    parts = urlsplit(host if "://" in host else f"//{host}")
    try:
        url_port = parts.port
    except ValueError as e:
        msg = f"Invalid port number in host URL: {host!r} ({e})."
        raise click.UsageError(msg) from e

    scheme = parts.scheme or "http"
    if scheme not in {"http", "https"}:
        msg = f"Unsupported scheme in host URL: {host!r} (use http or https)."
        raise click.UsageError(msg)
    # hostname drops credentials and an empty port ("localhost:"), but
    # also the brackets of an IPv6 address.
    hostname = parts.hostname
    if not hostname:
        msg = f"No host name in host URL: {host!r}."
        raise click.UsageError(msg)
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if url_port is None and scheme != "https":
        url_port = port
    netloc = hostname if url_port is None else f"{hostname}:{url_port}"
    return f"{scheme}://{netloc}{parts.path.rstrip('/')}"


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
    default=DEFAULT_PORT, type=int,
    help=(
        "AYON server port when --host has none "
        "(default 5000, not added to https:// hosts)"
    ))
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
