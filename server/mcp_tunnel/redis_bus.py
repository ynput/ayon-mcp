"""Tunnel bus on the AYON server's Redis."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from ayon_server.lib.redis import Redis
from ayon_server.logging import log_traceback

from .bus import TUNNEL_TTL, MessageHandler, TunnelBus

if TYPE_CHECKING:
    from redis.asyncio.client import PubSub


class RedisTunnelBus(TunnelBus):
    """Tunnel bus on Redis pub/sub, with tunnels listed in a sorted set.

    All keys and channels start with ``prefix``, so hubs of different
    addon versions (production and staging services) never mix.
    """

    def __init__(self, prefix: str) -> None:
        """Initialize the bus.

        Args:
            prefix: Prefix of all Redis keys and channels of this bus.

        """
        self._prefix = prefix
        self._pubsub: PubSub | None = None
        self._task: asyncio.Task[None] | None = None
        self._handler: MessageHandler | None = None

    def _name(self, name: str) -> str:
        return f"{Redis.prefix}{self._prefix}{name}"

    async def start(self, handler: MessageHandler) -> None:
        """Start the tunnel bus and set the message handler.

        Args:
            handler: Callable to handle incoming messages.

        """
        self._handler = handler
        self._pubsub = await Redis.pubsub()
        self._task = asyncio.create_task(self._read())

    async def stop(self) -> None:
        """Stop the tunnel bus."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._pubsub is not None:
            await self._pubsub.aclose()
            self._pubsub = None

    async def subscribe(self, channel: str) -> None:
        """Subscribe to a Redis channel.

        Args:
            channel: The Redis channel to subscribe to.

        Raises:
            RuntimeError: If the tunnel bus is not started.

        """
        if self._pubsub is None:
            msg = "tunnel bus not started"
            raise RuntimeError(msg)
        await self._pubsub.subscribe(self._name(channel))

    async def unsubscribe(self, channel: str) -> None:
        """Unsubscribe from a Redis channel.

        Args:
            channel: The Redis channel to unsubscribe from.

        """
        if self._pubsub is not None:
            await self._pubsub.unsubscribe(self._name(channel))

    async def publish(self, channel: str, message: bytes) -> int:
        """Publish a message to a Redis channel.

        Args:
            channel: The Redis channel to publish to.
            message: The message to publish.

        Returns:
            The number of clients that received the message.

        """
        if not Redis.connected:
            await Redis.connect()
        return await Redis.redis_pool.publish(self._name(channel), message)

    async def announce(self, tunnel_id: str) -> None:
        """Announce a tunnel to the Redis bus."""
        if not Redis.connected:
            await Redis.connect()
        await Redis.redis_pool.zadd(
            self._name("tunnels"), {tunnel_id: time.time() + TUNNEL_TTL}
        )

    async def withdraw(self, tunnel_id: str) -> None:
        """Withdraw a tunnel from the Redis bus.

        Args:
            tunnel_id: The ID of the tunnel to withdraw.

        """
        if not Redis.connected:
            await Redis.connect()
        await Redis.redis_pool.zrem(self._name("tunnels"), tunnel_id)

    async def tunnels(self) -> list[str]:
        """Return a list of currently announced tunnels.

        Returns:
            A list of tunnel IDs that are currently announced.

        """
        if not Redis.connected:
            await Redis.connect()
        key = self._name("tunnels")
        now = time.time()
        await Redis.redis_pool.zremrangebyscore(key, "-inf", now)
        members = await Redis.redis_pool.zrangebyscore(key, now, "+inf")
        return [
            m.decode() if isinstance(m, bytes) else str(m) for m in members
        ]

    async def _read(self) -> None:
        if self._pubsub is None:
            return
        prefix = self._name("")
        while True:
            if not self._pubsub.subscribed:
                # get_message fails until the first subscription
                await asyncio.sleep(0.1)
                continue
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # ruff: ignore[blind-except]
                log_traceback("MCP tunnel bus read failed")
                await asyncio.sleep(1)
                continue
            if message is None or self._handler is None:
                continue
            channel = message["channel"]
            if isinstance(channel, bytes):
                channel = channel.decode()
            try:
                await self._handler(
                    channel.removeprefix(prefix), message["data"]
                )
            except Exception:  # ruff: ignore[blind-except]
                log_traceback("MCP tunnel bus handler failed")
