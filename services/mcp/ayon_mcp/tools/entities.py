"""Read tools for project entities.

Entities: folders, tasks, products, versions,
representations, plus a raw GraphQL escape hatch.
"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import Field

from ayon_mcp.client import get_global_ayon_client as api
from ayon_mcp.utils import collect, entity_fields

from .utils import _CamelModel

FOLDER_FIELDS = {
    "id", "name", "label", "path", "folderType",
    "parentId", "status", "tags", "active",
}
TASK_FIELDS = {
    "id", "name", "label", "taskType", "folderId",
    "assignees", "status", "tags", "active",
}
PRODUCT_FIELDS = {
    "id", "name", "productType", "folderId", "status", "tags", "active",
}
VERSION_FIELDS = {
    "id", "version", "productId", "taskId", "author",
    "status", "tags", "active", "createdAt",
}
REPRESENTATION_FIELDS = {
    "id", "name", "versionId", "status", "tags", "active",
}


# ---------------------------------------------------------------------------
# Entity models
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


class HierarchyItem(_CamelModel):
    """One node in the folder hierarchy tree."""

    id: str
    name: str
    label: str
    status: str
    folder_type: str
    has_tasks: bool
    task_names: list[str]
    parents: list[str]
    parent_id: str | None = None
    children: list[HierarchyItem] = Field(default_factory=list)


HierarchyItem.model_rebuild()


class FolderHierarchy(_CamelModel):
    """Top-level response from the folder hierarchy endpoint."""

    hierarchy: list[HierarchyItem]


class Folder(_CamelModel):
    """An AYON folder entity (asset, shot, sequence, ...)."""

    id: str
    name: str
    label: str | None = None
    path: str
    folder_type: str
    parent_id: str | None = None
    status: str
    tags: list[str] = Field(default_factory=list)
    active: bool
    attrib: dict[str, Any] | None = None


class Task(_CamelModel):
    """An AYON task entity."""

    id: str
    name: str
    label: str | None = None
    task_type: str
    folder_id: str
    assignees: list[str] = Field(default_factory=list)
    status: str
    tags: list[str] = Field(default_factory=list)
    active: bool
    attrib: dict[str, Any] | None = None


class Product(_CamelModel):
    """An AYON product entity (published output group)."""

    id: str
    name: str
    product_type: str
    folder_id: str
    status: str
    tags: list[str] = Field(default_factory=list)
    active: bool


class Version(_CamelModel):
    """An AYON version entity (one published iteration of a product)."""

    id: str
    version: int
    product_id: str
    task_id: str | None = None
    author: str | None = None
    status: str
    tags: list[str] = Field(default_factory=list)
    active: bool
    created_at: str | None = None
    attrib: dict[str, Any] | None = None


class Representation(_CamelModel):
    """An AYON representation entity (one file format of a version)."""

    id: str
    name: str
    version_id: str
    status: str
    tags: list[str] = Field(default_factory=list)
    active: bool
    files: list[dict[str, Any]] | None = None


class EntityList(_CamelModel, Generic[_T]):
    """Paginated list of entities returned by list_* functions."""

    count: int
    truncated: bool
    items: list[_T]


def get_folder_hierarchy(
    project_name: str,
    search: str | None = None,
    folder_types: list[str] | None = None,
) -> FolderHierarchy:
    """Get the folder tree of a project in one call.

    Returns nested folders with id, name, folder type, status and task
    names. This is the best starting point for exploring a project's
    structure; use `list_tasks` or `list_products` afterwards with the
    folder ids it returns.

    Args:
        project_name: Project to inspect.
        search: Optional substring to filter folder names.
        folder_types: Optional folder type filter, e.g. ["Shot", "Asset"].

    Returns:
        A FolderHierarchy with a ``hierarchy`` list of nested HierarchyItem
        objects, each having id, name, folderType, status, taskNames and
        children.

    """
    data = api().get_folders_hierarchy(
        project_name, search_string=search, folder_types=folder_types
    )
    return FolderHierarchy.model_validate(data)


def list_folders(  # ruff: ignore[too-many-arguments]
    project_name: str,
    folder_path_regex: str | None = None,
    folder_types: list[str] | None = None,
    parent_id: str | None = None,
    statuses: list[str] | None = None,
    *,
    has_tasks: bool | None = None,
    include_attrib: bool = False,
    limit: int = 50,
) -> EntityList[Folder]:
    """List/filter folders (assets, shots, sequences...) of a project.

    Args:
        project_name: Project to query.
        folder_path_regex: Regex matched against folder paths,
            e.g. "^/shots/sq010/.*".
        folder_types: Folder type filter, e.g. ["Shot"].
        parent_id: Only direct children of this folder id.
        statuses: Status name filter, e.g. ["In progress"].
        has_tasks: Only folders with (or without) tasks.
        include_attrib: Include attributes (fps, resolution, frame
            ranges...). Off by default to keep responses small.
        limit: Maximum number of folders to return (default 50, max 500).

    Returns:
        An EntityList[Folder] with count, truncated and items fields.
        Each Folder has: id, name, label, path, folderType, parentId,
        status, tags, active. Includes attrib if `include_attrib` is True.

    """
    folders = api().get_folders(
        project_name,
        folder_path_regex=folder_path_regex,
        folder_types=folder_types,
        parent_ids=[parent_id] if parent_id else None,
        statuses=statuses,
        has_tasks=has_tasks,
        fields=entity_fields(FOLDER_FIELDS, include_attrib=include_attrib),
    )
    return EntityList[Folder].model_validate(collect(folders, limit))


def list_tasks(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    project_name: str,
    folder_id: str | None = None,
    task_types: list[str] | None = None,
    assignees: list[str] | None = None,
    statuses: list[str] | None = None,
    limit: int = 50,
    *,
    include_attrib: bool = False,
) -> EntityList[Task]:
    """List/filter tasks of a project.

    Args:
        project_name: Project to query.
        folder_id: Only tasks under this folder id.
        task_types: Task type filter, e.g. ["Modeling", "Compositing"].
        assignees: Only tasks assigned to any of these AYON user names.
        statuses: Status name filter, e.g. ["Ready to start"].
        include_attrib: Include task attributes (frame range etc.).
        limit: Maximum number of tasks to return (default 50, max 500).

    Returns:
        An EntityList[Task] with count, truncated and items fields.
        Each Task has: id, name, label, taskType, folderId, assignees,
        status, tags, active. Includes attrib if `include_attrib` is True.

    """
    tasks = api().get_tasks(
        project_name,
        folder_ids=[folder_id] if folder_id else None,
        task_types=task_types,
        assignees=assignees,
        statuses=statuses,
        fields=entity_fields(TASK_FIELDS, include_attrib=include_attrib),
    )
    return EntityList[Task].model_validate(collect(tasks, limit))


def list_products(
    project_name: str,
    folder_id: str | None = None,
    product_types: list[str] | None = None,
    name_regex: str | None = None,
    limit: int = 50,
) -> EntityList[Product]:
    """List/filter products (published outputs grouped by type) of a project.

    Args:
        project_name: Project to query.
        folder_id: Only products under this folder id.
        product_types: Product type filter, e.g. ["model", "render", "rig"].
        name_regex: Regex matched against product names.
        limit: Maximum number of products to return (default 50, max 500).

    Returns:
        An EntityList[Product] with count, truncated and items fields.
        Each Product has: id, name, product_type, folder_id,
        status, tags, active.

    """
    products = api().get_products(
        project_name,
        folder_ids=[folder_id] if folder_id else None,
        product_types=product_types,
        product_name_regex=name_regex,
        fields=PRODUCT_FIELDS,
    )
    return EntityList[Product].model_validate(collect(products, limit))


def list_versions(  # ruff: ignore[too-many-arguments]
    project_name: str,
    product_id: str | None = None,
    statuses: list[str] | None = None,
    limit: int = 50,
    *,
    latest_only: bool = False,
    include_attrib: bool = False,
) -> EntityList[Version]:
    """List published versions of a project or a single product.

    Args:
        project_name: Project to query.
        product_id: Only versions of this product id.
        latest_only: Only the latest version per product.
        statuses: Status name filter, e.g. ["Approved"].
        include_attrib: Include version attributes.
        limit: Maximum number of versions to return (default 50, max 500).

    Returns:
        An EntityList[Version] with count, truncated and items fields.
        Each Version has: id, version, productId, taskId, author, status,
        tags, active, createdAt. Includes attrib if `include_attrib` is True.

    """
    versions = api().get_versions(
        project_name,
        product_ids=[product_id] if product_id else None,
        latest=True if latest_only else None,
        statuses=statuses,
        fields=entity_fields(VERSION_FIELDS, include_attrib=include_attrib),
    )
    return EntityList[Version].model_validate(collect(versions, limit))


def list_representations(
    project_name: str,
    version_id: str | None = None,
    names: list[str] | None = None,
    limit: int = 50,
    *,
    include_files: bool = False,
) -> EntityList[Representation]:
    """List representations (published file formats) of a project or version.

    Args:
        project_name: Project to query.
        version_id: Only representations of this version id.
        names: Representation name filter, e.g. ["exr", "mov", "abc"].
        include_files: Include the file list (paths, sizes) of each
            representation.
        limit: Maximum number of representations (default 50, max 500).

    Returns:
        An EntityList[Representation] with count, truncated and items fields.
        Each Representation has: id, name, versionId, status, tags, active.
        Includes files if `include_files` is True.

    """
    fields = set(REPRESENTATION_FIELDS)
    if include_files:
        fields.add("files")
    representations = api().get_representations(
        project_name,
        version_ids=[version_id] if version_id else None,
        representation_names=names,
        fields=fields,
    )
    return EntityList[Representation].model_validate(
        collect(representations, limit)
    )


def get_entity(
    project_name: str,
    entity_type: str,
    entity_id: str,
) -> Folder | Task | Product | Version | Representation:
    """Get one entity with full detail, including all attributes and data.

    Args:
        project_name: Project the entity belongs to.
        entity_type: One of "folder", "task", "product", "version",
            "representation".
        entity_id: The entity id (hex string).

    Returns:
        The validated entity model: Folder, Task, Product, Version,
        or Representation, depending on ``entity_type``.

    Raises:
        RuntimeError: If ``entity_type`` is unknown or the entity is
            not found.
    """
    getters: dict[str, Any] = {
        "folder": (api().get_folder_by_id, Folder),
        "task": (api().get_task_by_id, Task),
        "product": (api().get_product_by_id, Product),
        "version": (api().get_version_by_id, Version),
        "representation": (api().get_representation_by_id, Representation),
    }
    entry = getters.get(entity_type)
    if entry is None:
        valid = ", ".join(sorted(getters))
        msg = f"Unknown entity_type {entity_type!r}. Expected one of: {valid}."
        raise RuntimeError(msg)
    getter, model_cls = entry
    entity = getter(project_name, entity_id)
    if entity is None:
        msg = (
            f"{entity_type} with id {entity_id!r} was not found "
            f"in project {project_name!r}."
        )
        raise RuntimeError(msg)
    return model_cls.model_validate(entity)


def query_graphql(
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a raw GraphQL query against the AYON server.

    Escape hatch for anything the dedicated tools cannot express
    (cross-entity joins, link traversal, custom field selections).
    The GraphQL explorer schema is served at <server>/graphql.

    Args:
        query: GraphQL query string.
        variables: Optional query variables.

    Returns:
        The "data" dict of the GraphQL response. Raises RuntimeError
        if the query fails, with all error messages joined into one string.

    Raises:
        RuntimeError: If the query fails, with all error messages joined
            into one string.

    """
    response = api().query_graphql(query, variables)
    if response.errors:
        messages = "; ".join(
            error.get("message", str(error)) for error in response.errors
        )
        msg = f"GraphQL query failed: {messages}"
        raise RuntimeError(msg)
    return response.data.get("data", {})
