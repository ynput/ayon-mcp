"""Accept or reject the tunnel WebSocket of an MCP service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._logging import logger
from .protocol import PROTOCOL_HEADER, PROTOCOL_VERSION

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.websockets import WebSocket

    from .hub import TunnelHub

# Close codes for rejected tunnels. The WebSocket is accepted first and then
# closed with one of these and a reason, so the service can log what is
# wrong. (A rejection before accepting reaches clients as a bare HTTP 403,
# which is also what servers without this endpoint send.)
CLOSE_UNSUPPORTED = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403

# WebSocket close reasons must fit in a control frame: 123 bytes of UTF-8.
MAX_REASON_BYTES = 123


class TunnelRejectedError(Exception):
    """The tunnel must not be served."""

    def __init__(self, code: int, reason: str) -> None:
        """Initialize the error.

        Args:
            code: WebSocket close code sent to the service.
            reason: Close reason sent to the service.

        """
        super().__init__(reason)
        self.code = code
        self.reason = reason


def close_reason(reason: str) -> str:
    """Shorten a close reason to fit a close frame.

    Returns:
        At most ``MAX_REASON_BYTES`` of UTF-8, cut between characters.

    """
    data = reason.encode()[:MAX_REASON_BYTES]
    return data.decode("utf-8", errors="ignore")


def _check_protocol(websocket: WebSocket, user_name: str) -> None:
    """Check that the service speaks this tunnel protocol.

    Raises:
        TunnelRejectedError: If it speaks another version.

    """
    version = websocket.headers.get(PROTOCOL_HEADER)
    if version == str(PROTOCOL_VERSION):
        return
    logger.warning(
        f"Rejecting MCP tunnel from {user_name}: "
        f"protocol {version!r}, expected {PROTOCOL_VERSION}"
    )
    raise TunnelRejectedError(
        CLOSE_UNSUPPORTED,
        f"Unsupported tunnel protocol {version!r}, "
        f"server supports {PROTOCOL_VERSION}",
    )


async def serve_tunnel(
    websocket: WebSocket,
    hub: TunnelHub,
    authenticate: Callable[[WebSocket], Awaitable[str]],
) -> None:
    """Serve a service's tunnel WebSocket until it disconnects.

    Args:
        websocket: The not yet accepted WebSocket.
        hub: Hub of this worker.
        authenticate: Returns the name of the service user that opened
            the WebSocket, or raises ``TunnelRejectedError``.

    """
    try:
        user_name = await authenticate(websocket)
        _check_protocol(websocket, user_name)
    except TunnelRejectedError as exc:
        await websocket.accept()
        await websocket.close(
            code=exc.code, reason=close_reason(exc.reason)
        )
        return

    await websocket.accept()
    await hub.serve(websocket, user_name)
