"""Read tools for project entities: folders, tasks, products, versions,
representations, plus a raw GraphQL escape hatch."""

from __future__ import annotations

from typing import Any

from ayon_mcp.app import mcp
from ayon_mcp.connection import api
from ayon_mcp.utils import collect, entity_fields

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


@mcp.tool()
def get_folder_hierarchy(
    project_name: str,
    search: str | None = None,
    folder_types: list[str] | None = None,
) -> dict[str, Any]:
    """Get the folder tree of a project in one call.

    Returns nested folders with id, name, folder type, status and task
    names. This is the best starting point for exploring a project's
    structure; use `list_tasks` or `list_products` afterwards with the
    folder ids it returns.

    Args:
        project_name: Project to inspect.
        search: Optional substring to filter folder names.
        folder_types: Optional folder type filter, e.g. ["Shot", "Asset"].
    """
    return api().get_folders_hierarchy(
        project_name, search_string=search, folder_types=folder_types
    )


@mcp.tool()
def list_folders(
    project_name: str,
    folder_path_regex: str | None = None,
    folder_types: list[str] | None = None,
    parent_id: str | None = None,
    statuses: list[str] | None = None,
    has_tasks: bool | None = None,
    include_attrib: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
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
    """
    folders = api().get_folders(
        project_name,
        folder_path_regex=folder_path_regex,
        folder_types=folder_types,
        parent_ids=[parent_id] if parent_id else None,
        statuses=statuses,
        has_tasks=has_tasks,
        fields=entity_fields(FOLDER_FIELDS, include_attrib),
    )
    return collect(folders, limit)


@mcp.tool()
def list_tasks(
    project_name: str,
    folder_id: str | None = None,
    task_types: list[str] | None = None,
    assignees: list[str] | None = None,
    statuses: list[str] | None = None,
    include_attrib: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """List/filter tasks of a project.

    Args:
        project_name: Project to query.
        folder_id: Only tasks under this folder id.
        task_types: Task type filter, e.g. ["Modeling", "Compositing"].
        assignees: Only tasks assigned to any of these AYON user names.
        statuses: Status name filter, e.g. ["Ready to start"].
        include_attrib: Include task attributes (frame range etc.).
        limit: Maximum number of tasks to return (default 50, max 500).
    """
    tasks = api().get_tasks(
        project_name,
        folder_ids=[folder_id] if folder_id else None,
        task_types=task_types,
        assignees=assignees,
        statuses=statuses,
        fields=entity_fields(TASK_FIELDS, include_attrib),
    )
    return collect(tasks, limit)


@mcp.tool()
def list_products(
    project_name: str,
    folder_id: str | None = None,
    product_types: list[str] | None = None,
    name_regex: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List/filter products (published outputs grouped by type) of a project.

    Args:
        project_name: Project to query.
        folder_id: Only products under this folder id.
        product_types: Product type filter, e.g. ["model", "render", "rig"].
        name_regex: Regex matched against product names.
        limit: Maximum number of products to return (default 50, max 500).
    """
    products = api().get_products(
        project_name,
        folder_ids=[folder_id] if folder_id else None,
        product_types=product_types,
        product_name_regex=name_regex,
        fields=PRODUCT_FIELDS,
    )
    return collect(products, limit)


@mcp.tool()
def list_versions(
    project_name: str,
    product_id: str | None = None,
    latest_only: bool = False,
    statuses: list[str] | None = None,
    include_attrib: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """List published versions of a project or a single product.

    Args:
        project_name: Project to query.
        product_id: Only versions of this product id.
        latest_only: Only the latest version per product.
        statuses: Status name filter, e.g. ["Approved"].
        include_attrib: Include version attributes.
        limit: Maximum number of versions to return (default 50, max 500).
    """
    versions = api().get_versions(
        project_name,
        product_ids=[product_id] if product_id else None,
        latest=True if latest_only else None,
        statuses=statuses,
        fields=entity_fields(VERSION_FIELDS, include_attrib),
    )
    return collect(versions, limit)


@mcp.tool()
def list_representations(
    project_name: str,
    version_id: str | None = None,
    names: list[str] | None = None,
    include_files: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """List representations (published file formats) of a project or version.

    Args:
        project_name: Project to query.
        version_id: Only representations of this version id.
        names: Representation name filter, e.g. ["exr", "mov", "abc"].
        include_files: Include the file list (paths, sizes) of each
            representation.
        limit: Maximum number of representations (default 50, max 500).
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
    return collect(representations, limit)


@mcp.tool()
def get_entity(project_name: str, entity_type: str, entity_id: str) -> dict[str, Any]:
    """Get one entity with full detail, including all attributes and data.

    Args:
        project_name: Project the entity belongs to.
        entity_type: One of "folder", "task", "product", "version",
            "representation".
        entity_id: The entity id (hex string).
    """
    server = api()
    getters = {
        "folder": server.get_folder_by_id,
        "task": server.get_task_by_id,
        "product": server.get_product_by_id,
        "version": server.get_version_by_id,
        "representation": server.get_representation_by_id,
    }
    getter = getters.get(entity_type)
    if getter is None:
        raise RuntimeError(
            f"Unknown entity_type {entity_type!r}. "
            f"Expected one of: {', '.join(sorted(getters))}."
        )
    entity = getter(project_name, entity_id)
    if entity is None:
        raise RuntimeError(
            f"{entity_type} with id {entity_id!r} was not found "
            f"in project {project_name!r}."
        )
    return entity


@mcp.tool()
def query_graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a raw GraphQL query against the AYON server.

    Escape hatch for anything the dedicated tools cannot express
    (cross-entity joins, link traversal, custom field selections).
    The GraphQL explorer schema is served at <server>/graphql.

    Args:
        query: GraphQL query string.
        variables: Optional query variables.
    """
    response = api().query_graphql(query, variables)
    if response.errors:
        messages = "; ".join(
            error.get("message", str(error)) for error in response.errors
        )
        raise RuntimeError(f"GraphQL query failed: {messages}")
    return response.data.get("data", {})
