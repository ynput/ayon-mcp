"""Addon, bundle and settings tools."""

from __future__ import annotations

from typing import Any

from services.mcp.app import mcp
from services.mcp.connection import api


@mcp.tool()
def list_addons() -> dict[str, Any]:
    """List addons installed on the server with their available versions."""
    info = api().get_addons_info(details=True)
    addons = []
    for addon in info.get("addons", []):
        addons.append({
            "name": addon.get("name"),
            "title": addon.get("title"),
            "versions": sorted((addon.get("versions") or {}).keys()),
        })
    return {"count": len(addons), "items": addons}


@mcp.tool()
def list_bundles() -> dict[str, Any]:
    """List server bundles and which one is production/staging.

    A bundle pins one version of each addon; settings variants
    ("production", "staging") resolve addon versions through bundles.
    """
    data = api().get_bundles()
    bundles = []
    for bundle in data.get("bundles", []):
        bundles.append({
            "name": bundle.get("name"),
            "is_production": bundle.get("isProduction", False),
            "is_staging": bundle.get("isStaging", False),
            "is_dev": bundle.get("isDev", False),
            "addons": bundle.get("addons") or {},
        })
    return {"count": len(bundles), "items": bundles}


@mcp.tool()
def get_addon_settings(
    addon_name: str,
    addon_version: str,
    project_name: str | None = None,
    variant: str = "production",
) -> dict[str, Any]:
    """Get resolved settings of an addon version.

    Without `project_name` returns studio-level settings; with it,
    project settings (studio settings + project overrides).

    Args:
        addon_name: Addon name, e.g. "core", "maya" (see `list_addons`).
        addon_version: Addon version, e.g. "1.2.3".
        project_name: Optional project for project-level settings.
        variant: Settings variant, "production" or "staging".
    """
    return api().get_addon_settings(
        addon_name,
        addon_version,
        project_name=project_name,
        variant=variant,
        use_site=False,
    )


@mcp.tool()
def set_addon_settings(
    addon_name: str,
    addon_version: str,
    settings: dict[str, Any],
    project_name: str | None = None,
    variant: str = "production",
) -> dict[str, Any]:
    """Set settings overrides of an addon version.

    WARNING: the given object REPLACES all existing overrides at that
    level (studio, or project when `project_name` is given). To change
    a single value safely: call `get_addon_settings`, take the current
    values you want to keep as overrides, modify, then submit here.
    Only keys present in `settings` become overrides; everything else
    reverts to defaults (studio level) or studio values (project level).

    Args:
        addon_name: Addon name (see `list_addons`).
        addon_version: Addon version.
        settings: Settings override object matching the addon schema.
        project_name: Optional project for project-level overrides.
        variant: Settings variant, "production" or "staging".
    """
    endpoint = f"addons/{addon_name}/{addon_version}/settings"
    if project_name:
        endpoint += f"/{project_name}"
    endpoint += f"?variant={variant}"
    response = api().raw_post(endpoint, json=settings)
    response.raise_for_status()
    return {
        "addon": f"{addon_name} {addon_version}",
        "level": f"project:{project_name}" if project_name else "studio",
        "variant": variant,
        "saved": True,
    }
