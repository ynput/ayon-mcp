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
        (
            "https://ayon.example.com/",
            f"https://ayon.example.com:{DEFAULT_PORT}",
        ),
        ("localhost", f"localhost:{DEFAULT_PORT}"),
        ("http://[::1]", f"http://[::1]:{DEFAULT_PORT}"),
        # a port in the host wins over --port
        ("http://localhost:8080", "http://localhost:8080"),
        ("localhost:8080", "localhost:8080"),
        ("http://[::1]:8080", "http://[::1]:8080"),
    ],
)
def test_build_server_url(host, expected):
    assert build_server_url(host, DEFAULT_PORT) == expected


def test_build_server_url_uses_port_option_when_host_has_none():
    assert (
        build_server_url("http://localhost", 6000) == "http://localhost:6000"
    )


def test_build_server_url_rejects_invalid_port():
    with pytest.raises(click.UsageError, match="Invalid port number"):
        build_server_url("http://localhost:abc", DEFAULT_PORT)
