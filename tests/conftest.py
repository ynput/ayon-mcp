"""Test configuration and shared fixtures."""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

import dotenv
import httpx
import pytest

# Make `ayon_mcp` importable from the services sub-project.
sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "mcp"))
dotenv.load_dotenv(dotenv.find_dotenv(), override=True)


GENERATED_DIR = (
    Path(__file__).parent.parent
    / "services" / "mcp" / "ayon_mcp" / "tools" / "openapi_generated"
)

requires_generated_tools = pytest.mark.skipif(
    not (GENERATED_DIR / "__init__.py").exists(),
    reason=(
        "Generated OpenAPI tools are missing; run "
        "`uv run python ./services/mcp/scripts/generate_openapi_tools.py`"
    ),
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require a live AYON server "
        "(set AYON_SERVER_URL and AYON_API_KEY to enable)",
    )
    config.addinivalue_line(
        "markers",
        "llm: marks tests that drive the MCP server through a real model "
        "(local Ollama via OLLAMA_HOST/OLLAMA_MODEL, or the Anthropic API "
        "via ANTHROPIC_API_KEY/ANTHROPIC_MODEL)",
    )


REPO_ROOT = Path(__file__).parent.parent
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"


@pytest.fixture(scope="session")
def ollama_host() -> str:
    """Return the configured Ollama base URL."""
    return os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST).rstrip("/")


@pytest.fixture(scope="session")
def ollama_model() -> str:
    """Return the configured Ollama model tag."""
    return os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


@pytest.fixture(scope="session")
def anthropic_model() -> str:
    """Return the configured Claude model id."""
    return os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)


@pytest.fixture
def require_anthropic_key() -> None:
    """Skip the test unless an Anthropic API key is configured."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        pytest.skip(
            "ANTHROPIC_API_KEY not set; skipping Claude-driven LLM tests."
        )


@pytest.fixture
def require_ollama(ollama_host: str, ollama_model: str) -> None:
    """Skip the test unless Ollama is reachable.
     
     Another condition is that the specified model must be pulled into Ollama.
     
     Args:
         ollama_host: The base URL of the Ollama server.
         ollama_model: The model tag that must be pulled into Ollama.
     
     Raises:
         pytest.skip: If Ollama is not reachable or the model is not pulled.
     
     """
    try:
        response = httpx.get(f"{ollama_host}/api/tags", timeout=3)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"Ollama not reachable at {ollama_host}: {exc}")

    tags = {m["model"] for m in response.json().get("models", [])}
    base_name = ollama_model.split(":", 1)[0]
    if ollama_model not in tags and not any(
        tag.split(":", 1)[0] == base_name for tag in tags
    ):
        pytest.skip(
            f"Model {ollama_model!r} not pulled in Ollama. "
            f"Run `ollama pull {ollama_model}` first."
        )


def _otel_endpoint_reachable(endpoint: str, timeout: float = 1.0) -> bool:
    """Return True if a TCP connection to the OTLP endpoint succeeds."""
    parsed = urlparse(endpoint)
    host = parsed.hostname
    port = parsed.port or 4317
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def telemetry_enabled() -> bool:
    """Return True when an OTLP collector (Vector/Tempo) is reachable.

    Optional: tests run fine without it, but when the observability stack
    (Loki/Vector/Tempo) is up, the spawned MCP server is launched through
    `opentelemetry-instrument` so its real metrics/traces show up there too.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return False
    return _otel_endpoint_reachable(endpoint)


@pytest.fixture
def mcp_llm_server_params(
    ayon_connection_env: tuple[str, str],
    telemetry_enabled: bool,
):
    """Build StdioServerParameters to spawn the MCP server for LLM tests.

    Runs in discovery exposure mode with generated OpenAPI tools disabled,
    so the tool surface stays small and startup doesn't need to sync
    against the live server's OpenAPI spec.
    """
    from mcp import StdioServerParameters

    server_url, api_key = ayon_connection_env
    scripts_dir = REPO_ROOT / "scripts"
    if sys.platform == "win32":
        command = "powershell"
        script = scripts_dir / "start_local.ps1"
        # -NoProfile: a user profile that prints to stdout (e.g. module
        # update banners) would corrupt the stdio JSON-RPC stream.
        args = ["-NoProfile", str(script)]
    else:
        command = "bash"
        script = scripts_dir / "start_local.sh"
        args = [str(script)]

    env = {
        **os.environ,
        "AYON_SERVER_URL": server_url,
        "AYON_API_KEY": api_key,
        "AYON_MCP_TOOL_EXPOSURE": "discovery",
        "AYON_MCP_ENABLE_OPENAPI_TOOLS": "false",
        "AYON_MCP_ENABLE_TELEMETRY": "1" if telemetry_enabled else "0",
    }
    return StdioServerParameters(command=command, args=args, env=env)


@pytest.fixture
def mock_api():
    """Return a MagicMock wired in as the global AYON API client."""
    from ayon_mcp.client import set_global_ayon_client

    client = MagicMock()
    set_global_ayon_client(client)
    return client


@pytest.fixture(scope="session")
def ayon_connection_env() -> tuple[str, str]:
    """Return (server_url, api_key) or skip when env vars are absent."""
    server_url = os.environ.get("AYON_SERVER_URL", "").strip()
    api_key = os.environ.get("AYON_API_KEY", "").strip()
    if not server_url or not api_key:
        pytest.skip(
            "Integration tests require AYON_SERVER_URL and AYON_API_KEY env vars"
        )
    return server_url, api_key


@pytest.fixture
def ayon_client(ayon_connection_env: tuple[str, str]):
    """Return a live AYON client set as the global client for this test."""
    from ayon_mcp.client import get_ayon_api, set_global_ayon_client

    server_url, api_key = ayon_connection_env
    client = get_ayon_api(server_url, api_key)
    set_global_ayon_client(client)
    return client
