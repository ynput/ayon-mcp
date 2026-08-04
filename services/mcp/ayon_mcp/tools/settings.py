"""Addon, bundle and settings tools."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ayon_mcp.client import get_global_ayon_client as api

from .utils import _CamelModel


class AddonItem(_CamelModel):
    """Addon item with name, title and available versions."""
    name: str
    title: str | None = None
    versions: list[str] = Field(default_factory=list)


class AddonItemsList(_CamelModel):
    """Generic list of addon items."""
    count: int = Field(..., description="Number of items in the list")
    items: list[AddonItem] = Field(..., description="List of addon items")


class BundleItem(_CamelModel):
    """Bundle item with name, flags and addon versions."""
    name: str
    is_production: bool = Field(
        default=False, description="Bundle is production")
    is_staging: bool = Field(
        default=False, description="Bundle is staging")
    is_dev: bool = Field(
        default=False, description="Bundle is development")
    addons: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of addon names to versions in the bundle",
    )


class SavedSettingsResponse(_CamelModel):
    """Response for saved settings."""
    addon: str
    level: str
    variant: str
    saved: bool = Field(..., description="Settings were saved successfully")


class BundleItemsList(_CamelModel):
    """Generic list of bundle items."""
    count: int = Field(..., description="Number of items in the list")
    items: list[BundleItem] = Field(..., description="List of bundle items")


def list_addons() -> AddonItemsList:
    """List addons installed on the server.

    Includes their available versions.

    Returns:
        AddonItemsList: List of addons with name, title and versions.

    """
    info = api().get_addons_info(details=True)
    addons = [{
            "name": addon.get("name"),
            "title": addon.get("title"),
            "versions": sorted((addon.get("versions") or {}).keys()),
        } for addon in info.get("addons", [])]
    return AddonItemsList(count=len(addons), items=addons)


def list_bundles() -> BundleItemsList:
    """List server bundles and which one is production/staging.

    A bundle pins one version of each addon; settings variants
    ("production", "staging") resolve addon versions through bundles.

    Returns:
        BundleItemsList: List of bundles with name,
            flags and addon versions.

    """
    data = api().get_bundles()
    bundles = [{
            "name": bundle.get("name"),
            "is_production": bundle.get("isProduction", False),
            "is_staging": bundle.get("isStaging", False),
            "is_dev": bundle.get("isDev", False),
            "addons": bundle.get("addons") or {},
        } for bundle in data.get("bundles", [])]
    return BundleItemsList(count=len(bundles), items=bundles)


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

    Returns:
        dict: Resolved settings object matching the addon schema.

    """
    return api().get_addon_settings(
        addon_name,
        addon_version,
        project_name=project_name,
        variant=variant,
        use_site=False,
    )


def set_addon_settings(
    addon_name: str,
    addon_version: str,
    settings: dict[str, Any],
    project_name: str | None = None,
    variant: str = "production",
) -> SavedSettingsResponse:
    """Set settings overrides of an addon version.

    Warning:
        The given object REPLACES all existing overrides at that
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

    Returns:
        SavedSettingsResponse: Confirmation of saved settings.

    """
    endpoint = f"addons/{addon_name}/{addon_version}/settings"
    if project_name:
        endpoint += f"/{project_name}"
    endpoint += f"?variant={variant}"
    response = api().raw_post(endpoint, json=settings)
    response.raise_for_status()
    return SavedSettingsResponse(
        addon=f"{addon_name} {addon_version}",
        level=f"project:{project_name}" if project_name else "studio",
        variant=variant,
        saved=True,
    )
