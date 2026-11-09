"""Tests for the set_attribute_description tool."""

from __future__ import annotations

from contextvars import copy_context
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from chuk_tool_processor.guards import SideEffectClass

from ayon_mcp.client import set_global_ayon_client
from ayon_mcp.tool_discovery import (
    AyonDynamicToolProvider,
    create_curated_tools,
    drop_mutating_tools,
    tool_side_effect,
)
from ayon_mcp.tools.attributes import set_attribute_description

ADMIN = {"name": "admin", "data": {"isAdmin": True}}


def _response(status: int = 200, data: Any = None, detail: str = ""):
    return SimpleNamespace(
        status_code=status,
        ok=200 <= status < 300,
        data=data if data is not None else {},
        detail=detail,
    )


def _attribute(description: str | None = None, title: str | None = None):
    return {
        "name": "bidDays",
        "scope": ["task"],
        "builtin": False,
        "data": {"type": "integer", "title": title, "description": description},
    }


def _client(
    *,
    user: dict | None = None,
    get: Any = None,
    patch: Any = None,
) -> MagicMock:
    client = MagicMock()
    client.get_user.return_value = ADMIN if user is None else user
    client.raw_get.return_value = (
        get if get is not None else _response(data=_attribute()))
    client.raw_patch.return_value = (
        patch if patch is not None else _response(204))
    return client


def _call(client: MagicMock, **kwargs: Any):
    kwargs.setdefault("attribute_name", "bidDays")
    kwargs.setdefault("description", "Man-days bid to the client.")

    def _run():
        set_global_ayon_client(client)
        return set_attribute_description(**kwargs)

    return copy_context().run(_run)


def test_saves_missing_description_and_patches_only_description() -> None:
    client = _client()

    result = _call(client, description="  Man-days bid to the client.  ")

    assert result.status == "saved"
    assert result.description == "Man-days bid to the client."
    assert result.previous_description is None
    assert result.scope == ["task"]
    client.raw_patch.assert_called_once_with(
        "attributes/bidDays",
        json={"data": {"description": "Man-days bid to the client."}},
    )


def test_whitespace_description_counts_as_missing() -> None:
    client = _client(get=_response(data=_attribute(description="   ")))

    assert _call(client).status == "saved"
    client.raw_patch.assert_called_once()


def test_existing_description_is_not_overwritten_without_flag() -> None:
    client = _client(get=_response(data=_attribute(description="Old text")))

    result = _call(client)

    assert result.status == "exists"
    assert result.previous_description == "Old text"
    assert result.description == "Old text"
    client.raw_patch.assert_not_called()


def test_existing_description_is_overwritten_with_flag() -> None:
    client = _client(get=_response(data=_attribute(description="Old text")))

    result = _call(client, overwrite=True)

    assert result.status == "saved"
    assert result.previous_description == "Old text"
    client.raw_patch.assert_called_once()


def test_same_description_makes_no_patch() -> None:
    client = _client(get=_response(
        data=_attribute(description="Man-days bid to the client.")))

    result = _call(client, overwrite=True)

    assert result.status == "unchanged"
    client.raw_patch.assert_not_called()


def test_title_is_set_only_when_missing() -> None:
    client = _client()
    result = _call(client, title="Bid Days")
    assert result.title == "Bid Days"
    assert client.raw_patch.call_args.kwargs["json"]["data"]["title"] == (
        "Bid Days")

    client = _client(get=_response(data=_attribute(title="Bid")))
    result = _call(client, title="Bid Days")
    assert result.title == "Bid"
    assert "title" not in client.raw_patch.call_args.kwargs["json"]["data"]


def test_empty_description_is_rejected_before_any_call() -> None:
    client = _client()

    with pytest.raises(RuntimeError, match="Description is empty"):
        _call(client, description="   ")

    client.get_user.assert_not_called()


@pytest.mark.parametrize(
    "user",
    [
        {"name": "artist", "data": {}},
        {"name": "manager", "data": {"isManager": True}},
        {"name": "guest", "data": {"isAdmin": True, "isGuest": True}},
    ],
)
def test_non_admin_is_refused_without_reading_or_writing(user) -> None:
    client = _client(user=user)

    with pytest.raises(RuntimeError, match="Only admins") as exc:
        _call(client)

    assert "Man-days bid to the client." in str(exc.value)
    client.raw_get.assert_not_called()
    client.raw_patch.assert_not_called()


def test_service_user_counts_as_admin() -> None:
    client = _client(user={"name": "ayon-mcp", "data": {"isService": True}})

    assert _call(client).status == "saved"


def test_unknown_attribute_points_to_attribute_list() -> None:
    client = _client(get=_response(404, detail="Attribute bid not found"))

    with pytest.raises(RuntimeError, match="get_attribute_list"):
        _call(client, attribute_name="bid")

    client.raw_patch.assert_not_called()


def test_server_403_on_patch_maps_to_admin_message() -> None:
    client = _client(patch=_response(403, detail="Forbidden"))

    with pytest.raises(RuntimeError, match="Only admins"):
        _call(client)


def test_server_error_on_patch_says_nothing_changed() -> None:
    client = _client(patch=_response(500, detail="boom"))

    with pytest.raises(RuntimeError, match="Nothing was changed.*") as exc:
        _call(client)

    assert "boom" in str(exc.value)


def test_tool_is_a_write_and_hidden_in_read_only_mode() -> None:
    assert tool_side_effect(set_attribute_description) is (
        SideEffectClass.WRITE)
    assert drop_mutating_tools([set_attribute_description]) == []


async def patch_attribute_config(*, attribute_name: Any, body: Any) -> Any:
    """Patch Attribute Config

    Endpoint: PATCH /api/attributes/{attribute_name}

    Partially update attribute configuration
    """
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["describe attribute", "attribute description"],
)
async def test_ranks_above_generic_patch_for_description_queries(query):
    # Same order as ALL_TOOLS: curated tools before generated ones. On a
    # name-score tie ("describe attribute") the stable sort keeps that order.
    provider = AyonDynamicToolProvider(create_curated_tools(
        [set_attribute_description, patch_attribute_config]))

    results = await provider.search_tools(query)

    assert results[0]["name"] == "set_attribute_description"
