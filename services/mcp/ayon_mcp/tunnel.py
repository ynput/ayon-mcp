"""Serve the MCP HTTP app through a WebSocket tunnel to the AYON server.

Instead of listening on a port, the service connects out to the MCP addon
on the AYON server (``/api/addons/mcp/{version}/ws``) and runs each
tunneled HTTP request through the in-process ASGI app - the same app
uvicorn would serve. See ``tunnel_protocol`` for the wire format.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .tunnel_protocol import (
    INITIAL_WINDOW,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    Frame,
    FrameType,
    chunks,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping, MutableMapping

    from starlette.applications import Starlette
    from starlette.types import ASGIApp, Message
    from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

TUNNEL_URL_ENV = "AYON_MCP_TUNNEL_URL"
TRANSPORT_ENV = "AYON_MCP_TRANSPORT"
TRANSPORTS = frozenset({"http", "tunnel"})
# Name of the server addon serving the tunnel endpoint.
ADDON_NAME = "mcp"
PING_INTERVAL = 20.0
MIN_RECONNECT_DELAY = 1.0
MAX_RECONNECT_DELAY = 30.0
# A rejected tunnel is closed by the server right after it is accepted.
REJECTION_WINDOW = 1.0
# A response stream whose client takes nothing for this long (seconds)
# while the flow control window is used up is dropped.
CREDIT_TIMEOUT = 300.0
# Close codes the server sends when it rejects the tunnel (see
# ``server/mcp_tunnel/endpoint.py``), with what to do about them.
# Retrying won't help until the configuration changes, so these back off
# to the maximum delay.
REJECTION_HINTS = {
    4400: "Run the MCP service of the same version as the MCP addon, so "
    "both speak the same tunnel protocol.",
    4401: "Check AYON_API_KEY.",
    4403: "Set AYON_API_KEY to the API key of an AYON service user.",
}


def select_transport(environ: Mapping[str, str] | None = None) -> str:
    """Return how the dockerized service is reached: http or tunnel.

    ``AYON_MCP_TRANSPORT`` wins if set. Otherwise a service run by ASH
    (which sets ``AYON_ADDON_NAME`` and ``AYON_SERVICE_NAME``) uses the
    tunnel - it needs no port or ingress - and anything else listens on
    HTTP.

    Returns:
        ``"http"`` or ``"tunnel"``.

    Raises:
        RuntimeError: If ``AYON_MCP_TRANSPORT`` has an unknown value.

    """
    env = os.environ if environ is None else environ
    if explicit := env.get(TRANSPORT_ENV, "").strip().lower():
        if explicit not in TRANSPORTS:
            msg = (
                f"{TRANSPORT_ENV} must be 'http' or 'tunnel', "
                f"not {explicit!r}"
            )
            raise RuntimeError(msg)
        return explicit
    if env.get("AYON_ADDON_NAME") and env.get("AYON_SERVICE_NAME"):
        return "tunnel"
    return "http"


def tunnel_url(
    base_url: str, addon_version: str, addon_name: str = ADDON_NAME
) -> str:
    """Return the WebSocket URL of the MCP addon's tunnel endpoint.

    Returns:
        The ``ws://`` or ``wss://`` URL.

    """
    parts = urlsplit(base_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = f"{parts.path}/api/addons/{addon_name}/{addon_version}/ws"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


async def production_addon_version(
    base_url: str, api_key: str, addon_name: str = ADDON_NAME
) -> str:
    """Return the version of an addon in the production bundle.

    Returns:
        The addon version.

    Raises:
        RuntimeError: If there is no production bundle or the addon is
            not in it.

    """
    async with httpx.AsyncClient(base_url=base_url.rstrip("/")) as http:
        response = await http.get(
            "/api/bundles", headers={"x-api-key": api_key}
        )
        response.raise_for_status()
    for bundle in response.json().get("bundles", []):
        if bundle.get("isProduction"):
            if version := (bundle.get("addons") or {}).get(addon_name):
                return str(version)
            break
    msg = (
        f"The production bundle on {base_url} has no '{addon_name}' "
        f"addon, so there is no MCP tunnel endpoint to connect to. Add "
        f"the addon to the bundle, or set AYON_ADDON_VERSION."
    )
    raise RuntimeError(msg)


async def resolve_tunnel_url(base_url: str, api_key: str) -> str:
    """Return the tunnel URL of the MCP addon this service belongs to.

    In order: ``AYON_MCP_TUNNEL_URL``; the addon of ``AYON_ADDON_NAME``
    and ``AYON_ADDON_VERSION``, which ASH sets for the services it runs;
    the MCP addon of the production bundle.

    Returns:
        The ``ws://`` or ``wss://`` URL.

    """
    if explicit := os.getenv(TUNNEL_URL_ENV, "").strip():
        return explicit
    addon_name = os.getenv("AYON_ADDON_NAME", "").strip() or ADDON_NAME
    addon_version = os.getenv("AYON_ADDON_VERSION", "").strip()
    if not addon_version:
        addon_version = await production_addon_version(
            base_url, api_key, addon_name
        )
    return tunnel_url(base_url, addon_version, addon_name)


class _ClientStalledError(Exception):
    """The HTTP client stopped reading the response."""


class _Stream:
    """One tunneled HTTP request being served by the ASGI app."""

    def __init__(self, stream_id: uuid.UUID) -> None:
        self.id = stream_id
        self.body: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.body_done = False
        self.disconnected = asyncio.Event()
        self.response_started = False
        self.task: asyncio.Task[None] | None = None
        # Response body bytes the server accepts before granting more.
        self.window = INITIAL_WINDOW
        self.credit = asyncio.Event()

    async def wait_for_credit(self) -> None:
        """Wait until the server accepts more response body.

        Raises:
            _ClientStalledError: If no credit comes in ``CREDIT_TIMEOUT``.

        """
        try:
            async with asyncio.timeout(CREDIT_TIMEOUT):
                while self.window <= 0:
                    self.credit.clear()
                    await self.credit.wait()
        except TimeoutError as exc:
            raise _ClientStalledError from exc

    async def receive(self) -> Message:
        """ASGI ``receive``: the request body, then wait for disconnect.

        Returns:
            The next ASGI message.

        """
        if not self.body_done:
            chunk = await self.body.get()
            if chunk is not None:
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": True,
                }
            self.body_done = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.disconnected.wait()
        return {"type": "http.disconnect"}


class TunnelClient:
    """Connect to the AYON server and serve tunneled HTTP requests."""

    def __init__(
        self,
        app: ASGIApp,
        url: str,
        api_key: str,
        *,
        lifespan_state: Mapping[str, Any] | None = None,
    ) -> None:
        """Initialize the client.

        Args:
            app: The ASGI app serving requests (its lifespan must already
                be running, see ``serve_forever``).
            url: Tunnel WebSocket URL, see ``tunnel_url``.
            api_key: Service API key used to authenticate the tunnel.
            lifespan_state: State yielded by the app's lifespan, copied
                into every request scope like uvicorn does.

        """
        self._app = app
        self._url = url
        self._api_key = api_key
        self._lifespan_state = lifespan_state or {}
        self._streams: dict[uuid.UUID, _Stream] = {}
        self._send_lock = asyncio.Lock()
        self._websocket: ClientConnection | None = None
        self._last_problem: str | None = None
        self._established = False

    async def run(self) -> None:
        """Keep the tunnel connected, reconnecting with backoff."""
        delay = MIN_RECONNECT_DELAY
        while True:
            try:
                rejected = await self._connect_once()
            except InvalidStatus as exc:
                # The server refused the WebSocket outright. The addon
                # accepts it and closes with a reason instead, so this is
                # a server without this MCP addon version (or a proxy in
                # front of it refusing WebSockets).
                self._problem(
                    f"AYON server rejected the MCP tunnel at {self._url} "
                    f"with HTTP {exc.response.status_code}. The server "
                    "probably has no MCP tunnel endpoint there: install "
                    "the MCP addon of the version in the URL (see "
                    "AYON_ADDON_VERSION) or run with "
                    "AYON_MCP_TRANSPORT=http.",
                    kind=f"http-{exc.response.status_code}",
                )
                rejected = True
            except OSError as exc:
                self._problem(
                    f"MCP tunnel connection failed: {exc}", kind="network"
                )
                rejected = False
            finally:
                await self._drop_streams()
            if rejected:
                delay = MAX_RECONNECT_DELAY
            elif self._established:
                delay = MIN_RECONNECT_DELAY
            # Full jitter, so replicas restarting together don't
            # reconnect in lockstep.
            await asyncio.sleep(random.uniform(0, delay))  # noqa: S311
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _connect_once(self) -> bool:
        """Connect and serve until the tunnel closes.

        Returns:
            True if the server rejected the tunnel.

        """
        self._established = False
        try:
            async with connect(
                self._url,
                additional_headers={
                    "x-api-key": self._api_key,
                    PROTOCOL_HEADER: str(PROTOCOL_VERSION),
                },
                ping_interval=PING_INTERVAL,
            ) as websocket:
                await self._serve(websocket)
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            reason = exc.rcvd.reason if exc.rcvd else ""
        else:
            code, reason = websocket.close_code, websocket.close_reason
        if self._rejection(code, reason):
            return True
        if self._established:
            logger.warning("MCP tunnel closed (code %s)", code)
        return False

    def _problem(self, message: str, kind: str) -> None:
        """Log a connection problem; repeats of the same kind only at debug.

        ``kind`` identifies the problem (close code, HTTP status) - the
        message text may vary between attempts, e.g. AYON reports an
        invalid API key first and an invalid session afterwards.
        """
        if kind == self._last_problem:
            logger.debug(message)
            return
        self._last_problem = kind
        logger.error(message)

    def _rejection(self, code: int | None, reason: str | None) -> bool:
        """Log a rejection by the server.

        Returns:
            True if ``code`` means the server rejected the tunnel.

        """
        hint = REJECTION_HINTS.get(code or 0)
        if hint is None:
            return False
        self._problem(
            f"AYON server rejected the MCP tunnel: {reason or code}. {hint}",
            kind=f"close-{code}",
        )
        return True

    async def _serve(self, websocket: ClientConnection) -> None:
        # The server accepts every WebSocket and rejects bad ones by
        # closing right away. Only a tunnel that stays open counts as
        # connected - this keeps rejections from resetting the backoff
        # and from being logged as successful connections.
        # asyncio.wait, not wait_for: on Python 3.11 wait_for can swallow
        # a cancellation that arrives as the inner task finishes, which
        # would keep this reconnect loop running after cancel().
        closed = asyncio.ensure_future(websocket.wait_closed())
        done, _ = await asyncio.wait({closed}, timeout=REJECTION_WINDOW)
        if done:
            return
        closed.cancel()
        self._established = True
        self._last_problem = None
        logger.info("MCP tunnel connected to %s", self._url)
        self._websocket = websocket
        try:
            async for message in websocket:
                if isinstance(message, str):
                    logger.warning("Ignoring text message on MCP tunnel")
                    continue
                try:
                    frame = Frame.decode(message)
                except ValueError:
                    logger.warning("Ignoring malformed MCP tunnel frame")
                    continue
                await self._dispatch(frame)
        finally:
            self._websocket = None

    async def _dispatch(self, frame: Frame) -> None:
        if frame.type is FrameType.REQUEST_START:
            try:
                start = frame.json()
            except (TypeError, ValueError):
                await self._send(Frame.with_json(
                    FrameType.RESPONSE_ERROR,
                    frame.stream_id,
                    {"message": "malformed request start frame"},
                ))
                return
            stream = _Stream(frame.stream_id)
            self._streams[frame.stream_id] = stream
            stream.task = asyncio.create_task(self._handle(stream, start))
            return
        stream = self._streams.get(frame.stream_id)
        if stream is None:
            return
        if frame.type is FrameType.REQUEST_BODY:
            stream.body.put_nowait(frame.payload)
        elif frame.type is FrameType.REQUEST_END:
            stream.body.put_nowait(None)
        elif frame.type is FrameType.WINDOW:
            try:
                stream.window += frame.credit()
            except ValueError:
                logger.warning("Ignoring malformed MCP tunnel window frame")
                return
            stream.credit.set()
        elif frame.type is FrameType.CANCEL:
            stream.disconnected.set()
            if stream.task is not None:
                stream.task.cancel()

    async def _send(self, frame: Frame) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        async with self._send_lock:
            with contextlib.suppress(ConnectionClosed):
                await websocket.send(frame.encode())

    async def _drop_streams(self) -> None:
        streams = list(self._streams.values())
        self._streams.clear()
        for stream in streams:
            stream.disconnected.set()
            if stream.task is not None:
                stream.task.cancel()
        tasks = [s.task for s in streams if s.task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_message(self, stream: _Stream, message: Message) -> None:
        """ASGI ``send``: forward the response to the server as it comes."""
        if message["type"] == "http.response.start":
            stream.response_started = True
            headers = [
                (name.decode("latin-1"), value.decode("latin-1"))
                for name, value in message.get("headers", [])
            ]
            await self._send(Frame.with_json(
                FrameType.RESPONSE_START,
                stream.id,
                {"status": message["status"], "headers": headers},
            ))
        elif message["type"] == "http.response.body":
            for chunk in chunks(message.get("body", b"")):
                # Blocks only this request while its client is behind.
                await stream.wait_for_credit()
                stream.window -= len(chunk)
                await self._send(
                    Frame(FrameType.RESPONSE_BODY, stream.id, chunk)
                )
            if not message.get("more_body", False):
                await self._send(Frame(FrameType.RESPONSE_END, stream.id))

    async def _handle(self, stream: _Stream, start: dict[str, Any]) -> None:
        async def send(message: Message) -> None:
            await self._send_message(stream, message)

        try:
            await self._app(self._scope(start), stream.receive, send)
        except asyncio.CancelledError:
            # Cancelled by the server (client went away) or by a
            # reconnect - nobody is waiting for a response.
            pass
        except _ClientStalledError:
            logger.warning(
                "MCP client stopped reading the response for %.0f s, "
                "dropping the request",
                CREDIT_TIMEOUT,
            )
            await self._send(Frame(FrameType.RESPONSE_END, stream.id))
        except Exception as exc:
            logger.exception("MCP tunnel request failed")
            if stream.response_started:
                await self._send(Frame(FrameType.RESPONSE_END, stream.id))
            else:
                await self._send(Frame.with_json(
                    FrameType.RESPONSE_ERROR,
                    stream.id,
                    {"message": f"{type(exc).__name__}: {exc}"},
                ))
        finally:
            self._streams.pop(stream.id, None)

    def _scope(self, start: dict[str, Any]) -> MutableMapping[str, Any]:
        path = str(start.get("path") or "/")
        headers = [
            (str(name).lower().encode("latin-1"), str(value).encode("latin-1"))
            for name, value in start.get("headers", [])
        ]
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": str(start.get("method") or "GET").upper(),
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": str(start.get("query") or "").encode("latin-1"),
            "root_path": "",
            "headers": headers,
            "client": ("ayon-server", 0),
            "server": ("ayon-mcp-tunnel", 80),
            "state": dict(self._lifespan_state),
        }


async def serve_forever(app: Starlette, url: str, api_key: str) -> None:
    """Run the app's lifespan and keep the tunnel connected.

    The lifespan is entered here because nothing else runs it: FastMCP's
    streamable HTTP session manager fails without it.
    """
    async with app.router.lifespan_context(app) as state:
        client = TunnelClient(app, url, api_key, lifespan_state=state)
        await client.run()
