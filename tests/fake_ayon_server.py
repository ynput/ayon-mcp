"""AYON server workers serving the MCP addon's tunnel endpoints, minus AYON.

Uses the addon's real tunnel code (``server/mcp_tunnel``). Only what needs
an AYON server is replaced: Redis by an in-memory bus shared by the
workers, user authentication by a fixed service key and an
``x-test-user`` header.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from mcp_tunnel import (
    CLOSE_UNAUTHORIZED,
    TunnelHub,
    TunnelRejectedError,
    proxy_request,
    serve_tunnel,
)
from mcp_tunnel.bus import MessageHandler, TunnelBus

SERVICE_KEY = "service-key"


class MemoryNetwork:
    """Shared state of in-memory buses - the "Redis" of the tests."""

    def __init__(self) -> None:
        self.subscribers: dict[str, set[MemoryTunnelBus]] = defaultdict(set)
        self.tunnels: set[str] = set()


class MemoryTunnelBus(TunnelBus):
    def __init__(self, network: MemoryNetwork) -> None:
        self.network = network
        self.queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def start(self, handler: MessageHandler) -> None:
        async def read() -> None:
            while True:
                channel, message = await self.queue.get()
                await handler(channel, message)

        self.task = asyncio.create_task(read())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        for subscribers in self.network.subscribers.values():
            subscribers.discard(self)

    async def subscribe(self, channel: str) -> None:
        self.network.subscribers[channel].add(self)

    async def unsubscribe(self, channel: str) -> None:
        self.network.subscribers[channel].discard(self)

    async def publish(self, channel: str, message: bytes) -> int:
        subscribers = list(self.network.subscribers[channel])
        for bus in subscribers:
            bus.queue.put_nowait((channel, message))
        return len(subscribers)

    async def announce(self, tunnel_id: str) -> None:
        self.network.tunnels.add(tunnel_id)

    async def withdraw(self, tunnel_id: str) -> None:
        self.network.tunnels.discard(tunnel_id)

    async def tunnels(self) -> list[str]:
        return sorted(self.network.tunnels)


class FakeWorker:
    """One server worker with the MCP addon's endpoints.

    ``/ws`` and ``/mcp`` stand in for ``/api/addons/mcp/{version}/ws`` and
    ``/api/addons/mcp/{version}/mcp``.
    """

    def __init__(self, network: MemoryNetwork | None = None) -> None:
        self.network = network or MemoryNetwork()
        self.hub = TunnelHub(MemoryTunnelBus(self.network))
        self.app = Starlette(
            routes=[
                WebSocketRoute("/ws", self._ws),
                Route("/mcp", self._proxy, methods=["GET", "POST", "DELETE"]),
            ]
        )
        self.port = 0

    async def _authenticate(self, websocket: WebSocket) -> str:
        if websocket.headers.get("x-api-key") != SERVICE_KEY:
            raise TunnelRejectedError(CLOSE_UNAUTHORIZED, "invalid API key")
        return "service"

    async def _ws(self, websocket: WebSocket) -> None:
        await serve_tunnel(websocket, self.hub, self._authenticate)

    async def _proxy(self, request: Request) -> Response:
        # Stands in for AYON auth: the authenticated user's name.
        return await proxy_request(
            request, self.hub, request.headers.get("x-test-user", "nobody")
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    async def wait_for_tunnels(self, count: int = 1) -> None:
        await wait_until(lambda: self.hub.local_count >= count)


@contextlib.asynccontextmanager
async def running(worker: FakeWorker):
    """Serve ``worker`` on a free local port."""
    config = uvicorn.Config(
        worker.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    worker.port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield worker
    finally:
        await worker.hub.shutdown()
        server.should_exit = True
        await task


@contextlib.asynccontextmanager
async def background(coro):
    """Run ``coro`` as a task, cancelled on exit."""
    task = asyncio.create_task(coro)
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def wait_until(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")
