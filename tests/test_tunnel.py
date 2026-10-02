"""Tests for the service side of the HTTP-over-WebSocket tunnel to AYON.

The end-to-end tests run a fake AYON server (``fake_tunnel_server``, the
same protocol as ``ayon_server/mcp`` in ayon-backend) on a real uvicorn
port, connect the service's ``TunnelClient`` to it and talk to the proxied
endpoint over HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from unittest.mock import MagicMock

import httpx
import pytest
import uvicorn
from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.dependencies import get_http_headers
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from ayon_mcp import server as server_module
from ayon_mcp import tunnel as tunnel_module
from ayon_mcp import tunnel_protocol
from ayon_mcp.client import (
    get_global_ayon_api_key,
    get_global_ayon_as_user,
    get_global_ayon_client,
)
from ayon_mcp.server import ServiceUserMiddleware
from ayon_mcp.tunnel import TunnelClient, serve_forever, tunnel_url
from ayon_mcp.tunnel_protocol import USER_HEADER, Frame, FrameType

import fake_tunnel_server as addon_tunnel


def test_frame_round_trip() -> None:
    stream_id = uuid.uuid4()
    frame = Frame.with_json(FrameType.RESPONSE_START, stream_id, {"a": 1})

    decoded = Frame.decode(frame.encode())

    assert decoded == frame
    assert decoded.json() == {"a": 1}


def test_frame_decode_rejects_short_data() -> None:
    with pytest.raises(ValueError, match="too short"):
        Frame.decode(b"\x01abc")


def test_chunks_split_large_bodies() -> None:
    size = tunnel_protocol.MAX_CHUNK_SIZE
    body = b"x" * (size * 2 + 1)

    parts = tunnel_protocol.chunks(body)

    assert [len(part) for part in parts] == [size, size, 1]
    assert tunnel_protocol.chunks(b"") == []


def test_filter_headers_drops_hop_by_hop_and_extra() -> None:
    headers = [
        ("Host", "ayon"),
        ("Connection", "keep-alive"),
        ("Cookie", "accessToken=secret"),
        ("Mcp-Session-Id", "abc"),
    ]

    assert tunnel_protocol.filter_headers(
        headers, drop=frozenset({"cookie"})
    ) == [("mcp-session-id", "abc")]


def test_tunnel_url_from_server_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AYON_MCP_TUNNEL_URL", raising=False)

    assert (
        tunnel_url("https://ayon.example.com/")
        == "wss://ayon.example.com/api/mcp/ws"
    )
    assert tunnel_url("http://server:5000") == "ws://server:5000/api/mcp/ws"


def test_tunnel_url_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AYON_MCP_TUNNEL_URL", "ws://custom/ws")
    assert tunnel_url("http://ignored") == "ws://custom/ws"


# --- end-to-end ------------------------------------------------------------


class FakeAyonServer:
    """Fake AYON server: the MCP tunnel endpoints on a real port."""

    def __init__(self) -> None:
        self.registry = addon_tunnel.TunnelRegistry()
        self.app = Starlette(routes=[
            WebSocketRoute("/ws", self._ws),
            Route("/mcp", self._proxy, methods=["GET", "POST", "DELETE"]),
        ])
        self.port = 0

    async def _ws(self, websocket: WebSocket) -> None:
        if websocket.headers.get("x-api-key") != "service-key":
            await addon_tunnel.reject(
                websocket, addon_tunnel.CLOSE_UNAUTHORIZED, "invalid API key"
            )
            return
        await addon_tunnel.serve_tunnel(websocket, self.registry, "service")

    async def _proxy(self, request: Request) -> Response:
        # Stands in for AYON auth: the authenticated user's name.
        return await addon_tunnel.proxy_request(
            request,
            self.registry,
            path="/mcp",
            user_name=request.headers.get("x-test-user", "nobody"),
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def wait_for_tunnels(self, count: int = 1) -> None:
        for _ in range(200):
            if len(self.registry) >= count:
                return
            await asyncio.sleep(0.02)
        msg = f"expected {count} tunnel(s), have {len(self.registry)}"
        raise AssertionError(msg)


@contextlib.asynccontextmanager
async def running_server(fake: FakeAyonServer):
    config = uvicorn.Config(
        fake.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    fake.port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield fake
    finally:
        server.should_exit = True
        await task


@contextlib.asynccontextmanager
async def running_task(coro):
    task = asyncio.create_task(coro)
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_proxy_returns_503_without_tunnel() -> None:
    async with running_server(FakeAyonServer()) as fake:
        async with httpx.AsyncClient() as http:
            response = await http.post(f"{fake.url}/mcp", json={})

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


@pytest.mark.asyncio
async def test_mcp_tool_call_through_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[MagicMock] = []

    def fake_get_ayon_api(base_url: str, api_key: str) -> MagicMock:
        client = MagicMock(name=f"ServerAPI({api_key})")
        clients.append(client)
        return client

    monkeypatch.setattr(server_module, "get_ayon_api", fake_get_ayon_api)
    mcp = FastMCP("tunnel-test")
    mcp.add_middleware(ServiceUserMiddleware("http://ayon", "service-key"))

    @mcp.tool
    async def whoami(ctx: Context) -> dict:
        """Report who the service acts as and what it received."""
        await ctx.report_progress(1, 2, "halfway")
        headers = get_http_headers(include_all=True)
        return {
            "api_key": get_global_ayon_api_key(),
            "as_user": get_global_ayon_as_user(),
            "client": get_global_ayon_client() is clients[0],
            "caller_api_key": headers.get("x-api-key"),
            "cookie": headers.get("cookie"),
        }

    app = mcp.http_app(path="/mcp", stateless_http=True)
    progress: list[float] = []

    async def on_progress(value: float, total, message) -> None:
        progress.append(value)

    async with running_server(FakeAyonServer()) as fake:
        url = f"ws://127.0.0.1:{fake.port}/ws"
        async with running_task(serve_forever(app, url, "service-key")):
            await fake.wait_for_tunnels()
            transport = StreamableHttpTransport(
                f"{fake.url}/mcp",
                headers={
                    "x-test-user": "alice",
                    "x-api-key": "alices-own-key",
                    "cookie": "accessToken=must-not-leak",
                    # A client must not be able to pick the user.
                    USER_HEADER: "admin",
                },
            )
            client = Client(transport, progress_handler=on_progress)
            async with client:
                tools = await client.list_tools()
                result = await client.call_tool("whoami", {})
                await client.call_tool("whoami", {})

    assert [tool.name for tool in tools] == ["whoami"]
    assert result.data == {
        "api_key": "service-key",
        "as_user": "alice",
        "client": True,
        "caller_api_key": None,
        "cookie": None,
    }
    assert progress == [1, 1]
    # One cached client per user, set to act as that user.
    assert len(clients) == 1
    clients[0].set_default_service_username.assert_called_once_with("alice")


async def _streaming_app(scope, receive, send) -> None:
    """ASGI app streaming three chunks until told to stop."""
    request = Request(scope, receive)
    body = await request.body()
    state = _streaming_app.state

    async def events():
        try:
            for index in range(3):
                yield f"chunk {index} {body.decode()}\n".encode()
                await state["release"].wait()
                state["release"].clear()
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"].set()
            raise

    await StreamingResponse(events(), media_type="text/plain")(
        scope, receive, send
    )


@pytest.mark.asyncio
async def test_response_is_streamed_and_cancelled_on_disconnect() -> None:
    _streaming_app.state = {
        "release": asyncio.Event(),
        "cancelled": asyncio.Event(),
    }
    state = _streaming_app.state

    async with running_server(FakeAyonServer()) as fake:
        url = f"ws://127.0.0.1:{fake.port}/ws"
        client = TunnelClient(_streaming_app, url, "service-key")
        async with running_task(client.run()):
            await fake.wait_for_tunnels()
            async with httpx.AsyncClient() as http:
                async with http.stream(
                    "POST", f"{fake.url}/mcp", content=b"hi"
                ) as response:
                    assert response.status_code == 200
                    assert response.headers["x-accel-buffering"] == "no"
                    lines = response.aiter_lines()
                    # Each chunk arrives before the app produces the next
                    # one - nothing is buffered until the end.
                    assert await anext(lines) == "chunk 0 hi"
                    state["release"].set()
                    assert await anext(lines) == "chunk 1 hi"
            # Closing the response mid-stream must cancel the request
            # in the service.
            await asyncio.wait_for(state["cancelled"].wait(), timeout=5)


@pytest.mark.asyncio
async def test_service_with_wrong_key_is_rejected() -> None:
    async with running_server(FakeAyonServer()) as fake:
        url = f"ws://127.0.0.1:{fake.port}/ws"
        client = TunnelClient(_streaming_app, url, "wrong-key")
        async with running_task(client.run()):
            await asyncio.sleep(0.3)
            assert len(fake.registry) == 0


@pytest.mark.asyncio
async def test_service_user_middleware_requires_user_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "get_http_headers", lambda **_: {})
    middleware = ServiceUserMiddleware("http://ayon", "service-key")

    async def call_next(context):
        raise AssertionError("tool must not run")

    with pytest.raises(RuntimeError, match=USER_HEADER):
        await middleware.on_call_tool(MagicMock(), call_next)


def test_service_user_clients_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server_module, "get_ayon_api", lambda *_: MagicMock()
    )
    middleware = ServiceUserMiddleware(
        "http://ayon", "service-key", max_clients=2
    )

    alice = middleware._client_for("alice")
    middleware._client_for("bob")
    assert middleware._client_for("alice") is alice
    middleware._client_for("carol")  # evicts bob, used least recently

    assert list(middleware._clients) == ["alice", "carol"]


@pytest.mark.asyncio
async def test_rest_client_sends_as_user() -> None:
    from ayon_mcp.client import (
        set_global_ayon_api_key,
        set_global_ayon_as_user,
    )
    from ayon_mcp.rest_client import RestApiClient

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={})

    client = RestApiClient("http://ayon")
    client.http_client = httpx.AsyncClient(
        base_url="http://ayon", transport=httpx.MockTransport(handler)
    )
    set_global_ayon_api_key("service-key")
    set_global_ayon_as_user("alice")
    try:
        await client.request("GET", "/api/users/me")
    finally:
        set_global_ayon_as_user(None)
        await client.close()

    assert seen["x-api-key"] == "service-key"
    assert seen["x-as-user"] == "alice"


@pytest.fixture
def fast_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel_module, "MIN_RECONNECT_DELAY", 0.01)
    monkeypatch.setattr(tunnel_module, "MAX_RECONNECT_DELAY", 0.01)
    monkeypatch.setattr(tunnel_module, "REJECTION_WINDOW", 0.3)


def _tunnel_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "ayon_mcp.tunnel" and record.levelname == "ERROR"
    ]


async def _run_rejected(client: TunnelClient, seconds: float = 1.5) -> None:
    async with running_task(client.run()):
        await asyncio.sleep(seconds)


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_reconnect")
async def test_rejected_key_is_logged_once_with_hint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with running_server(FakeAyonServer()) as fake:
        client = TunnelClient(
            _streaming_app, f"ws://127.0.0.1:{fake.port}/ws", "wrong-key"
        )
        await _run_rejected(client)
        assert len(fake.registry) == 0

    # Retried several times, reported once.
    assert _tunnel_errors(caplog) == [
        "AYON server rejected the MCP tunnel: invalid API key. "
        "Check AYON_API_KEY."
    ]
    assert not any(
        "MCP tunnel connected" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_reconnect")
async def test_protocol_mismatch_is_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(tunnel_module, "PROTOCOL_VERSION", 99)
    async with running_server(FakeAyonServer()) as fake:
        client = TunnelClient(
            _streaming_app, f"ws://127.0.0.1:{fake.port}/ws", "service-key"
        )
        await _run_rejected(client)
        assert len(fake.registry) == 0

    [error] = _tunnel_errors(caplog)
    assert "Unsupported tunnel protocol '99'" in error
    assert "same tunnel protocol" in error


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_reconnect")
async def test_server_without_tunnel_endpoint_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with running_server(FakeAyonServer()) as fake:
        client = TunnelClient(
            _streaming_app,
            f"ws://127.0.0.1:{fake.port}/no-such-endpoint",
            "service-key",
        )
        await _run_rejected(client, seconds=0.5)

    [error] = _tunnel_errors(caplog)
    assert "no MCP tunnel endpoint (/api/mcp)" in error
    assert "AYON_MCP_TRANSPORT=http" in error
