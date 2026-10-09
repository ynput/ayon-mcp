"""Tests for the HTTP-over-WebSocket tunnel between AYON and the service.

The end-to-end tests serve the addon's tunnel endpoints
(``fake_ayon_server``, the real ``server/mcp_tunnel`` code) on a real
uvicorn port, connect the service's ``TunnelClient`` to them and talk to
the proxied endpoint over HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.dependencies import get_http_headers
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from ayon_mcp import server as server_module
from ayon_mcp import tunnel as tunnel_module
from ayon_mcp import tunnel_protocol
from ayon_mcp.client import (
    get_global_ayon_api_key,
    get_global_ayon_as_user,
    get_global_ayon_client,
)
from ayon_mcp.server import ServiceUserMiddleware
from ayon_mcp.tunnel import (
    TunnelClient,
    production_addon_version,
    resolve_tunnel_url,
    select_transport,
    serve_forever,
    tunnel_url,
)
from ayon_mcp.tunnel_protocol import USER_HEADER, Frame, FrameType

from fake_ayon_server import (
    SERVICE_KEY,
    FakeWorker,
    MemoryNetwork,
    background,
    running,
    wait_until,
)

REPO_ROOT = Path(__file__).parent.parent


def test_protocol_copies_are_identical() -> None:
    service = REPO_ROOT / "services" / "mcp" / "ayon_mcp" / "tunnel_protocol.py"
    addon = REPO_ROOT / "server" / "mcp_tunnel" / "protocol.py"

    assert service.read_bytes() == addon.read_bytes(), (
        "server/mcp_tunnel/protocol.py must be a copy of "
        "services/mcp/ayon_mcp/tunnel_protocol.py"
    )


def test_frame_round_trip() -> None:
    stream_id = uuid.uuid4()
    frame = Frame.with_json(FrameType.RESPONSE_START, stream_id, {"a": 1})

    decoded = Frame.decode(frame.encode())

    assert decoded == frame
    assert decoded.json() == {"a": 1}


def test_window_frame_round_trip() -> None:
    frame = Frame.window(uuid.uuid4(), 123456)

    decoded = Frame.decode(frame.encode())

    assert decoded.type is FrameType.WINDOW
    assert decoded.credit() == 123456
    with pytest.raises(ValueError, match="WINDOW"):
        Frame(FrameType.WINDOW, uuid.uuid4(), b"x").credit()


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


def test_filter_headers_drops_headers_named_by_connection() -> None:
    headers = [
        ("Connection", "close, X-Hop"),
        ("connection", "x-other"),
        ("X-Hop", "1"),
        ("X-Other", "2"),
        ("X-Kept", "3"),
    ]

    assert tunnel_protocol.filter_headers(headers) == [("x-kept", "3")]


def test_tunnel_url_of_addon_version() -> None:
    assert (
        tunnel_url("https://ayon.example.com/", "1.2.3")
        == "wss://ayon.example.com/api/addons/mcp/1.2.3/ws"
    )
    assert (
        tunnel_url("http://server:5000", "0.1.0+dev", "other")
        == "ws://server:5000/api/addons/other/0.1.0+dev/ws"
    )


ASH_ENV = {"AYON_ADDON_NAME": "mcp", "AYON_SERVICE_NAME": "mcp"}


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, "http"),  # standalone container or local run
        (ASH_ENV, "tunnel"),  # run by ASH
        ({**ASH_ENV, "AYON_MCP_TRANSPORT": "http"}, "http"),  # opted out
        ({"AYON_MCP_TRANSPORT": " Tunnel "}, "tunnel"),
        ({"AYON_ADDON_NAME": "mcp"}, "http"),  # not a service
        ({**ASH_ENV, "AYON_MCP_TRANSPORT": ""}, "tunnel"),
    ],
)
def test_select_transport(environ: dict, expected: str) -> None:
    assert select_transport(environ) == expected


def test_select_transport_rejects_unknown() -> None:
    with pytest.raises(RuntimeError, match="AYON_MCP_TRANSPORT"):
        select_transport({"AYON_MCP_TRANSPORT": "stdio"})


@pytest.fixture
def no_addon_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("AYON_MCP_TUNNEL_URL", "AYON_ADDON_NAME", "AYON_ADDON_VERSION"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.asyncio
async def test_resolve_tunnel_url_explicit(
    no_addon_env: pytest.MonkeyPatch,
) -> None:
    no_addon_env.setenv("AYON_MCP_TUNNEL_URL", "ws://custom/ws")
    no_addon_env.setenv("AYON_ADDON_VERSION", "1.0.0")

    assert await resolve_tunnel_url("http://ignored", "key") == "ws://custom/ws"


@pytest.mark.asyncio
async def test_resolve_tunnel_url_from_ash_environment(
    no_addon_env: pytest.MonkeyPatch,
) -> None:
    # ASH sets these for the services it runs.
    no_addon_env.setenv("AYON_ADDON_NAME", "mcp")
    no_addon_env.setenv("AYON_ADDON_VERSION", "1.0.0")

    async def unexpected(*_):
        raise AssertionError("must not ask the server")

    no_addon_env.setattr(tunnel_module, "production_addon_version", unexpected)

    assert (
        await resolve_tunnel_url("http://server:5000", "key")
        == "ws://server:5000/api/addons/mcp/1.0.0/ws"
    )


@pytest.mark.asyncio
async def test_resolve_tunnel_url_from_production_bundle(
    no_addon_env: pytest.MonkeyPatch,
) -> None:
    async def bundles(request: Request) -> JSONResponse:
        assert request.headers["x-api-key"] == "key"
        return JSONResponse({
            "bundles": [
                {"name": "staging", "isProduction": False,
                 "addons": {"mcp": "2.0.0"}},
                {"name": "prod", "isProduction": True,
                 "addons": {"mcp": "1.5.0", "core": "1.0.0"}},
            ]
        })

    worker = FakeWorker()
    worker.app = Starlette(routes=[Route("/api/bundles", bundles)])
    async with running(worker):
        base_url = f"http://127.0.0.1:{worker.port}"
        url = await resolve_tunnel_url(base_url, "key")
        with pytest.raises(RuntimeError, match="no 'other' addon"):
            await production_addon_version(base_url, "key", "other")

    assert url == f"ws://127.0.0.1:{worker.port}/api/addons/mcp/1.5.0/ws"


# --- end-to-end ------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_returns_503_without_tunnel() -> None:
    async with running(FakeWorker()) as worker:
        async with httpx.AsyncClient() as http:
            response = await http.post(worker.url, json={})

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
    mcp.add_middleware(ServiceUserMiddleware("http://ayon", SERVICE_KEY))

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

    async with running(FakeWorker()) as worker:
        async with background(serve_forever(app, worker.ws_url, SERVICE_KEY)):
            await worker.wait_for_tunnels()
            transport = StreamableHttpTransport(
                worker.url,
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
        "api_key": SERVICE_KEY,
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

    async with running(FakeWorker()) as worker:
        client = TunnelClient(_streaming_app, worker.ws_url, SERVICE_KEY)
        async with background(client.run()):
            await worker.wait_for_tunnels()
            async with httpx.AsyncClient() as http:
                async with http.stream(
                    "POST", worker.url, content=b"hi"
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


class _FirehoseApp:
    """ASGI app streaming 64 KiB chunks forever, counting what it made.

    ``?small`` answers right away, to check other requests still work.
    """

    def __init__(self) -> None:
        self.produced = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["query_string"] == b"small":
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
            return
        await send({"type": "http.response.start", "status": 200,
                    "headers": []})
        chunk = b"x" * tunnel_protocol.MAX_CHUNK_SIZE
        while True:
            await send({"type": "http.response.body", "body": chunk,
                        "more_body": True})
            self.produced += len(chunk)
            # Like a real app, let the event loop run between chunks.
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_slow_client_holds_back_only_its_own_stream() -> None:
    app = _FirehoseApp()

    async with running(FakeWorker()) as worker:
        client = TunnelClient(app, worker.ws_url, SERVICE_KEY)
        async with background(client.run()):
            await worker.wait_for_tunnels()
            async with httpx.AsyncClient() as http:
                async with http.stream("POST", worker.url) as response:
                    assert response.status_code == 200
                    # The client reads nothing: the service stops once the
                    # window (plus the sockets' buffers) is full.
                    await wait_until(lambda: app.produced > 0)
                    await asyncio.sleep(0.5)
                    stalled = app.produced
                    await asyncio.sleep(0.5)
                    assert app.produced == stalled
                    assert stalled < 64 * tunnel_protocol.INITIAL_WINDOW

                    # Another request on the same tunnel goes through.
                    small = await http.post(worker.url + "?small")
                    assert small.content == b"ok"

                    # Reading resumes the stream.
                    received = 0
                    async for data in response.aiter_bytes():
                        received += len(data)
                        if received > 2 * stalled:
                            break
                    assert app.produced > stalled


@pytest.mark.asyncio
async def test_service_with_wrong_key_is_rejected() -> None:
    async with running(FakeWorker()) as worker:
        client = TunnelClient(_streaming_app, worker.ws_url, "wrong-key")
        async with background(client.run()):
            await asyncio.sleep(0.3)
            assert worker.hub.local_count == 0


@pytest.mark.asyncio
async def test_service_user_middleware_requires_user_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "get_http_headers", lambda **_: {})
    middleware = ServiceUserMiddleware("http://ayon", SERVICE_KEY)

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
        "http://ayon", SERVICE_KEY, max_clients=2
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
    set_global_ayon_api_key(SERVICE_KEY)
    set_global_ayon_as_user("alice")
    try:
        await client.request("GET", "/api/users/me")
    finally:
        set_global_ayon_as_user(None)
        await client.close()

    assert seen["x-api-key"] == SERVICE_KEY
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
    async with background(client.run()):
        await asyncio.sleep(seconds)


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_reconnect")
async def test_rejected_key_is_logged_once_with_hint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with running(FakeWorker()) as worker:
        client = TunnelClient(_streaming_app, worker.ws_url, "wrong-key")
        await _run_rejected(client)
        assert worker.hub.local_count == 0

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
    async with running(FakeWorker()) as worker:
        client = TunnelClient(_streaming_app, worker.ws_url, SERVICE_KEY)
        await _run_rejected(client)
        assert worker.hub.local_count == 0

    [error] = _tunnel_errors(caplog)
    assert "Unsupported tunnel protocol '99'" in error
    assert "same tunnel protocol" in error


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_reconnect")
async def test_server_without_tunnel_endpoint_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with running(FakeWorker()) as worker:
        client = TunnelClient(
            _streaming_app,
            worker.ws_url.replace("/ws", "/no-such-endpoint"),
            SERVICE_KEY,
        )
        await _run_rejected(client, seconds=0.5)

    [error] = _tunnel_errors(caplog)
    assert "no MCP tunnel endpoint there" in error
    assert "AYON_MCP_TRANSPORT=http" in error


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_reconnect")
@pytest.mark.parametrize("readyz", [True, False], ids=["ready", "no-readyz"])
async def test_ready_server_without_tunnel_endpoint_is_reported(
    caplog: pytest.LogCaptureFixture, readyz: bool
) -> None:
    # A ready server refusing the handshake, or one too old to tell
    # (no /readyz), still gets the error.
    async with running(FakeWorker(readyz=readyz)) as worker:
        client = TunnelClient(
            _streaming_app,
            worker.ws_url.replace("/ws", "/no-such-endpoint"),
            SERVICE_KEY,
            server_url=worker.base_url,
        )
        await _run_rejected(client, seconds=0.5)

    [error] = _tunnel_errors(caplog)
    assert "no MCP tunnel endpoint there" in error


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_reconnect")
async def test_starting_server_is_not_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="ayon_mcp.tunnel")
    async with running(FakeWorker()) as worker:
        worker.ready = False
        client = TunnelClient(
            _streaming_app,
            worker.ws_url,
            SERVICE_KEY,
            server_url=worker.base_url,
        )
        async with background(client.run()):
            await asyncio.sleep(0.5)
            assert worker.hub.local_count == 0
            worker.ready = True
            await worker.wait_for_tunnels()

    assert not _tunnel_errors(caplog)
    starting = [
        record
        for record in caplog.records
        if "probably starting up" in record.getMessage()
    ]
    # Retried several times, reported once.
    assert [record.levelname for record in starting] == ["INFO"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_reconnect")
async def test_server_going_away_after_connecting_is_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="ayon_mcp.tunnel")
    server = running(FakeWorker())
    worker = await server.__aenter__()
    client = TunnelClient(_streaming_app, worker.ws_url, SERVICE_KEY)
    async with background(client.run()):
        await wait_until(
            lambda: any(
                "MCP tunnel connected to" in record.getMessage()
                for record in caplog.records
            )
        )
        await server.__aexit__(None, None, None)
        await wait_until(
            lambda: any(
                "MCP tunnel connection failed" in record.getMessage()
                for record in caplog.records
            )
        )

    assert not _tunnel_errors(caplog)


@pytest.mark.asyncio
async def test_remote_stream_gets_credit_through_the_bus() -> None:
    """A response bigger than the window, tunnel on another worker."""
    size = 3 * tunnel_protocol.INITIAL_WINDOW + 5

    async def big_app(scope, receive, send) -> None:
        await StreamingResponse(
            iter([b"y" * size]), media_type="application/octet-stream"
        )(scope, receive, send)

    network = MemoryNetwork()
    workers = [FakeWorker(network), FakeWorker(network)]
    async with running(workers[0]), running(workers[1]):
        client = TunnelClient(big_app, workers[0].ws_url, SERVICE_KEY)
        async with background(client.run()):
            await workers[0].wait_for_tunnels()
            async with httpx.AsyncClient() as http:
                response = await asyncio.wait_for(
                    http.post(workers[1].url), timeout=10
                )

    assert response.status_code == 200
    assert len(response.content) == size


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_reconnect")
async def test_connection_closed_during_handshake_is_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """E.g. an ingress dropping the connection while the server restarts."""
    attempts = 0

    async def drop(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal attempts
        attempts += 1
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 101 Switching")  # cut off mid-response
        writer.close()

    server = await asyncio.start_server(drop, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        client = TunnelClient(
            _streaming_app, f"ws://127.0.0.1:{port}/ws", SERVICE_KEY
        )
        task = asyncio.create_task(client.run())
        try:
            await wait_until(lambda: attempts >= 3)
            assert not task.done()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    [error] = _tunnel_errors(caplog)
    assert "MCP tunnel connection failed" in error
