"""Tests for the CLI host/port handling."""
from __future__ import annotations

import click
import pytest

from ayon_mcp.cli import DEFAULT_PORT, build_server_url


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        # the default --host has a scheme but no port
        ("http://localhost", f"http://localhost:{DEFAULT_PORT}"),
        # https without a port keeps its default port
        ("https://ayon.example.com/", "https://ayon.example.com"),
        ("https://ayon.example.com:8443", "https://ayon.example.com:8443"),
        # a host without a scheme gets http://
        ("localhost", f"http://localhost:{DEFAULT_PORT}"),
        ("http://[::1]", f"http://[::1]:{DEFAULT_PORT}"),
        # a port in the host wins over --port
        ("http://localhost:8080", "http://localhost:8080"),
        ("localhost:8080", "http://localhost:8080"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        # repeated path slashes are not a scheme marker
        ("localhost:5001//", "http://localhost:5001"),
        ("localhost//", f"http://localhost:{DEFAULT_PORT}"),
        ("ayon.example.com/sub//", f"http://ayon.example.com:{DEFAULT_PORT}/sub"),
        # an empty port uses --port
        ("http://localhost:/", f"http://localhost:{DEFAULT_PORT}"),
        ("localhost:", f"http://localhost:{DEFAULT_PORT}"),
        ("http://[::1]:", f"http://[::1]:{DEFAULT_PORT}"),
        ("[::1]:", f"http://[::1]:{DEFAULT_PORT}"),
        # credentials are dropped, trailing slashes and spaces trimmed
        ("http://user:pw@host:5001", "http://host:5001"),
        ("https://ayon.example.com/ayon/", "https://ayon.example.com/ayon"),
        (" localhost ", f"http://localhost:{DEFAULT_PORT}"),
        ("HTTPS://ayon.example.com", "https://ayon.example.com"),
    ],
)
def test_build_server_url(host, expected):
    assert build_server_url(host, DEFAULT_PORT) == expected


def test_build_server_url_uses_port_option_when_host_has_none():
    assert (
        build_server_url("http://localhost", 6000) == "http://localhost:6000"
    )


@pytest.mark.parametrize(
    "host", ["http://localhost:abc", "http://localhost:99999"]
)
def test_build_server_url_rejects_invalid_port(host):
    with pytest.raises(click.UsageError, match="Invalid port number"):
        build_server_url(host, DEFAULT_PORT)


@pytest.mark.parametrize(
    ("host", "error"),
    [
        ("", "No host name"),
        ("http://", "No host name"),
        ("ftp://host", "Unsupported scheme"),
    ],
)
def test_build_server_url_rejects_unusable_hosts(host, error):
    with pytest.raises(click.UsageError, match=error):
        build_server_url(host, DEFAULT_PORT)
