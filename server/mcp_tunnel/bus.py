"""Message bus connecting the MCP tunnel hubs of all server workers.

A service's tunnel WebSocket is held by one worker of one server replica,
but an MCP request may land on any of them. Workers exchange tunnel
frames over the bus:

- ``tunnel channel`` - frames for a tunnel, published by any worker and
  read by the worker holding the tunnel.
- ``worker channel`` - response frames for requests a worker is serving.

Connected tunnels are announced with an expiry, so a crashed worker's
tunnels disappear after ``TUNNEL_TTL`` seconds.

The Redis implementation (``redis_bus``) needs the AYON server; this
module doesn't, so the hub can be tested without it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

# How long an announced tunnel stays listed without a heartbeat.
TUNNEL_TTL = 30

# Called with the channel name and the message.
MessageHandler = Callable[[str, bytes], Awaitable[None]]


class TunnelBus(ABC):
    """Transport between the tunnel hubs of all workers."""

    @abstractmethod
    async def start(self, handler: MessageHandler) -> None:
        """Start delivering messages of subscribed channels to ``handler``.

        ``handler`` is called with the channel name and the message, one
        message at a time, in the order they were published. It must not
        block on a slow peer - that would hold up every channel.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop delivering messages."""

    @abstractmethod
    async def subscribe(self, channel: str) -> None:
        """Start receiving messages published to ``channel``."""

    @abstractmethod
    async def unsubscribe(self, channel: str) -> None:
        """Stop receiving messages published to ``channel``."""

    @abstractmethod
    async def publish(self, channel: str, message: bytes) -> int:
        """Send ``message`` to the subscribers of ``channel``.

        Returns:
            The number of subscribers that received the message.

        """

    @abstractmethod
    async def announce(self, tunnel_id: str) -> None:
        """List a connected tunnel for ``TUNNEL_TTL`` seconds."""

    @abstractmethod
    async def withdraw(self, tunnel_id: str) -> None:
        """Remove a tunnel from the list."""

    @abstractmethod
    async def tunnels(self) -> list[str]:
        """Return the ids of all listed tunnels.

        Returns:
            Ids of tunnels whose listing has not expired.

        """
