"""Write tools: create/update/delete entities and comments.

These modify production data. All of them go through the AYON
operations endpoint, so server-side validation and events apply.
"""

from __future__ import annotations

from typing import Any

from ayon_mcp.client import get_global_ayon_client as api

from .utils import _CamelModel

WRITABLE_ENTITY_TYPES = ("folder", "task", "product", "version")


class EntityOperationResponse(_CamelModel):
    """Response for create/update/delete entity operations."""
    entity_id: str
    entity_type: str
    project: str | None = None
    updated: list[str] | None = None
    deleted: bool | None = None


class CommentResponse(_CamelModel):
    """Response for add_comment operation."""
    activity_id: str
    entity_id: str
    entity_type: str


def _check_entity_type(entity_type: str) -> None:
    """Raise RuntimeError if entity_type is not writable.

    Args:
        entity_type: Entity type to check.

    Raises:
        RuntimeError: If entity_type is not one of the writable types.

    """
    if entity_type not in WRITABLE_ENTITY_TYPES:
        msg = (
            f"Unsupported entity_type {entity_type!r}. "
            f"Expected one of: {', '.join(WRITABLE_ENTITY_TYPES)}."
        )
        raise RuntimeError(msg)


def create_entity(
    project_name: str,
    entity_type: str,
    data: dict[str, Any],
) -> EntityOperationResponse:
    """Create a folder, task, product or version in a project.

    Required keys in `data` per entity type:
    - folder: "name", "folderType" (e.g. "Shot"); "parentId" for nesting
        (omit for a root folder).
    - task: "name", "taskType" (e.g. "Modeling"), "folderId".
    - product: "name", "productType" (e.g. "model"), "folderId".
    - version: "version" (integer), "productId".

    Optional keys: "status", "tags", "attrib" (dict of attribute
    values), "assignees" (tasks only), "label".

    Folder/task types, statuses and tags are defined per project (the
    values above are just common examples) — check `get_project_anatomy`
    first. Returns the new entity id.

    Args:
        project_name: Name of the project to create the entity in.
        entity_type: One of "folder", "task", "product", "version".
        data: Dictionary of entity fields to set.

    Returns:
        EntityOperationResponse: Contains the new entity's ID,
            type and project.

    """
    _check_entity_type(entity_type)
    result = api().send_batch_operations(
        project_name,
        [{"type": "create", "entityType": entity_type, "data": data}],
    )
    operation = result[0] if result else {}
    return EntityOperationResponse(
        entity_id=operation.get("entityId"),  # ty:ignore[invalid-argument-type]
        entity_type=entity_type,
        project=project_name,
    )


def update_entity(
    project_name: str,
    entity_type: str,
    entity_id: str,
    data: dict[str, Any],
) -> EntityOperationResponse:
    """Update fields of an existing folder, task, product or version.

    `data` contains only the fields to change, e.g.
    {"status": "Approved"}, {"assignees": ["john"]},
    {"attrib": {"frameStart": 1001, "frameEnd": 1150}},
    {"name": "..."}, {"label": "..."}, {"tags": [...]},
    {"active": false} (archives the entity).

    Attribute updates are merged; other fields are replaced as given.

    Args:
        project_name: Name of the project containing the entity.
        entity_type: One of "folder", "task", "product", "version".
        entity_id: ID of the entity to update.
        data: Dictionary of fields to update.

    Returns:
        EntityOperationResponse: Contains the updated entity's ID,
            type and project.

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
    return EntityOperationResponse(
        entity_id=entity_id,
        entity_type=entity_type,
        project=project_name,
        updated=sorted(data),
    )


def delete_entity(
    project_name: str,
    entity_type: str,
    entity_id: str,
) -> EntityOperationResponse:
    """Permanently delete a folder, task, product or version.

    This cannot be undone and the server will refuse to delete entities
    that still have children (e.g. a folder with tasks or products).
    Prefer archiving via `update_entity` with {"active": false} unless
    permanent deletion was explicitly requested.

    Args:
        project_name: Name of the project containing the entity.
        entity_type: One of "folder", "task", "product", "version".
        entity_id: ID of the entity to delete.

    Returns:
        EntityOperationResponse: Contains the deleted entity's ID,
            type and project.

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
    return EntityOperationResponse(
        entity_id=entity_id,
        entity_type=entity_type,
        project=project_name,
        deleted=True,
    )


def add_comment(
    project_name: str,
    entity_type: str,
    entity_id: str,
    text: str,
) -> CommentResponse:
    """Post a comment on an entity's activity feed.

    Markdown is supported; @mentions use the AYON user name, e.g.
    "@john please review". `entity_type` is one of "folder", "task",
    "product", "version".

    Args:
        project_name: Name of the project containing the entity.
        entity_type: One of "folder", "task", "product", "version".
        entity_id: ID of the entity to comment on.
        text: Comment text in Markdown format.

    Returns:
        CommentResponse: Contains the new activity ID, entity ID and type.

    """
    activity_id = api().create_activity(
        project_name,
        entity_id,
        entity_type,
        "comment",
        body=text,
    )
    return CommentResponse(
        activity_id=activity_id,
        entity_id=entity_id,
        entity_type=entity_type,
    )
