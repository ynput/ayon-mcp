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
