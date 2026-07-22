"""Event stream tools: inspect and dispatch server events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..utils import collect

if TYPE_CHECKING:
    import ayon_api
    from fastmcp import FastMCP

EVENT_FIELDS = {
    "id", "topic", "project", "user", "sender", "status",
    "description", "createdAt", "updatedAt",
}


def register(mcp: "FastMCP", api: "ayon_api.ServerAPI") -> None:
    """Register event tools on *mcp* using *api* for data access."""

    @mcp.tool()
    def list_events(
        topics: list[str] | None = None,
        project_names: list[str] | None = None,
        statuses: list[str] | None = None,
        users: list[str] | None = None,
        newer_than: str | None = None,
        older_than: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List server events, newest first. Useful for pipeline debugging.

        Args:
            topics: Topic filter, supports wildcards on the server side,
                e.g. ["entity.folder.*", "log.error", "publish.*"].
            project_names: Only events of these projects.
            statuses: Event state filter; any of "pending", "in_progress",
                "finished", "failed", "aborted", "restarted".
            users: Only events created by these AYON user names.
            newer_than: ISO 8601 timestamp, e.g. "2026-07-13T00:00:00Z".
            older_than: ISO 8601 timestamp.
            limit: Maximum number of events to return (default 50, max 500).

        Returns compact event records; use `get_event` for full payload.
        """
        events = api.get_events(
            topics=topics,
            project_names=project_names,
            statuses=statuses,
            users=users,
            newer_than=newer_than,
            older_than=older_than,
            fields=EVENT_FIELDS,
        )
        return collect(events, limit)

    @mcp.tool()
    def get_event(event_id: str) -> dict[str, Any]:
        """Get one event with full detail, including summary and payload."""
        event = api.get_event(event_id)
        if not event:
            raise RuntimeError(f"Event {event_id!r} was not found.")
        return event

    @mcp.tool()
    def dispatch_event(
        topic: str,
        project_name: str | None = None,
        description: str | None = None,
        summary: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        finished: bool = True,
    ) -> dict[str, Any]:
        """Dispatch a new event to the AYON event stream.

        Other services (and users watching the event viewer) will see it.
        Custom topics should be namespaced, e.g. "mytool.sync.finished".

        Args:
            topic: Event topic.
            project_name: Optional project the event relates to.
            description: Short human-readable description.
            summary: Small JSON-serializable summary dict.
            payload: Full JSON-serializable payload dict.
            finished: Whether the event is created in finished state
                (False creates a pending event another service may process).
        """
        response = api.dispatch_event(
            topic,
            project_name=project_name,
            description=description,
            summary=summary,
            payload=payload,
            finished=finished,
        )
        data = response.data if hasattr(response, "data") else {}
        return {"event_id": (data or {}).get("id"), "topic": topic}
