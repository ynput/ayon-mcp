"""Proxy for the MCP service, reached through a WebSocket tunnel.

The MCP service connects to the addon's WebSocket
(``/api/addons/mcp/{version}/ws``) and serves the addon's MCP endpoint
(``/api/addons/mcp/{version}/mcp``) through it, so it needs no listening
port, public IP or ingress. See ``protocol`` for the wire format and
``hub`` for routing across server workers.

Only ``redis_bus`` needs the AYON server; the rest runs anywhere, which
lets the tests use it without one.
"""

from .endpoint import (
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHORIZED,
    CLOSE_UNSUPPORTED,
    TunnelRejectedError,
    serve_tunnel,
)
from .hub import TunnelHub
from .proxy import proxy_request

__all__ = [
    "CLOSE_FORBIDDEN",
    "CLOSE_UNAUTHORIZED",
    "CLOSE_UNSUPPORTED",
    "TunnelHub",
    "TunnelRejectedError",
    "proxy_request",
    "serve_tunnel",
]
