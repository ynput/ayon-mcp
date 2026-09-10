"""Generic REST gateway tools covering the full AYON server API.

Three tools give access to every server endpoint without registering a
tool per endpoint: discover endpoints from the OpenAPI spec, inspect one
endpoint's schemas, then call it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from ayon_mcp.openapi_spec import (
    get_openapi_spec,
    iter_operations,
    resolve_refs,
)
from ayon_mcp.rest_client import get_global_rest_client
from ayon_mcp.utils import read_only_enabled

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
_READ_METHODS = {"GET", "HEAD"}
_MAX_ERROR_BODY_CHARS = 2000


class RestEndpointItem(BaseModel):
    """Compact description of one REST endpoint."""

    method: str = Field(..., description="Uppercase HTTP method")
    path: str = Field(
        ..., description="Path template, e.g. /api/projects/{project_name}")
    summary: str = Field("", description="Short endpoint summary")
    tag: str = Field("", description="Endpoint group tag")
    deprecated: bool = Field(
        default=False, description="Endpoint is deprecated")


class RestTagSummary(BaseModel):
    """Endpoint count for one tag (endpoint group)."""

    name: str = Field(..., description="Tag name to filter by")
    endpoint_count: int = Field(
        ..., description="Number of endpoints with this tag")


class RestEndpointList(BaseModel):
    """Result of a REST endpoint discovery call."""

    count: int = Field(..., description="Number of matching endpoints")
    items: list[RestEndpointItem] = Field(
        default_factory=list, description="Matching endpoints")
    tags: list[RestTagSummary] = Field(
        default_factory=list,
        description="Tag overview (populated when no filter was given)")
    note: str | None = Field(
        None, description="Hint about how to narrow the listing")


def _operation_tag(operation: dict[str, Any]) -> str:
    tags = operation.get("tags")
    if isinstance(tags, list) and tags:
        return str(tags[0])
    return ""


async def list_rest_endpoints(
    *,
    search: str | None = None,
    tag: str | None = None,
    method: str | None = None,
) -> RestEndpointList:
    """Discover AYON REST API endpoints from the server's OpenAPI spec.

    Called without arguments it returns an overview of endpoint tags
    (groups) with counts; call it again with `tag` or `search` to list
    the matching endpoints. Use `get_rest_endpoint` next to inspect the
    parameters and schemas of one endpoint.

    Args:
        search: Case-insensitive substring matched against path, summary,
            description, operation id and tag.
        tag: Only endpoints with this tag (see the unfiltered overview).
        method: Only endpoints with this HTTP method (e.g. "GET").

    Returns:
        RestEndpointList: Matching endpoints, or a tag overview when no
        filter was given.

    """
    spec = await get_openapi_spec()
    operations = list(iter_operations(spec))

    if not search and not tag and not method:
        tag_counts: dict[str, int] = {}
        for _, _, operation in operations:
            name = _operation_tag(operation) or "(untagged)"
            tag_counts[name] = tag_counts.get(name, 0) + 1
        return RestEndpointList(
            count=len(operations),
            tags=[
                RestTagSummary(name=name, endpoint_count=count)
                for name, count in sorted(tag_counts.items())
            ],
            note=(
                "Call again with `tag` or `search` to list the "
                "matching endpoints."
            ),
        )

    method_filter = (method or "").strip().lower()
    tag_filter = (tag or "").strip().lower()
    search_filter = (search or "").strip().lower()

    items: list[RestEndpointItem] = []
    for op_method, path, operation in operations:
        if method_filter and op_method != method_filter:
            continue
        op_tag = _operation_tag(operation)
        if tag_filter and op_tag.lower() != tag_filter:
            continue
        if search_filter:
            haystack = " ".join((
                path,
                str(operation.get("operationId", "")),
                str(operation.get("summary", "")),
                str(operation.get("description", "")),
                op_tag,
            )).lower()
            if search_filter not in haystack:
                continue
        items.append(RestEndpointItem(
            method=op_method.upper(),
            path=path,
            summary=str(operation.get("summary", "")),
            tag=op_tag,
            deprecated=bool(operation.get("deprecated", False)),
        ))

    return RestEndpointList(count=len(items), items=items)


async def get_rest_endpoint(method: str, path: str) -> dict[str, Any]:
    """Get parameters and schemas of one AYON REST API endpoint.

    Returns the endpoint's parameters, request body schema and response
    schemas with `$ref` references inlined, ready to build a
    `call_rest_endpoint` request from.

    Args:
        method: HTTP method of the endpoint (e.g. "GET").
        path: Path template exactly as returned by `list_rest_endpoints`
            (e.g. "/api/projects/{project_name}").

    Returns:
        dict: Endpoint detail with parameters, requestBody and responses.

    Raises:
        RuntimeError: If the endpoint is not present in the OpenAPI spec.

    """
    spec = await get_openapi_spec()
    path_item = spec.get("paths", {}).get(path)
    operation = None
    if isinstance(path_item, dict):
        operation = path_item.get(method.strip().lower())
    if not isinstance(operation, dict):
        msg = (
            f"Endpoint {method.upper()} {path!r} was not found in the "
            "OpenAPI spec. Use list_rest_endpoints to discover available "
            "endpoints; `path` must match the path template exactly."
        )
        raise RuntimeError(msg)

    parameters = [
        *path_item.get("parameters", []),
        *operation.get("parameters", []),
    ]

    detail: dict[str, Any] = {
        "method": method.upper(),
        "path": path,
        "operationId": operation.get("operationId", ""),
        "summary": operation.get("summary", ""),
        "description": operation.get("description", ""),
        "tags": operation.get("tags", []),
        "deprecated": bool(operation.get("deprecated", False)),
        "parameters": resolve_refs(parameters, spec),
    }
    if "requestBody" in operation:
        detail["requestBody"] = resolve_refs(operation["requestBody"], spec)
    if "responses" in operation:
        detail["responses"] = resolve_refs(operation["responses"], spec)
    return detail


async def call_rest_endpoint(
    method: str,
    path: str,
    *,
    path_params: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
    body: Any | None = None,
) -> Any:
    """Call an AYON REST API endpoint and return its response.

    Use `list_rest_endpoints` and `get_rest_endpoint` first to find the
    endpoint and its schemas. POST/PUT/PATCH/DELETE endpoints modify
    production data — call them only when explicitly asked to change
    something.

    Args:
        method: HTTP method (GET, POST, PUT, PATCH, DELETE or HEAD).
        path: Path template (e.g. "/api/projects/{project_name}");
            placeholders are filled from `path_params`.
        path_params: Values for the placeholders in `path`.
        query_params: Query string parameters.
        body: JSON request body for methods that accept one.

    Returns:
        Parsed JSON response, response text, or None for empty responses.

    Raises:
        RuntimeError: If the method is unsupported, a path placeholder is
            missing, or the server responds with an error status.

    """
    normalized_method = method.strip().upper()
    if normalized_method not in _ALLOWED_METHODS:
        msg = (
            f"Unsupported HTTP method {method!r}. "
            f"Supported methods: {', '.join(sorted(_ALLOWED_METHODS))}."
        )
        raise RuntimeError(msg)

    if read_only_enabled() and normalized_method not in _READ_METHODS:
        msg = (
            f"This server runs in read-only mode: {normalized_method} "
            "requests are not allowed. Only GET and HEAD are permitted."
        )
        raise RuntimeError(msg)

    encoded_params = {
        key: quote(str(value), safe="")
        for key, value in (path_params or {}).items()
    }
    try:
        resolved_path = path.format(**encoded_params)
    except (KeyError, IndexError) as exc:
        msg = (
            f"Path template {path!r} has placeholders not covered by "
            f"path_params ({exc}). Provide every placeholder value in "
            "path_params."
        )
        raise RuntimeError(msg) from exc

    try:
        return await get_global_rest_client().request(
            method=normalized_method,
            path=resolved_path,
            params={
                key: value
                for key, value in (query_params or {}).items()
                if value is not None
            },
            json=body,
        )
    except httpx.HTTPStatusError as exc:
        response_body = exc.response.text[:_MAX_ERROR_BODY_CHARS]
        msg = (
            f"{normalized_method} {resolved_path} failed with status "
            f"{exc.response.status_code}: {response_body}"
        )
        raise RuntimeError(msg) from exc
