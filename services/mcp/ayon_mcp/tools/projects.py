"""Project-level and server-level read tools."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ayon_mcp.client import get_global_ayon_client as api

from .utils import _CamelModel


class SimpleProjectItem(_CamelModel):
    """Minimal project item with name and code."""
    name: str = Field(..., description="Project name")
    code: str = Field(..., description="Project code")
    active: bool = Field(..., description="Project is active")
    created_at: str = Field(..., description="Project creation timestamp")


class ProjectItemsList(_CamelModel):
    """Generic list of items in a project."""
    count: int = Field(
        ..., description="Number of items in the list")
    items: list[SimpleProjectItem] = Field(
        ..., description="List of project items")


class StatusInfo(_CamelModel):
    """One status defined in a project's anatomy."""

    name: str = Field(..., description="Status name to use in writes")
    state: str | None = Field(
        None,
        description=(
            "Status semantics: not_started, in_progress, done or blocked"
        ),
    )
    scope: list[str] | None = Field(
        None,
        description=(
            "Entity types this status applies to (all types when null)"
        ),
    )


class LinkTypeInfo(_CamelModel):
    """One entity link type defined in a project."""

    link_type: str = Field(..., description="Link type name, e.g. breakdown")
    input_type: str = Field(..., description="Entity type on the input side")
    output_type: str = Field(
        ..., description="Entity type on the output side")


class ProjectAnatomy(_CamelModel):
    """Compact per-project vocabulary for entity writes."""

    project_name: str
    folder_types: list[str] = Field(
        ..., description="Valid folderType values for folders")
    task_types: list[str] = Field(
        ..., description="Valid taskType values for tasks")
    statuses: list[StatusInfo] = Field(
        ..., description="Valid status values, with state and scope")
    tags: list[str] = Field(
        ..., description="Predefined tag names")
    link_types: list[LinkTypeInfo] = Field(
        ..., description="Valid entity link types")


class ServerInfo(_CamelModel):
    """AYON server info and connected user identity."""
    server_url: str = Field(..., description="AYON server URL")
    server_version: str = Field(..., description="AYON server version")
    user_name: str | None = Field(
        None, description="Connected user name")
    user_is_admin: bool = Field(
        default=False, description="Connected user is admin")
    user_is_service: bool = Field(
        default=False, description="Connected user is service account")


def list_projects(
        *,
        include_inactive: bool = False
    ) -> ProjectItemsList:
    """List projects on the AYON server.

    Returns name, code and flags for each project. Use `get_project` for
    full detail (statuses, folder/task types, attributes) of one project.

    Args:
        include_inactive: If True, include inactive projects in the list.

    Returns:
        ProjectItemsList: List of projects with name, code and flags.

    """
    # This exact field set keeps ayon_api on its light REST-list path;
    # anything beyond it makes ayon_api return full project entities.
    projects = list(api().get_projects(
        active=None if include_inactive else True,
        fields={"name", "code", "active", "createdAt"},
    ))
    project_models = [SimpleProjectItem.model_validate(p) for p in projects]
    return ProjectItemsList(count=len(projects), items=project_models)


def get_project(project_name: str) -> dict[str, Any]:
    """Get full detail of one project.

    Includes attributes (fps, resolution, frame ranges...), and the
    project anatomy lists: statuses, tags, folder types and task types.

    Args:
        project_name: Name of the project to retrieve.

    Returns:
        dict: Full project detail as returned by the AYON API.

    Raises:
        RuntimeError: If the project is not found on the server.

    Note:
        This function returns the raw dictionary from the AYON API.
        There is no Pydantic model for the full project detail, as it may vary
        depending on the server configuration and custom attributes.

    """
    project = api().get_project(project_name)
    if project is None:
        msg = (
            f"Project {project_name!r} was not found on the server. "
            "Use list_projects to see available projects."
        )
        raise RuntimeError(msg)
    return project


def get_project_anatomy(project_name: str) -> ProjectAnatomy:
    """Get the vocabulary of one project: valid types, statuses and tags.

    This is the compact reference for entity writes — check it before
    `create_entity` / `update_entity` so folderType, taskType, status
    and tags values are valid for this specific project. For attribute
    definitions use `list_attributes`; for everything else about the
    project use `get_project`.

    Note this is the write-relevant subset of AYON's project anatomy;
    the full anatomy (also roots, path templates, entity naming) is
    served by the REST endpoint
    `/api/projects/{project_name}/anatomy`.

    Args:
        project_name: Name of the project to inspect.

    Returns:
        ProjectAnatomy: folder_types, task_types, statuses (with state
        and scope), tags and link_types of the project.

    Raises:
        RuntimeError: If the project is not found on the server.

    """
    project = api().get_project(project_name)
    if project is None:
        msg = (
            f"Project {project_name!r} was not found on the server. "
            "Use list_projects to see available projects."
        )
        raise RuntimeError(msg)
    return ProjectAnatomy(
        project_name=project.get("name", project_name),
        folder_types=[
            folder_type["name"]
            for folder_type in project.get("folderTypes") or []
        ],
        task_types=[
            task_type["name"]
            for task_type in project.get("taskTypes") or []
        ],
        statuses=[
            StatusInfo(
                name=status.get("name", ""),
                state=status.get("state"),
                scope=status.get("scope"),
            )
            for status in project.get("statuses") or []
        ],
        tags=[tag["name"] for tag in project.get("tags") or []],
        link_types=[
            LinkTypeInfo(
                link_type=link.get("linkType", ""),
                input_type=link.get("inputType", ""),
                output_type=link.get("outputType", ""),
            )
            for link in project.get("linkTypes") or []
        ],
    )


def get_server_info() -> ServerInfo:
    """Get AYON server version and the identity of the connected user.

    Useful as a connectivity check before other calls.

    Returns:
        ServerInfo: AYON server info and connected user identity.

    """
    user = api().get_user()
    return ServerInfo(
        server_url=api().base_url,
        server_version=api().get_server_version(),
        user_name=user.get("name"),
        user_is_admin=user.get("data", {}).get("isAdmin", False),
        user_is_service=user.get("data", {}).get("isService", False),
    )
