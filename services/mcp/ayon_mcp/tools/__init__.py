"""Tool modules."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .entities import (
    get_entity,
    get_folder_hierarchy,
    list_folders,
    list_products,
    list_representations,
    list_tasks,
    list_versions,
    query_graphql,
)
from .events import (
    dispatch_event,
    get_event,
    list_events,
)
from .projects import (
    get_project,
    get_server_info,
    list_projects,
)
from .rest import (
    call_rest_endpoint,
    get_rest_endpoint,
    list_rest_endpoints,
)
from .settings import (
    get_addon_settings,
    list_addons,
    list_bundles,
    set_addon_settings,
)
from .write import (
    add_comment,
    create_entity,
    delete_entity,
    update_entity,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


ALL_TOOLS: Sequence[Callable] = [  # ruff: ignore[non-empty-init-module]
    # entities
    get_folder_hierarchy,
    list_folders,
    list_tasks,
    list_products,
    list_versions,
    list_representations,
    get_entity,
    query_graphql,

    # events
    list_events,
    get_event,
    dispatch_event,

    # projects
    get_project,
    get_server_info,
    list_projects,

    # settings
    get_addon_settings,
    list_addons,
    list_bundles,
    set_addon_settings,

    # write
    create_entity,
    update_entity,
    delete_entity,
    add_comment,

    # rest gateway
    list_rest_endpoints,
    get_rest_endpoint,
    call_rest_endpoint,
]

# MCP tool annotations (hints only — clients use them for permission
# UX; they are not a security boundary). AYON is a closed domain, so
# openWorldHint is False everywhere. The GraphQL API is query-only,
# which makes `query_graphql` a read tool.
_READ: dict[str, Any] = {  # ruff: ignore[non-empty-init-module]
    "readOnlyHint": True, "openWorldHint": False,
}


def _write_hints(  # ruff: ignore[non-empty-init-module]
    *, destructive: bool, idempotent: bool,
) -> dict[str, Any]:
    return {
        "readOnlyHint": False,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": False,
    }


TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {  # ruff: ignore[non-empty-init-module]
    # entities (read)
    "get_folder_hierarchy": _READ,
    "list_folders": _READ,
    "list_tasks": _READ,
    "list_products": _READ,
    "list_versions": _READ,
    "list_representations": _READ,
    "get_entity": _READ,
    "query_graphql": _READ,

    # events
    "list_events": _READ,
    "get_event": _READ,
    "dispatch_event": _write_hints(destructive=False, idempotent=False),

    # projects (read)
    "get_project": _READ,
    "get_server_info": _READ,
    "list_projects": _READ,

    # settings
    "get_addon_settings": _READ,
    "list_addons": _READ,
    "list_bundles": _READ,
    "set_addon_settings": _write_hints(destructive=True, idempotent=True),

    # write
    "create_entity": _write_hints(destructive=False, idempotent=False),
    "update_entity": _write_hints(destructive=True, idempotent=True),
    "delete_entity": _write_hints(destructive=True, idempotent=True),
    "add_comment": _write_hints(destructive=False, idempotent=False),

    # rest gateway (call_rest_endpoint can reach write endpoints; in
    # read-only mode it is restricted to GET/HEAD at runtime)
    "list_rest_endpoints": _READ,
    "get_rest_endpoint": _READ,
    "call_rest_endpoint": _write_hints(destructive=True, idempotent=False),
}

__all__ = [
    "ALL_TOOLS",
    "TOOL_ANNOTATIONS",
    "add_comment",
    "call_rest_endpoint",
    "create_entity",
    "delete_entity",
    "dispatch_event",
    "get_addon_settings",
    "get_entity",
    "get_event",
    "get_folder_hierarchy",
    "get_project",
    "get_rest_endpoint",
    "get_server_info",
    "list_addons",
    "list_bundles",
    "list_events",
    "list_folders",
    "list_products",
    "list_projects",
    "list_representations",
    "list_rest_endpoints",
    "list_tasks",
    "list_versions",
    "query_graphql",
    "set_addon_settings",
    "update_entity",
]
