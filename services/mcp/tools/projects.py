"""Project-level and server-level read tools."""

from __future__ import annotations

from typing import Any

from services.mcp.app import mcp
from services.mcp.connection import api


@mcp.tool()
def list_projects(include_inactive: bool = False) -> dict[str, Any]:
    """List projects on the AYON server.

    Returns name, code and flags for each project. Use `get_project` for
    full detail (statuses, folder/task types, attributes) of one project.
    """
    # This exact field set keeps ayon_api on its light REST-list path;
    # anything beyond it makes ayon_api return full project entities.
    projects = list(api().get_projects(
        active=None if include_inactive else True,
        fields={"name", "code", "active", "createdAt"},
    ))
    return {"count": len(projects), "items": projects}


@mcp.tool()
def get_project(project_name: str) -> dict[str, Any]:
    """Get full detail of one project.

    Includes attributes (fps, resolution, frame ranges...), and the
    project anatomy lists: statuses, tags, folder types and task types.
    """
    project = api().get_project(project_name)
    if project is None:
        raise RuntimeError(
            f"Project {project_name!r} was not found on the server. "
            "Use list_projects to see available projects."
        )
    return project


@mcp.tool()
def get_server_info() -> dict[str, Any]:
    """Get AYON server version and the identity of the connected user.

    Useful as a connectivity check before other calls.
    """
    server = api()
    user = server.get_user()
    return {
        "server_url": server.base_url,
        "server_version": server.get_server_version(),
        "user_name": user.get("name"),
        "user_is_admin": user.get("data", {}).get("isAdmin", False),
        "user_is_service": user.get("data", {}).get("isService", False),
    }
