"""Write tools: create/update/delete entities and comments.

These modify production data. All of them go through the AYON
operations endpoint, so server-side validation and events apply.
"""

from __future__ import annotations

from typing import Any

from services.mcp.app import mcp
from services.mcp.connection import api

WRITABLE_ENTITY_TYPES = ("folder", "task", "product", "version")


def _check_entity_type(entity_type: str) -> None:
    if entity_type not in WRITABLE_ENTITY_TYPES:
        raise RuntimeError(
            f"Unsupported entity_type {entity_type!r}. "
            f"Expected one of: {', '.join(WRITABLE_ENTITY_TYPES)}."
        )


@mcp.tool()
def create_entity(
    project_name: str,
    entity_type: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Create a folder, task, product or version in a project.

    Required keys in `data` per entity type:
    - folder: "name", "folderType" (e.g. "Shot"); "parentId" for nesting
      (omit for a root folder).
    - task: "name", "taskType" (e.g. "Modeling"), "folderId".
    - product: "name", "productType" (e.g. "model"), "folderId".
    - version: "version" (integer), "productId".

    Optional keys: "status", "tags", "attrib" (dict of attribute
    values), "assignees" (tasks only), "label".

    Valid folder/task types and statuses are project-specific — check
    `get_project` first. Returns the new entity id.
    """
    _check_entity_type(entity_type)
    result = api().send_batch_operations(
        project_name,
        [{"type": "create", "entityType": entity_type, "data": data}],
    )
    operation = result[0] if result else {}
    return {
        "entity_id": operation.get("entityId"),
        "entity_type": entity_type,
        "project": project_name,
    }


@mcp.tool()
def update_entity(
    project_name: str,
    entity_type: str,
    entity_id: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Update fields of an existing folder, task, product or version.

    `data` contains only the fields to change, e.g.
    {"status": "Approved"}, {"assignees": ["john"]},
    {"attrib": {"frameStart": 1001, "frameEnd": 1150}},
    {"name": "..."}, {"label": "..."}, {"tags": [...]},
    {"active": false} (archives the entity).

    Attribute updates are merged; other fields are replaced as given.
    """
    _check_entity_type(entity_type)
    api().send_batch_operations(
        project_name,
        [{
            "type": "update",
            "entityType": entity_type,
            "entityId": entity_id,
            "data": data,
        }],
    )
    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "updated": sorted(data),
    }


@mcp.tool()
def delete_entity(
    project_name: str,
    entity_type: str,
    entity_id: str,
) -> dict[str, Any]:
    """Permanently delete a folder, task, product or version.

    This cannot be undone and the server will refuse to delete entities
    that still have children (e.g. a folder with tasks or products).
    Prefer archiving via `update_entity` with {"active": false} unless
    permanent deletion was explicitly requested.
    """
    _check_entity_type(entity_type)
    api().send_batch_operations(
        project_name,
        [{
            "type": "delete",
            "entityType": entity_type,
            "entityId": entity_id,
        }],
    )
    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "deleted": True,
    }


@mcp.tool()
def add_comment(
    project_name: str,
    entity_type: str,
    entity_id: str,
    text: str,
) -> dict[str, Any]:
    """Post a comment on an entity's activity feed.

    Markdown is supported; @mentions use the AYON user name, e.g.
    "@john please review". `entity_type` is one of "folder", "task",
    "product", "version".
    """
    activity_id = api().create_activity(
        project_name,
        entity_id,
        entity_type,
        "comment",
        body=text,
    )
    return {"activity_id": activity_id, "entity_id": entity_id}
