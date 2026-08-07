"""Event stream tools: inspect and dispatch server events."""

from __future__ import annotations

from typing import Any, Iterable, Literal

from ayon_mcp.client import get_global_ayon_client as api
from ayon_mcp.utils import collect

from .utils import _CamelModel

EVENT_FIELDS = {
    "id", "topic", "project", "user", "sender", "status",
    "description", "createdAt", "updatedAt",
}

TStatuses = Iterable[
    Literal[
        "pending",
        "in_progress",
        "finished",
        "failed",
        "aborted",
        "restarted",
    ]
]


class EventItem(_CamelModel):
    """Event item."""
    id: str
    hash: str | None = None
    topic: str
    sender: str | None = None
    sender_type: str | None = None
    project: str | None = None
    user: str | None = None
    depends_on: str | None = None
    status: str | None = None
    retries: int | None = None
    description: str | None = None
    summary: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None


def list_events(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    topics: list[str] | None = None,
    project_names: list[str] | None = None,
    statuses: TStatuses | None = None,
    users: list[str] | None = None,
    newer_than: str | None = None,
    older_than: str | None = None,
    limit: int = 50,
    offset: int = 0,
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
        offset: Items to skip for paging; use the `next_offset` value
            returned by the previous call.

    Returns:
        compact event records; use `get_event` for full payload.
        When truncated, call again with `offset=next_offset` for the
        next page.
    """
    events = api().get_events(
        topics=topics,
        project_names=project_names,
        statuses=statuses,
        users=users,
        newer_than=newer_than,
        older_than=older_than,
        fields=EVENT_FIELDS,
    )
    return collect(events, limit, offset)


def get_event(event_id: str) -> EventItem:
    """Get one event with full detail.

    Including summary and payload.

    Args:
        event_id: Event ID (hex string).

    Returns:
        EventItem with full detail.

    Raises:
        RuntimeError: If the event was not found.

    """
    event = api().get_event(event_id)
    if not event:
        msg = f"Event {event_id!r} was not found."
        raise RuntimeError(msg)

    return EventItem.model_validate(event)


def dispatch_event(  # ruff: ignore[too-many-arguments]
    topic: str,
    project_name: str | None = None,
    description: str | None = None,
    summary: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    *,
    finished: bool = True,
) -> EventItem:
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

    Returns:
        EventItem with the new event's ID and topic.

    """
    response = api().dispatch_event(
        topic,
        project_name=project_name,
        description=description,
        summary=summary,
        payload=payload,
        finished=finished,
    )
    data = response.data if hasattr(response, "data") else {}
    return EventItem(
        id=(data or {}).get("id"),  # ty:ignore[invalid-argument-type]
        topic=topic,
        project=project_name,
        description=description,
        summary=summary,
        payload=payload,
        status="finished" if finished else "pending",
    )
