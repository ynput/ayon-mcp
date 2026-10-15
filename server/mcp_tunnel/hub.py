"""Per-worker hub of MCP service tunnels.

Each server worker has one ``TunnelHub`` per addon version. It holds the
tunnel WebSockets of services connected to this worker and routes
requests to tunnels:

- A request served by this worker goes straight to a local tunnel if there
  is one, otherwise through the bus to the worker holding a remote tunnel.
- Frames arriving from the bus for a local tunnel are forwarded to the
  service; the service's response frames go back to the requesting worker.

Messages on a tunnel channel are the 16-byte id of the requesting worker
followed by the encoded frame. Messages on a worker channel are frames.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import itertools
import random
import uuid
from typing import TYPE_CHECKING

from ._logging import logger
from .bus import TUNNEL_TTL, TunnelBus
from .protocol import INITIAL_WINDOW, MAX_CHUNK_SIZE, Frame, FrameType

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

END_FRAMES = frozenset({FrameType.RESPONSE_END, FrameType.RESPONSE_ERROR})

# A request waiting on a tunnel held by another worker checks this often
# (seconds without frames) that the tunnel is still listed. If that worker
# crashed, its listing expires after TUNNEL_TTL and the request ends
# instead of hanging.
LIVENESS_INTERVAL = 10.0

# Response body bytes a request may have buffered. The service sends at
# most INITIAL_WINDOW ahead of the client (plus the chunk it is sending
# when the window runs out), so more means a broken service.
MAX_BUFFERED = INITIAL_WINDOW + MAX_CHUNK_SIZE

# Bytes queued for a service before requests of this worker wait for the
# service to catch up.
OUTBOX_HIGH_WATER = 1024 * 1024

# Most bytes queued for a service. Requests of other workers arrive over
# the bus and can't wait - the bus reader must not block - so a request
# whose frames would go over this fails instead.
OUTBOX_LIMIT = 8 * 1024 * 1024

# Frames carrying request data, the ones ``OUTBOX_LIMIT`` applies to.
# Control frames (end, cancel, window) are tiny and always queued, so a
# request can still be cancelled when the service is behind.
DATA_FRAMES = frozenset({FrameType.REQUEST_START, FrameType.REQUEST_BODY})


class TunnelClosedError(Exception):
    """The tunnel closed before the request was complete."""


class TunnelBusyError(Exception):
    """The service is too far behind to take more request data."""


def _tunnel_channel(tunnel_id: str) -> str:
    return f"tunnel:{tunnel_id}"


def _worker_channel(worker_id: uuid.UUID) -> str:
    return f"worker:{worker_id.hex}"


class LocalTunnel:
    """Tunnel WebSocket of a service connected to this worker.

    Frames for the service are queued and written by one writer task, so
    a slow service never blocks the bus or other tunnels.
    """

    def __init__(self, websocket: WebSocket, user_name: str) -> None:
        """Initialize the tunnel.

        Args:
            websocket: Accepted WebSocket of the service.
            user_name: Service user that opened the tunnel.

        """
        self.id = uuid.uuid4().hex
        self.websocket = websocket
        self.user_name = user_name
        self.closed = False
        self._outbox: collections.deque[bytes] = collections.deque()
        self.queued_bytes = 0
        self._pending = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()
        self._writer: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start writing queued frames to the service."""
        self._writer = asyncio.create_task(self._write())

    async def close(self) -> None:
        """Stop the writer; frames still queued are dropped."""
        self.closed = True
        self._drained.set()
        if self._writer is not None:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer

    def send_nowait(self, frame: Frame, *, bounded: bool = False) -> None:
        """Queue a frame for the service.

        Args:
            frame: The frame.
            bounded: Refuse the frame if it would take the outbox over
                ``OUTBOX_LIMIT``.

        Raises:
            TunnelClosedError: If the tunnel is closed.
            TunnelBusyError: If ``bounded`` and the outbox is full.

        """
        if self.closed:
            raise TunnelClosedError
        data = frame.encode()
        if bounded and self.queued_bytes + len(data) > OUTBOX_LIMIT:
            raise TunnelBusyError
        self._outbox.append(data)
        self.queued_bytes += len(data)
        self._pending.set()
        if self.queued_bytes >= OUTBOX_HIGH_WATER:
            self._drained.clear()

    async def send(self, frame: Frame) -> None:
        """Queue a frame, waiting while the service is behind.

        Raises:
            TunnelClosedError: If the tunnel is closed.

        """
        self.send_nowait(frame)
        await self._drained.wait()
        if self.closed:
            raise TunnelClosedError

    async def _next(self) -> bytes:
        while not self._outbox:
            self._drained.set()
            self._pending.clear()
            await self._pending.wait()
        data = self._outbox.popleft()
        self.queued_bytes -= len(data)
        if self.queued_bytes < OUTBOX_HIGH_WATER // 2:
            self._drained.set()
        return data

    async def _write(self) -> None:
        while True:
            data = await self._next()
            try:
                await self.websocket.send_bytes(data)
            except Exception:  # ruff: ignore[blind-except]
                # The service is gone; the receive loop sees the
                # disconnect and fails the requests in flight.
                logger.debug(f"MCP tunnel {self.id} write failed")
                self.closed = True
                self._drained.set()
                return


class Route:
    """Path of one request from this worker to a tunnel and back."""

    def __init__(
        self,
        hub: TunnelHub,
        tunnel_id: str,
        local: LocalTunnel | None,
    ) -> None:
        """Initialize the route.

        Args:
            hub: Hub of this worker.
            tunnel_id: Tunnel serving the request.
            local: The tunnel, if this worker holds it.

        """
        self.stream_id = uuid.uuid4()
        self.tunnel_id = tunnel_id
        self._queue: asyncio.Queue[Frame | None] = asyncio.Queue()
        self._buffered = 0
        self._ended = False
        self._cancelling: asyncio.Task[None] | None = None
        self._hub = hub
        self._local = local

    def deliver(self, frame: Frame | None) -> None:
        """Queue a response frame; ``None`` means the tunnel closed.

        The queue is bounded by flow control. A service that sends more
        than its window gets the stream dropped instead of growing it.
        """
        if self._ended:
            return
        if frame is not None and frame.type is FrameType.RESPONSE_BODY:
            self._buffered += len(frame.payload)
            if self._buffered > MAX_BUFFERED:
                logger.warning(
                    f"MCP service overran the flow control window of "
                    f"stream {self.stream_id}, dropping the stream"
                )
                frame = None
                # Stop the service right away - the request only sees the
                # end once its client reads again.
                self._cancelling = asyncio.ensure_future(self._cancel())
        if frame is None:
            self._ended = True
        self._queue.put_nowait(frame)

    async def _cancel(self) -> None:
        with contextlib.suppress(Exception):
            await self.send(Frame(FrameType.CANCEL, self.stream_id))

    async def send(self, frame: Frame) -> None:
        """Send a request frame to the tunnel.

        Raises:
            TunnelClosedError: If the tunnel is gone.

        """
        if self._local is not None:
            await self._local.send(frame)
            return
        received = await self._hub.bus.publish(
            _tunnel_channel(self.tunnel_id),
            self._hub.worker_id.bytes + frame.encode(),
        )
        if not received:
            # Nobody holds the tunnel any more - its worker died without
            # withdrawing it. Unlist it so the next request skips it.
            with contextlib.suppress(Exception):
                await self._hub.bus.withdraw(self.tunnel_id)
            raise TunnelClosedError

    async def receive(self) -> Frame | None:
        """Wait for the next response frame.

        Returns:
            The frame, or ``None`` if the tunnel closed or - for a tunnel
            held by another worker - is no longer listed (that worker
            died).

        """
        while True:
            if self._local is not None:
                frame = await self._queue.get()
            else:
                try:
                    frame = await asyncio.wait_for(
                        self._queue.get(), LIVENESS_INTERVAL
                    )
                except TimeoutError:
                    if not await self._hub.is_listed(self.tunnel_id):
                        return None
                    continue
            if frame is not None and frame.type is FrameType.RESPONSE_BODY:
                self._buffered -= len(frame.payload)
            return frame

    def close(self) -> None:
        """Stop routing response frames to this request."""
        self._hub.routes.pop(self.stream_id, None)


class TunnelHub:
    """MCP service tunnels of this worker, connected to the other workers."""

    def __init__(self, bus: TunnelBus) -> None:
        """Initialize the hub.

        Args:
            bus: Bus shared with the hubs of the other workers.

        """
        self.worker_id = uuid.uuid4()
        self.bus = bus
        self.routes: dict[uuid.UUID, Route] = {}
        self._local: dict[str, LocalTunnel] = {}
        # Requests from other workers to local tunnels:
        # stream id -> (tunnel id, requesting worker id)
        self._forwarded: dict[uuid.UUID, tuple[str, uuid.UUID]] = {}
        self._counter = itertools.count()
        self._started = False
        self._start_lock = asyncio.Lock()

    @property
    def local_count(self) -> int:
        """Number of tunnels connected to this worker."""
        return len(self._local)

    async def ensure_started(self) -> None:
        """Start listening on the bus, once."""
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            await self.bus.start(self._on_bus_message)
            try:
                await self.bus.subscribe(_worker_channel(self.worker_id))
            except BaseException:
                # Stop the reader, or the next attempt starts a second one.
                with contextlib.suppress(Exception):
                    await self.bus.stop()
                raise
            self._started = True

    async def shutdown(self) -> None:
        """Stop listening on the bus."""
        if not self._started:
            return
        self._started = False
        await self.bus.stop()

    #
    # Requests
    #

    async def open_route(self) -> Route | None:
        """Pick a tunnel for a new request, preferring local ones.

        Returns:
            The route, or ``None`` if no service is connected to any
            worker.

        """
        await self.ensure_started()
        if self._local:
            local = list(self._local.values())
            tunnel = local[next(self._counter) % len(local)]
            route = Route(self, tunnel.id, tunnel)
        else:
            remote = await self.bus.tunnels()
            if not remote:
                return None
            route = Route(self, random.choice(remote), None)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
        self.routes[route.stream_id] = route
        return route

    async def is_listed(self, tunnel_id: str) -> bool:
        """Return whether a tunnel is listed; True if the bus can't tell.

        Returns:
            Whether the tunnel is listed.

        """
        try:
            return tunnel_id in await self.bus.tunnels()
        except Exception:  # ruff: ignore[blind-except]
            return True

    #
    # Service connections
    #

    async def serve(self, websocket: WebSocket, user_name: str) -> None:
        """Serve an accepted tunnel WebSocket until it disconnects."""
        await self.ensure_started()
        tunnel = LocalTunnel(websocket, user_name)
        heartbeat: asyncio.Task[None] | None = None
        try:
            # Inside the try, so a failed setup (e.g. Redis down) is
            # cleaned up too and the tunnel is never picked for requests.
            heartbeat = await self._attach(tunnel)
            logger.info(f"MCP tunnel connected ({user_name}, {tunnel.id})")
            await self._receive_loop(tunnel)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            await tunnel.close()
            self._local.pop(tunnel.id, None)
            with contextlib.suppress(Exception):
                await self.bus.withdraw(tunnel.id)
                await self.bus.unsubscribe(_tunnel_channel(tunnel.id))
            await self._fail_streams(tunnel.id)
            logger.info(
                f"MCP tunnel disconnected ({user_name}, {tunnel.id})"
            )

    async def _attach(self, tunnel: LocalTunnel) -> asyncio.Task[None]:
        """Make a tunnel available to all workers.

        Returns:
            The task keeping the tunnel listed.

        """
        tunnel.start()
        self._local[tunnel.id] = tunnel
        await self.bus.subscribe(_tunnel_channel(tunnel.id))
        await self.bus.announce(tunnel.id)
        return asyncio.create_task(self._heartbeat(tunnel.id))

    async def _heartbeat(self, tunnel_id: str) -> None:
        while True:
            await asyncio.sleep(TUNNEL_TTL / 3)
            with contextlib.suppress(Exception):
                await self.bus.announce(tunnel_id)

    async def _receive_loop(self, tunnel: LocalTunnel) -> None:
        while True:
            message = await tunnel.websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None:
                continue
            try:
                frame = Frame.decode(data)
            except ValueError:
                logger.warning("Ignoring malformed MCP tunnel frame")
                continue
            await self._from_service(tunnel.id, frame)

    async def _from_service(self, tunnel_id: str, frame: Frame) -> None:
        route = self.routes.get(frame.stream_id)
        if route is not None and route.tunnel_id == tunnel_id:
            route.deliver(frame)
            return
        forwarded = self._forwarded.get(frame.stream_id)
        if forwarded is None or forwarded[0] != tunnel_id:
            return
        if frame.type in END_FRAMES:
            self._forwarded.pop(frame.stream_id, None)
        received = await self.bus.publish(
            _worker_channel(forwarded[1]), frame.encode()
        )
        if not received and frame.type not in END_FRAMES:
            # The requesting worker is gone, so is its client - stop the
            # request in the service instead of streaming into the void.
            self._forwarded.pop(frame.stream_id, None)
            tunnel = self._local.get(tunnel_id)
            if tunnel is not None:
                with contextlib.suppress(TunnelClosedError):
                    tunnel.send_nowait(
                        Frame(FrameType.CANCEL, frame.stream_id)
                    )

    async def _fail_streams(self, tunnel_id: str) -> None:
        """End all requests still waiting on a closed tunnel."""
        for route in list(self.routes.values()):
            if route.tunnel_id == tunnel_id:
                route.deliver(None)
        for stream_id, (owner, origin) in list(self._forwarded.items()):
            if owner != tunnel_id:
                continue
            self._forwarded.pop(stream_id, None)
            await self._reply_error(
                origin, stream_id, "MCP service disconnected"
            )

    async def _reply_error(
        self, origin: uuid.UUID, stream_id: uuid.UUID, message: str
    ) -> None:
        frame = Frame.with_json(
            FrameType.RESPONSE_ERROR, stream_id, {"message": message}
        )
        with contextlib.suppress(Exception):
            await self.bus.publish(_worker_channel(origin), frame.encode())

    #
    # Bus
    #

    async def _on_bus_message(self, channel: str, data: bytes) -> None:
        # Called by the bus reader for every message of this worker, so
        # nothing here waits on a service or a client.
        if channel == _worker_channel(self.worker_id):
            frame = Frame.decode(data)
            route = self.routes.get(frame.stream_id)
            if route is not None:
                route.deliver(frame)
            return
        await self._to_service(
            channel.removeprefix("tunnel:"),
            uuid.UUID(bytes=data[:16]),
            Frame.decode(data[16:]),
        )

    async def _to_service(
        self, tunnel_id: str, origin: uuid.UUID, frame: Frame
    ) -> None:
        """Forward a frame from another worker to a local tunnel."""
        tunnel = self._local.get(tunnel_id)
        if tunnel is None:
            if frame.type is FrameType.REQUEST_START:
                await self._reply_error(
                    origin, frame.stream_id, "MCP service disconnected"
                )
            return
        if frame.type is FrameType.REQUEST_START:
            self._forwarded[frame.stream_id] = (tunnel_id, origin)
        elif frame.stream_id not in self._forwarded:
            # Finished or failed request - drop the rest of its frames.
            return
        elif frame.type is FrameType.CANCEL:
            self._forwarded.pop(frame.stream_id, None)
        try:
            tunnel.send_nowait(frame, bounded=frame.type in DATA_FRAMES)
        except TunnelBusyError:
            await self._fail_busy(tunnel, origin, frame.stream_id)
        except TunnelClosedError:
            if self._forwarded.pop(frame.stream_id, None) is not None:
                await self._reply_error(
                    origin, frame.stream_id, "MCP service disconnected"
                )

    async def _fail_busy(
        self, tunnel: LocalTunnel, origin: uuid.UUID, stream_id: uuid.UUID
    ) -> None:
        """Fail a request the service is too far behind to take."""
        logger.warning(
            f"MCP service of tunnel {tunnel.id} is not reading requests, "
            f"failing request {stream_id}"
        )
        self._forwarded.pop(stream_id, None)
        # The service may already have part of the request.
        with contextlib.suppress(TunnelClosedError):
            tunnel.send_nowait(Frame(FrameType.CANCEL, stream_id))
        await self._reply_error(
            origin, stream_id, "MCP service is not keeping up with requests"
        )
