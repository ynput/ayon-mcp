"""Tool modules."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

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
]


def openapi_tools_enabled() -> bool:  # ruff: ignore[non-empty-init-module]
    """Return True if generated OpenAPI tools should be registered."""
    value = (os.getenv("AYON_MCP_ENABLE_OPENAPI_TOOLS", "true") or "").strip()
    return value.lower() not in {"0", "false", "no", "off"}


if openapi_tools_enabled():  # ruff: ignore[non-empty-init-module]
    try:
        from .openapi_generated import ALL_OPENAPI_TOOLS
    except ImportError:
        ALL_OPENAPI_TOOLS = []
    ALL_TOOLS = [*ALL_TOOLS, *ALL_OPENAPI_TOOLS]

__all__ = [
    "ALL_TOOLS",
    "add_comment",
    "create_entity",
    "delete_entity",
    "dispatch_event",
    "get_addon_settings",
    "get_entity",
    "get_event",
    "get_folder_hierarchy",
    "get_project",
    "get_server_info",
    "list_addons",
    "list_bundles",
    "list_events",
    "list_folders",
    "list_products",
    "list_projects",
    "list_representations",
    "list_tasks",
    "list_versions",
    "query_graphql",
    "set_addon_settings",
    "update_entity",
]
