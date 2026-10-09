"""Tests for the addon side of the MCP tunnel (``server/mcp_tunnel``).

Two fake server workers on real uvicorn ports share an in-memory bus
instead of Redis. A minimal fake MCP service connects over a real
WebSocket and answers with the request it received, so the tests cover
routing, streaming and cancellation without the MCP SDK.
"""

import asyncio
import contextlib
import json
import uuid
from typing import cast

import httpx
import pytest
from starlette.websockets import WebSocket
from websockets.asyncio.client import connect

import mcp_tunnel.hub as hub_module
from mcp_tunnel import TunnelHub
from mcp_tunnel.endpoint import close_reason
from mcp_tunnel.hub import (
    MAX_BUFFERED,
    OUTBOX_HIGH_WATER,
    OUTBOX_LIMIT,
    LocalTunnel,
    Route,
)
from mcp_tunnel.protocol import (
    MAX_CHUNK_SIZE,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    USER_HEADER,
    Frame,
    FrameType,
)

from fake_ayon_server import (
    SERVICE_KEY,
    FakeWorker,
    MemoryNetwork,
    MemoryTunnelBus,
    background,
    running,
    wait_until,
)


class FakeService:
    """Answers each request with a JSON echo of what it received.

    With ``stream=True`` it sends one line per ``release`` event instead,
    forever, so tests can check streaming and cancellation. With
    ``silent=True`` it never answers.
    """

    def __init__(
        self, url: str, *, stream: bool = False, silent: bool = False
    ) -> None:
        self.url = url
        self.stream = stream
        self.silent = silent
        self.release = asyncio.Event()
        self.cancelled: set[uuid.UUID] = set()
        self._requests: dict[uuid.UUID, dict] = {}
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        async with connect(
            self.url,
            additional_headers={
                "x-api-key": SERVICE_KEY,
                PROTOCOL_HEADER: str(PROTOCOL_VERSION),
            },
        ) as websocket:
            async for message in websocket:
                assert isinstance(message, bytes)
                frame = Frame.decode(message)
                if frame.type is FrameType.REQUEST_START:
                    self._requests[frame.stream_id] = {
                        **frame.json(),
                        "body": b"",
                    }
                elif frame.type is FrameType.REQUEST_BODY:
                    self._requests[frame.stream_id]["body"] += frame.payload
                elif frame.type is FrameType.REQUEST_END and not self.silent:
                    task = asyncio.create_task(
                        self._respond(websocket, frame.stream_id)
                    )
                    self._tasks.add(task)
                elif frame.type is FrameType.CANCEL:
                    self.cancelled.add(frame.stream_id)

    async def _respond(self, websocket, stream_id: uuid.UUID) -> None:
        request = self._requests.pop(stream_id)
        await websocket.send(
            Frame.with_json(
                FrameType.RESPONSE_START,
                stream_id,
                {
                    "status": 200,
                    "headers": [
                        ["content-type", "application/json"],
                        ["set-cookie", "evil=1"],
                        ["mcp-session-id", "abc"],
                    ],
                },
            ).encode()
        )
        if not self.stream:
            headers = dict(request["headers"])
            echo = {
                "method": request["method"],
                "path": request["path"],
                "query": request["query"],
                "user": headers.get(USER_HEADER),
                "api_key": headers.get("x-api-key"),
                "cookie": headers.get("cookie"),
                "body": request["body"].decode(),
            }
            await websocket.send(
                Frame(
                    FrameType.RESPONSE_BODY, stream_id, json.dumps(echo).encode()
                ).encode()
            )
            await websocket.send(Frame(FrameType.RESPONSE_END, stream_id).encode())
            return
        index = 0
        while stream_id not in self.cancelled:
            line = f"line {index}\n".encode()
            await websocket.send(
                Frame(FrameType.RESPONSE_BODY, stream_id, line).encode()
            )
            index += 1
            await self.release.wait()
            self.release.clear()


async def _echo_through(request_worker: int) -> httpx.Response:
    network = MemoryNetwork()
    workers = [FakeWorker(network), FakeWorker(network)]
    async with running(workers[0]), running(workers[1]):
        service = FakeService(workers[0].ws_url)
        async with background(service.run()):
            await workers[0].wait_for_tunnels()
            async with httpx.AsyncClient() as http:
                return await http.post(
                    workers[request_worker].url + "?x=1",
                    content=b'{"jsonrpc": "2.0"}',
                    headers={
                        "x-test-user": "alice",
                        "cookie": "accessToken=must-not-leak",
                        "x-api-key": "callers-own-key",
                        # A client must not be able to pick the user.
                        USER_HEADER: "admin",
                    },
                )


def _assert_echo(response: httpx.Response) -> None:
    assert response.status_code == 200
    assert response.json() == {
        "method": "POST",
        "path": "/mcp",
        "query": "x=1",
        "user": "alice",
        "api_key": None,
        "cookie": None,
        "body": '{"jsonrpc": "2.0"}',
    }
    assert response.headers["mcp-session-id"] == "abc"
    assert "set-cookie" not in response.headers
    assert response.headers["x-accel-buffering"] == "no"


def test_request_to_worker_holding_the_tunnel() -> None:
    _assert_echo(asyncio.run(_echo_through(0)))


def test_request_routed_to_tunnel_on_another_worker() -> None:
    _assert_echo(asyncio.run(_echo_through(1)))


def test_stale_tunnel_is_withdrawn() -> None:
    network = MemoryNetwork()
    # Listed, but no worker holds it (its worker crashed).
    network.tunnels.add("dead")

    async def run() -> httpx.Response:
        async with running(FakeWorker(network)) as worker:
            async with httpx.AsyncClient() as http:
                return await http.post(worker.url, content=b"{}")

    response = asyncio.run(run())

    assert response.status_code == 502
    assert network.tunnels == set()


def test_cross_worker_stream_is_incremental_and_cancelled() -> None:
    async def run() -> None:
        network = MemoryNetwork()
        workers = [FakeWorker(network), FakeWorker(network)]
        async with running(workers[0]), running(workers[1]):
            service = FakeService(workers[0].ws_url, stream=True)
            async with background(service.run()):
                await workers[0].wait_for_tunnels()
                async with httpx.AsyncClient() as http:
                    async with http.stream(
                        "POST", workers[1].url, content=b"{}"
                    ) as response:
                        assert response.status_code == 200
                        lines = response.aiter_lines()
                        # Each line arrives before the service produces
                        # the next one - nothing is buffered.
                        assert await anext(lines) == "line 0"
                        service.release.set()
                        assert await anext(lines) == "line 1"
                # Closing the response mid-stream reaches the service.
                await wait_until(lambda: len(service.cancelled) == 1)
                service.release.set()

    asyncio.run(run())


def test_service_disconnect_fails_waiting_remote_request() -> None:
    async def run() -> httpx.Response:
        network = MemoryNetwork()
        workers = [FakeWorker(network), FakeWorker(network)]
        async with running(workers[0]), running(workers[1]):
            service = FakeService(workers[0].ws_url)
            # Never answer: drop the connection once the request arrives.
            service._respond = lambda *_: asyncio.sleep(3600)  # type: ignore
            async with background(service.run()) as task:
                await workers[0].wait_for_tunnels()
                async with httpx.AsyncClient() as http:
                    pending = asyncio.create_task(
                        http.post(workers[1].url, content=b"{}")
                    )
                    await wait_until(lambda: bool(workers[0].hub._forwarded))
                    task.cancel()
                    return await asyncio.wait_for(pending, timeout=5)

    response = asyncio.run(run())

    assert response.status_code == 502
    assert response.json()["detail"] == "MCP service error: MCP service disconnected"


def test_remote_stream_ends_when_tunnel_worker_dies(monkeypatch) -> None:
    monkeypatch.setattr(hub_module, "LIVENESS_INTERVAL", 0.2)

    async def run() -> list[str]:
        network = MemoryNetwork()
        workers = [FakeWorker(network), FakeWorker(network)]
        async with running(workers[0]), running(workers[1]):
            service = FakeService(workers[0].ws_url, stream=True)
            async with background(service.run()):
                await workers[0].wait_for_tunnels()
                async with httpx.AsyncClient() as http:
                    async with http.stream(
                        "POST", workers[1].url, content=b"{}"
                    ) as response:
                        lines = response.aiter_lines()
                        received = [await anext(lines)]
                        # Worker 0 "crashes": it stops talking over the bus
                        # and its tunnel listing expires.
                        await workers[0].hub.bus.stop()
                        network.tunnels.clear()
                        # Without the liveness check this would hang.
                        async for line in lines:
                            received.append(line)
                        return received

    assert asyncio.run(asyncio.wait_for(run(), timeout=10)) == ["line 0"]


def test_service_cancelled_when_requesting_worker_dies() -> None:
    async def run() -> set[uuid.UUID]:
        network = MemoryNetwork()
        workers = [FakeWorker(network), FakeWorker(network)]
        async with running(workers[0]), running(workers[1]):
            service = FakeService(workers[0].ws_url, stream=True)
            async with background(service.run()):
                await workers[0].wait_for_tunnels()
                async with httpx.AsyncClient() as http:
                    async with http.stream(
                        "POST", workers[1].url, content=b"{}"
                    ) as response:
                        assert await anext(response.aiter_lines()) == "line 0"
                        # Worker 1 "crashes" while the service is streaming.
                        await workers[1].hub.bus.stop()
                        service.release.set()
                        # The next line finds nobody listening, so worker 0
                        # cancels the request in the service.
                        await wait_until(lambda: bool(service.cancelled))
                        service.release.set()
                        return service.cancelled

    assert len(asyncio.run(run())) == 1


# --- flow control and backpressure ------------------------------------------


@pytest.mark.asyncio
async def test_route_drops_stream_of_service_overrunning_its_window() -> None:
    worker = FakeWorker()
    tunnel = LocalTunnel(cast(WebSocket, _BlockedWebSocket()), "service")
    route = Route(worker.hub, tunnel.id, tunnel)
    chunk = b"x" * 1024

    # A service ignoring flow control keeps sending...
    for _ in range(MAX_BUFFERED // len(chunk) + 1):
        route.deliver(Frame(FrameType.RESPONSE_BODY, route.stream_id, chunk))
    route.deliver(Frame(FrameType.RESPONSE_BODY, route.stream_id, chunk))

    # ...and the request sees what fit in the window, then the end.
    frames = []
    while (frame := await route.receive()) is not None:
        frames.append(frame)
    assert sum(len(f.payload) for f in frames) <= MAX_BUFFERED
    assert route._queue.empty()


class _BlockedWebSocket:
    """WebSocket of a service that stopped reading."""

    def __init__(self) -> None:
        self.sent = 0

    async def send_bytes(self, data: bytes) -> None:
        self.sent += 1
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_stuck_service_does_not_block_the_bus() -> None:
    websocket = _BlockedWebSocket()
    tunnel = LocalTunnel(cast(WebSocket, websocket), "service")
    tunnel.start()
    frame = Frame(FrameType.REQUEST_BODY, uuid.uuid4(), b"x")
    try:
        # Frames from the bus are queued without waiting...
        while tunnel.queued_bytes < OUTBOX_HIGH_WATER * 2:
            tunnel.send_nowait(Frame(
                FrameType.REQUEST_BODY, frame.stream_id, b"x" * MAX_CHUNK_SIZE
            ))
        # ...while requests of this worker wait for the service.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(tunnel.send(frame), timeout=0.2)
    finally:
        await tunnel.close()
    assert websocket.sent == 1


class _FloodingService(FakeService):
    """Ignores flow control: sends body chunks until cancelled."""

    async def _respond(self, websocket, stream_id: uuid.UUID) -> None:
        self._requests.pop(stream_id)
        await websocket.send(
            Frame.with_json(
                FrameType.RESPONSE_START, stream_id, {"status": 200, "headers": []}
            ).encode()
        )
        chunk = Frame(FrameType.RESPONSE_BODY, stream_id, b"x" * 65536).encode()
        while stream_id not in self.cancelled:
            await websocket.send(chunk)
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_service_overrunning_its_window_is_cancelled() -> None:
    async with running(FakeWorker()) as worker:
        service = _FloodingService(worker.ws_url)
        async with background(service.run()):
            await worker.wait_for_tunnels()
            async with httpx.AsyncClient() as http:
                async with http.stream("POST", worker.url) as response:
                    assert response.status_code == 200
                    # The client reads nothing; the service is stopped
                    # anyway once it sends more than its window.
                    await wait_until(lambda: bool(service.cancelled))


# --- review fixes -------------------------------------------------------------


def test_token_query_parameter_is_not_forwarded() -> None:
    async def run() -> httpx.Response:
        async with running(FakeWorker()) as worker:
            service = FakeService(worker.ws_url)
            async with background(service.run()):
                await worker.wait_for_tunnels()
                async with httpx.AsyncClient() as http:
                    # AYON accepts an access token in ?token=...
                    return await http.post(
                        worker.url + "?x=1&token=secret&y=a%20b&flag&t%6Fken=2",
                        content=b"{}",
                    )

    response = asyncio.run(run())

    assert response.status_code == 200
    # Everything else is passed on as sent.
    assert response.json()["query"] == "x=1&y=a%20b&flag"


def test_close_reason_fits_control_frame() -> None:
    reason = close_reason("\u00e9" * 100)

    assert len(reason.encode()) <= 123
    assert reason == "\u00e9" * 61  # no half character at the end


def test_cross_worker_cancel_survives_slow_bus() -> None:
    """The client goes away while publishing to Redis takes time."""

    async def run() -> None:
        network = MemoryNetwork()
        workers = [FakeWorker(network), FakeWorker(network)]
        async with running(workers[0]), running(workers[1]):
            service = FakeService(workers[0].ws_url, stream=True)
            async with background(service.run()):
                await workers[0].wait_for_tunnels()
                async with httpx.AsyncClient() as http:
                    async with http.stream(
                        "POST", workers[1].url, content=b"{}"
                    ) as response:
                        assert await anext(response.aiter_lines()) == "line 0"
                        network.publish_delay = 0.05
                await wait_until(lambda: len(service.cancelled) == 1)
                service.release.set()

    asyncio.run(run())


class _FailingBus(MemoryTunnelBus):
    """Fails the first ``fail`` calls of ``method``."""

    def __init__(self, network: MemoryNetwork, method: str, fail: int = 1):
        super().__init__(network)
        self.method = method
        self.fail = fail
        self.starts = 0
        self.stops = 0

    async def _maybe_fail(self, method: str) -> None:
        if method == self.method and self.fail:
            self.fail -= 1
            raise ConnectionError(f"{method} failed")

    async def start(self, handler) -> None:
        self.starts += 1
        await super().start(handler)

    async def stop(self) -> None:
        self.stops += 1
        await super().stop()

    async def subscribe(self, channel: str) -> None:
        await self._maybe_fail("subscribe")
        await super().subscribe(channel)

    async def announce(self, tunnel_id: str) -> None:
        await self._maybe_fail("announce")
        await super().announce(tunnel_id)


@pytest.mark.asyncio
async def test_failed_start_does_not_leak_the_bus() -> None:
    bus = _FailingBus(MemoryNetwork(), "subscribe")
    hub = TunnelHub(bus)

    with pytest.raises(ConnectionError):
        await hub.ensure_started()
    first_reader = bus.task
    await asyncio.sleep(0)  # let the cancelled reader finish
    await hub.ensure_started()

    assert (bus.starts, bus.stops) == (2, 1)
    assert first_reader is not None and first_reader.cancelled()
    await hub.shutdown()


@pytest.mark.asyncio
async def test_failed_tunnel_setup_is_cleaned_up() -> None:
    network = MemoryNetwork()
    worker = FakeWorker(network)
    worker.hub = TunnelHub(_FailingBus(network, "announce"))
    async with running(worker):
        with contextlib.suppress(Exception):
            await FakeService(worker.ws_url).run()
        # The failed tunnel is not picked for requests...
        await wait_until(lambda: worker.hub.local_count == 0)
        assert network.tunnels == set()
        # ...and the service can connect again.
        service = FakeService(worker.ws_url)
        async with background(service.run()):
            await worker.wait_for_tunnels()
            async with httpx.AsyncClient() as http:
                response = await http.post(worker.url, content=b"{}")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_bus_upload_to_stuck_service_is_bounded() -> None:
    """Request bodies from other workers can't wait, so they fail."""
    network = MemoryNetwork()
    hub = TunnelHub(MemoryTunnelBus(network))
    await hub.ensure_started()
    websocket = _BlockedWebSocket()
    tunnel = LocalTunnel(cast(WebSocket, websocket), "service")
    tunnel.start()
    hub._local[tunnel.id] = tunnel
    origin = uuid.uuid4()
    replies = MemoryTunnelBus(network)
    await replies.subscribe(f"worker:{origin.hex}")
    stream_id = uuid.uuid4()
    channel = f"tunnel:{tunnel.id}"

    def message(frame: Frame) -> bytes:
        return origin.bytes + frame.encode()

    try:
        await hub._on_bus_message(channel, message(Frame.with_json(
            FrameType.REQUEST_START, stream_id, {"method": "POST"}
        )))
        chunk = b"x" * MAX_CHUNK_SIZE
        for _ in range(OUTBOX_LIMIT // MAX_CHUNK_SIZE * 2):
            await hub._on_bus_message(channel, message(
                Frame(FrameType.REQUEST_BODY, stream_id, chunk)
            ))

        assert tunnel.queued_bytes <= OUTBOX_LIMIT + MAX_CHUNK_SIZE
        _, reply = replies.queue.get_nowait()
        error = Frame.decode(reply)
        assert error.type is FrameType.RESPONSE_ERROR
        assert "not keeping up" in error.json()["message"]
        assert stream_id not in hub._forwarded
    finally:
        await tunnel.close()
        await hub.shutdown()


def test_client_leaving_before_the_response_cancels_the_request() -> None:
    """The client gives up while the service has not answered yet."""

    async def run() -> None:
        async with running(FakeWorker()) as worker:
            service = FakeService(worker.ws_url, silent=True)
            async with background(service.run()):
                await worker.wait_for_tunnels()
                async with httpx.AsyncClient(timeout=0.2) as http:
                    with pytest.raises(httpx.ReadTimeout):
                        await http.post(worker.url, content=b"{}")
                # Well before the response-start timeout.
                await wait_until(lambda: len(service.cancelled) == 1)
                assert worker.hub.routes == {}

    asyncio.run(run())
