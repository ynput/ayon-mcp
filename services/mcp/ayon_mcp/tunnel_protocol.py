"""Wire format of the HTTP-over-WebSocket tunnel between AYON and MCP service.

The MCP service opens a WebSocket to the MCP addon on the AYON server
(``/api/addons/mcp/{version}/ws``) and serves the HTTP requests the addon
receives on ``/api/addons/mcp/{version}/mcp``. The service needs no
listening port, public IP or ingress.

Every WebSocket message is one binary frame::

    1 byte frame type | 16 bytes stream id (UUID) | payload

One HTTP request is one stream. The server sends ``REQUEST_START``
(JSON: method, path, query, headers), any number of ``REQUEST_BODY``
chunks and ``REQUEST_END``. The service answers with ``RESPONSE_START``
(JSON: status, headers), ``RESPONSE_BODY`` chunks as the app produces them
and ``RESPONSE_END``, or ``RESPONSE_ERROR`` (JSON: message). The server
sends ``CANCEL`` when the HTTP client goes away.

Response bodies are flow controlled per stream: the service sends at most
``INITIAL_WINDOW`` bytes of ``RESPONSE_BODY`` payload ahead of what the
server has passed on to the HTTP client. The server grants more with
``WINDOW`` frames (4-byte unsigned credit in bytes) as the client reads,
so a slow client holds back only its own stream.

The tunnel is MCP-agnostic on purpose: it forwards plain HTTP, so changes
in the MCP transport never need a server release.

This module is stdlib-only and exists twice in ayon-mcp, byte for byte:
``services/mcp/ayon_mcp/tunnel_protocol.py`` (service) and
``server/mcp_tunnel/protocol.py`` (server addon), because the service
image and the addon package are deployed separately. A test checks that
the copies are identical. ``PROTOCOL_VERSION`` is checked when the tunnel
connects; bump it on incompatible changes.
"""

from __future__ import annotations

import enum
import json
import struct
import uuid
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 3
# Sent by the service when connecting, so the server can reject
# incompatible clients before any traffic.
PROTOCOL_HEADER = "x-ayon-mcp-tunnel"

# Set by the server to the name of the authenticated AYON user a request is
# made for. The service calls AYON with its own service API key and
# ``x-as-user: <this value>``, so the caller's permissions apply. The server
# drops this header from client requests, so it can't be spoofed through
# the tunnel.
USER_HEADER = "x-ayon-mcp-user"

# Request/response bodies are split into chunks of at most this size.
MAX_CHUNK_SIZE = 64 * 1024

# Response body bytes the service may send on a stream before it needs
# credit from the server (``WINDOW`` frames).
INITIAL_WINDOW = 1024 * 1024

_HEADER = struct.Struct("!B16s")
_CREDIT = struct.Struct("!I")

# Connection-level headers that must not be forwarded by a proxy
# (RFC 9110, section 7.6.1), plus ``host`` and ``content-length`` which
# the receiving side sets itself.
HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})


class FrameType(enum.IntEnum):
    """Type of a tunnel frame."""

    REQUEST_START = 1
    REQUEST_BODY = 2
    REQUEST_END = 3
    CANCEL = 4
    RESPONSE_START = 5
    RESPONSE_BODY = 6
    RESPONSE_END = 7
    RESPONSE_ERROR = 8
    WINDOW = 9


@dataclass(frozen=True)
class Frame:
    """One tunnel frame."""

    type: FrameType
    stream_id: uuid.UUID
    payload: bytes = b""

    @classmethod
    def with_json(
        cls, frame_type: FrameType, stream_id: uuid.UUID, data: dict[str, Any]
    ) -> Frame:
        """Create a frame with a JSON payload.

        Returns:
            The frame.

        """
        return cls(frame_type, stream_id, json.dumps(data).encode("utf-8"))

    @classmethod
    def decode(cls, data: bytes) -> Frame:
        """Parse a frame received from the WebSocket.

        Returns:
            The frame.

        Raises:
            ValueError: If the data is not a valid frame.

        """
        if len(data) < _HEADER.size:
            msg = f"tunnel frame too short ({len(data)} bytes)"
            raise ValueError(msg)
        frame_type, stream_id = _HEADER.unpack_from(data)
        return cls(
            FrameType(frame_type),
            uuid.UUID(bytes=stream_id),
            data[_HEADER.size:],
        )

    @classmethod
    def window(cls, stream_id: uuid.UUID, credit: int) -> Frame:
        """Create a ``WINDOW`` frame granting ``credit`` more body bytes.

        Returns:
            The frame.

        """
        return cls(FrameType.WINDOW, stream_id, _CREDIT.pack(credit))

    def credit(self) -> int:
        """Return the credit of a ``WINDOW`` frame.

        Returns:
            Number of response body bytes granted.

        Raises:
            ValueError: If the payload is not a credit.

        """
        if len(self.payload) != _CREDIT.size:
            msg = f"bad WINDOW payload ({len(self.payload)} bytes)"
            raise ValueError(msg)
        return _CREDIT.unpack(self.payload)[0]

    def encode(self) -> bytes:
        """Serialize the frame for sending over the WebSocket.

        Returns:
            The frame bytes.

        """
        return _HEADER.pack(self.type, self.stream_id.bytes) + self.payload

    def json(self) -> dict[str, Any]:
        """Parse the JSON payload.

        Returns:
            The decoded payload.

        Raises:
            TypeError: If the payload is not a JSON object.

        """
        data = json.loads(self.payload)
        if not isinstance(data, dict):
            msg = f"expected a JSON object in {self.type.name} frame"
            raise TypeError(msg)
        return data


def filter_headers(
    headers: list[tuple[str, str]], drop: frozenset[str] = frozenset()
) -> list[tuple[str, str]]:
    """Remove hop-by-hop headers and ``drop`` (lower-case names).

    Hop-by-hop headers are the fixed ``HOP_BY_HOP_HEADERS`` plus any
    header named in a ``Connection`` header (RFC 9110, section 7.6.1).

    Returns:
        Headers with lower-case names, in the original order.

    """
    nominated = {
        token.strip().lower()
        for name, value in headers
        if name.lower() == "connection"
        for token in value.split(",")
    }
    blocked = HOP_BY_HOP_HEADERS | drop | nominated
    return [
        (name.lower(), value)
        for name, value in headers
        if name.lower() not in blocked
    ]


def chunks(data: bytes) -> list[bytes]:
    """Split a body into tunnel-sized chunks.

    Returns:
        Chunks of at most ``MAX_CHUNK_SIZE`` bytes; none for empty data.

    """
    return [
        data[offset:offset + MAX_CHUNK_SIZE]
        for offset in range(0, len(data), MAX_CHUNK_SIZE)
    ]
