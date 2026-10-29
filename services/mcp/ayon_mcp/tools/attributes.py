"""Attribute definition tools."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from ayon_mcp.client import get_global_ayon_client as api

from .utils import _CamelModel

if TYPE_CHECKING:
    from ayon_api.utils import RestApiResponse

STUDIO_WIDE_NOTE = (
    "Attribute descriptions are studio-wide and apply to every project."
)


class AttributeDescriptionResult(_CamelModel):
    """Result of setting an attribute description."""
    attribute_name: str
    status: Literal["saved", "exists", "unchanged"] = Field(
        ...,
        description=(
            "saved: written. exists: not written, a description is already "
            "set (ask the user, then call again with overwrite=True). "
            "unchanged: same text, nothing written."
        ),
    )
    description: str | None = Field(
        None, description="Description now stored")
    previous_description: str | None = Field(
        None, description="Description before the call")
    title: str | None = Field(None, description="Title now stored")
    scope: list[str] = Field(
        default_factory=list,
        description="Entity types the attribute is on",
    )
    builtin: bool = False
    note: str = STUDIO_WIDE_NOTE


def _is_admin(user: dict | None) -> bool:
    """Mirror the server's ``UserEntity.is_admin``.

    Service users count as admins. In tunnel mode the client acts as the
    calling user (``x-as-user``), so this checks the caller, not the service.

    Returns:
        True if the user has admin rights.

    """
    data = (user or {}).get("data") or {}
    if data.get("isGuest"):
        return False
    return bool(data.get("isAdmin") or data.get("isService"))


def _admin_only_message(description: str) -> str:
    return (
        "Only admins can change attribute descriptions. Tell the user that "
        "an admin has to save it (Studio Settings → Attributes), and give "
        f'them the suggested text: "{description}".'
    )


def _not_found_message(attribute_name: str) -> str:
    return (
        f"Attribute '{attribute_name}' does not exist. Use the attribute "
        "name, not its title — get_attribute_list lists names and titles."
    )


def _server_error_message(attribute_name: str, detail: str) -> str:
    return (
        "AYON server error while saving the description of "
        f"'{attribute_name}': {detail}. Nothing was changed by this tool; "
        "try again later."
    )


def _check_response(
    response: RestApiResponse, attribute_name: str, description: str
) -> None:
    """Raise the tool's error message for a failed REST response.

    Raises:
        RuntimeError: For any non-2xx response.

    """
    if response.ok:
        return
    if response.status_code == HTTPStatus.FORBIDDEN:
        raise RuntimeError(_admin_only_message(description))
    if response.status_code == HTTPStatus.NOT_FOUND:
        raise RuntimeError(_not_found_message(attribute_name))
    raise RuntimeError(
        _server_error_message(attribute_name, response.detail))


def set_attribute_description(
    attribute_name: str,
    description: str,
    *,
    overwrite: bool = False,
    title: str | None = None,
) -> AttributeDescriptionResult:
    """Save what an attribute means.

    Set the description of a custom or built-in attribute (for example
    `bidDays`, `budget`) so people and AI agents understand it in reports,
    metrics and filters. Use this when the user explains an attribute that
    has no description, or asks to change its description. Report tools
    flag such attributes as missing a description. Prefer this over
    `patch_attribute_config`, which edits the whole attribute definition
    (type, scope, enum). The description is studio-wide: it applies to
    every project.

    Never write a description you inferred from the attribute name. Use the
    user's words, or a wording the user confirmed. If the attribute already
    has a description, the call does not change it and returns the current
    text (status "exists"). Show that text to the user and call again with
    `overwrite=True` only if they agree. Like every write tool it needs
    `confirm_mutation=true`, also when nothing ends up being written.
    Requires admin rights.

    Args:
        attribute_name: Attribute name as stored (camelCase, e.g.
            "bidDays"), not its title. See `get_attribute_list`.
        description: What the attribute means, in one or two sentences,
            including its unit if the user gave one (e.g. "Bid in man-days
            agreed with the client").
        overwrite: Replace an existing description. Only after the user
            has seen and approved replacing it.
        title: Human-readable label. Applied only when the attribute has
            no title yet.

    Returns:
        AttributeDescriptionResult: Status, stored and previous description.

    Raises:
        RuntimeError: Empty description, unknown attribute, missing admin
            rights or a server error. Nothing is written in those cases.

    """
    description = (description or "").strip()
    if not description:
        msg = (
            f"Description is empty. Ask the user what '{attribute_name}' "
            "means and pass their wording."
        )
        raise RuntimeError(msg)

    client = api()
    if not _is_admin(client.get_user()):
        raise RuntimeError(_admin_only_message(description))

    endpoint = f"attributes/{attribute_name}"
    response = client.raw_get(endpoint)
    _check_response(response, attribute_name, description)

    attribute = response.data
    data = attribute.get("data") or {}
    current_description = (data.get("description") or "").strip() or None
    current_title = (data.get("title") or "").strip() or None
    result = {
        "attribute_name": attribute_name,
        "previous_description": current_description,
        "title": current_title,
        "scope": attribute.get("scope") or [],
        "builtin": attribute.get("builtin", False),
    }

    if current_description == description:
        return AttributeDescriptionResult(
            status="unchanged", description=description, **result)
    if current_description and not overwrite:
        return AttributeDescriptionResult(
            status="exists", description=current_description, **result)

    payload = {"description": description}
    title = (title or "").strip()
    if title and not current_title:
        payload["title"] = title
        result["title"] = title

    response = client.raw_patch(endpoint, json={"data": payload})
    _check_response(response, attribute_name, description)

    return AttributeDescriptionResult(
        status="saved", description=description, **result)
