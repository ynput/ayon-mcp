"""Minimal stand-in for the AYON server's MCP tunnel endpoints.

The real implementation lives in ayon-backend (``ayon_server/mcp``) and
adds authentication and routing across server workers. This single-process
version speaks the same protocol, so the service side can be tested
end to end without an AYON server.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import uuid
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from ayon_mcp.tunnel_protocol import (
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    USER_HEADER,
    Frame,
    FrameType,
    chunks,
    filter_headers,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request

logger = logging.getLogger(__name__)

# How long to wait for the service to start answering a request.
RESPONSE_START_TIMEOUT = 60.0

# Close codes sent to a service whose tunnel is rejected.
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_UNSUPPORTED = 4400

# Caller credentials never reach the service; the server passes only the
# authenticated user name in USER_HEADER (and drops a client-sent one).
CREDENTIAL_HEADERS = frozenset({
    "authorization",
    "cookie",
    "x-api-key",
    "x-as-user",
    USER_HEADER,
})

# The service must not set cookies on the AYON server origin.
BLOCKED_RESPONSE_HEADERS = frozenset({"set-cookie"})


class TunnelClosedError(Exception):
    """The tunnel closed before the response was complete."""


class Tunnel:
    """One connected MCP service."""

    def __init__(self, websocket: WebSocket, user_name: str) -> None:
        """Initialize the tunnel.

        Args:
            websocket: Accepted WebSocket of the service.
            user_name: Service user that opened the tunnel.

        """
        self.websocket = websocket
        self.user_name = user_name
        self._streams: dict[uuid.UUID, asyncio.Queue[Frame | None]] = {}
        self._send_lock = asyncio.Lock()
        self._closed = False

    def open_stream(self) -> tuple[uuid.UUID, asyncio.Queue[Frame | None]]:
        """Register a new request stream.

        Returns:
            The stream id and the queue its response frames arrive on.
            ``None`` on the queue means the tunnel closed.

        Raises:
            TunnelClosedError: If the tunnel is already closed.

        """
        if self._closed:
            raise TunnelClosedError
        stream_id = uuid.uuid4()
        queue: asyncio.Queue[Frame | None] = asyncio.Queue()
        self._streams[stream_id] = queue
        return stream_id, queue

    def close_stream(self, stream_id: uuid.UUID) -> None:
        """Forget a finished request stream."""
        self._streams.pop(stream_id, None)

    async def send(self, frame: Frame) -> None:
        """Send a frame to the service.

        Raises:
            TunnelClosedError: If the tunnel is closed.

        """
        if self._closed:
            raise TunnelClosedError
        async with self._send_lock:
            try:
                await self.websocket.send_bytes(frame.encode())
            except (WebSocketDisconnect, RuntimeError) as exc:
                raise TunnelClosedError from exc

    async def receive_loop(self) -> None:
        """Route frames from the service to waiting requests until closed."""
        try:
            while True:
                message = await self.websocket.receive()
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
                queue = self._streams.get(frame.stream_id)
                if queue is not None:
                    queue.put_nowait(frame)
        finally:
            self._closed = True
            for queue in self._streams.values():
                queue.put_nowait(None)
            self._streams.clear()


class TunnelRegistry:
    """Connected tunnels of this process; requests are spread round-robin."""

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._tunnels: list[Tunnel] = []
        self._counter = itertools.count()

    def __len__(self) -> int:
        """Return the number of connected tunnels.

        Returns:
            Number of tunnels.

        """
        return len(self._tunnels)

    def add(self, tunnel: Tunnel) -> None:
        """Register a connected tunnel."""
        self._tunnels.append(tunnel)

    def remove(self, tunnel: Tunnel) -> None:
        """Unregister a tunnel."""
        with contextlib.suppress(ValueError):
            self._tunnels.remove(tunnel)

    def pick(self) -> Tunnel | None:
        """Return the tunnel for the next request.

        Returns:
            A tunnel, or ``None`` if no service is connected.

        """
        if not self._tunnels:
            return None
        return self._tunnels[next(self._counter) % len(self._tunnels)]


async def reject(websocket: WebSocket, code: int, reason: str) -> None:
    """Reject a tunnel the way the AYON server does: accept, then close."""
    await websocket.accept()
    await websocket.close(code=code, reason=reason)


async def serve_tunnel(
    websocket: WebSocket, registry: TunnelRegistry, user_name: str
) -> None:
    """Accept a service connection and serve it until it disconnects.

    The caller must have authenticated ``websocket`` as a service user.
    """
    version = websocket.headers.get(PROTOCOL_HEADER)
    if version != str(PROTOCOL_VERSION):
        logger.warning(
            "Rejecting MCP tunnel from %s: protocol %r, expected %s",
            user_name,
            version,
            PROTOCOL_VERSION,
        )
        await reject(
            websocket,
            CLOSE_UNSUPPORTED,
            f"Unsupported tunnel protocol {version!r}, "
            f"server supports {PROTOCOL_VERSION}",
        )
        return

    await websocket.accept()
    tunnel = Tunnel(websocket, user_name)
    registry.add(tunnel)
    logger.info(
        "MCP tunnel connected (%s, %d active)", user_name, len(registry)
    )
    try:
        await tunnel.receive_loop()
    finally:
        registry.remove(tunnel)
        logger.info(
            "MCP tunnel disconnected (%s, %d active)",
            user_name,
            len(registry),
        )


def _error(status: int, detail: str) -> Response:
    return JSONResponse({"detail": detail}, status_code=status)


def _unavailable() -> Response:
    response = _error(503, "MCP service is not connected")
    response.headers["Retry-After"] = "5"
    return response


class _ProxiedRequest:
    """One HTTP request forwarded through a tunnel."""

    def __init__(
        self,
        tunnel: Tunnel,
        stream_id: uuid.UUID,
        queue: asyncio.Queue[Frame | None],
    ) -> None:
        self.tunnel = tunnel
        self.stream_id = stream_id
        self.queue = queue
        self.finished = False

    async def forward(
        self, request: Request, *, path: str, user_name: str
    ) -> None:
        """Send the request line, headers and body to the service."""
        headers = filter_headers(
            list(request.headers.items()), drop=CREDENTIAL_HEADERS
        )
        headers.append((USER_HEADER, user_name))
        await self.tunnel.send(Frame.with_json(
            FrameType.REQUEST_START,
            self.stream_id,
            {
                "method": request.method,
                "path": path,
                "query": request.url.query,
                "headers": headers,
            },
        ))
        async for body in request.stream():
            for chunk in chunks(body):
                await self.tunnel.send(
                    Frame(FrameType.REQUEST_BODY, self.stream_id, chunk)
                )
        await self.tunnel.send(Frame(FrameType.REQUEST_END, self.stream_id))

    def close(self) -> None:
        """Stop routing frames to this request."""
        self.tunnel.close_stream(self.stream_id)

    async def cancel(self) -> None:
        """Close the stream and tell the service, unless it finished."""
        self.close()
        if not self.finished:
            with contextlib.suppress(TunnelClosedError):
                await self.tunnel.send(
                    Frame(FrameType.CANCEL, self.stream_id)
                )

    async def respond(self, first: Frame | None) -> Response:
        """Turn the service's first frame into the client response.

        Returns:
            A streaming response, or 502 if the service failed.

        """
        if first is None:
            self.close()
            return _error(502, "MCP service disconnected")
        if first.type is FrameType.RESPONSE_ERROR:
            self.close()
            message = first.json().get("message", "unknown error")
            return _error(502, f"MCP service error: {message}")
        if first.type is not FrameType.RESPONSE_START:
            await self.cancel()
            return _error(502, f"unexpected tunnel frame {first.type.name}")

        start = first.json()
        response = StreamingResponse(
            self._body(), status_code=int(start.get("status", 502))
        )
        for name, value in filter_headers(
            [tuple(header) for header in start.get("headers", [])],
            drop=BLOCKED_RESPONSE_HEADERS,
        ):
            response.headers.append(name, value)
        # Keep reverse proxies (nginx, ingress) from buffering SSE streams.
        response.headers["X-Accel-Buffering"] = "no"
        return response

    async def _body(self) -> AsyncIterator[bytes]:
        try:
            while True:
                frame = await self.queue.get()
                if frame is None or frame.type in {
                    FrameType.RESPONSE_END,
                    FrameType.RESPONSE_ERROR,
                }:
                    # The status is already sent; on error the client
                    # sees a truncated body.
                    self.finished = True
                    return
                if frame.type is FrameType.RESPONSE_BODY:
                    yield frame.payload
        finally:
            # Runs on normal completion and when the client disconnects
            # mid-stream; only the latter sends CANCEL.
            await self.cancel()


async def proxy_request(
    request: Request,
    registry: TunnelRegistry,
    *,
    path: str,
    user_name: str,
) -> Response:
    """Forward an HTTP request through a tunnel and stream the response.

    Args:
        request: Incoming request, already authenticated by the caller.
        registry: Connected tunnels.
        path: Path to request on the service (e.g. ``/mcp``).
        user_name: Authenticated AYON user the service acts as.

    Returns:
        The service's response, streamed, or 502/503/504 on tunnel errors.

    """
    tunnel = registry.pick()
    if tunnel is None:
        return _unavailable()
    try:
        stream_id, queue = tunnel.open_stream()
    except TunnelClosedError:
        return _unavailable()

    proxied = _ProxiedRequest(tunnel, stream_id, queue)
    try:
        await proxied.forward(request, path=path, user_name=user_name)
        first = await asyncio.wait_for(
            queue.get(), timeout=RESPONSE_START_TIMEOUT
        )
    except TunnelClosedError:
        proxied.close()
        return _error(502, "MCP service disconnected")
    except TimeoutError:
        await proxied.cancel()
        return _error(504, "MCP service did not respond")
    except BaseException:
        # Client went away while the request was being forwarded.
        await proxied.cancel()
        raise
    return await proxied.respond(first)
