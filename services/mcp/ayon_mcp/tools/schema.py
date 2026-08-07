"""Schema discovery tools: attributes, settings schemas, GraphQL schema.

These tools expose the server's "vocabulary" so an assistant can learn
what fields exist and which values are valid before reading or writing
data, instead of guessing.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ayon_mcp.client import get_global_ayon_client as api
from ayon_mcp.openapi_spec import resolve_refs

from .utils import _CamelModel

_MAX_SEARCH_RESULTS = 100


# ---------------------------------------------------------------------------
# attributes
# ---------------------------------------------------------------------------


class AttributeInfo(_CamelModel):
    """One attribute definition, flattened for readability."""

    name: str
    scope: list[str] = Field(
        default_factory=list,
        description="Entity types the attribute applies to",
    )
    builtin: bool = False
    type: str = Field("", description="Value type, e.g. integer, string")
    title: str | None = None
    description: str | None = None
    enum: list[dict[str, Any]] | None = Field(
        None, description="Allowed values when the attribute is an enum")


class AttributeList(_CamelModel):
    """Result of a list_attributes call."""

    count: int
    items: list[AttributeInfo]


def list_attributes(scope: str | None = None) -> AttributeList:
    """List attribute definitions configured on the AYON server.

    Attributes are the typed fields behind every entity's `attrib` dict
    (fps, resolution, frameStart, ...). Check them before writing
    `attrib` values with `create_entity` / `update_entity`: the `name`
    is the key to use, `type` the expected value type, and `enum` the
    allowed values when present.

    Args:
        scope: Only attributes applying to this entity type, e.g.
            "folder", "task", "product", "version", "representation",
            "project", "user".

    Returns:
        AttributeList: Attribute definitions with name, scope, type,
        title, description and enum values.

    """
    data = api().get_attributes_schema()
    items = []
    for attribute in data.get("attributes", []):
        attr_scope = attribute.get("scope") or []
        if scope and scope not in attr_scope:
            continue
        attr_data = attribute.get("data") or {}
        items.append(AttributeInfo(
            name=attribute.get("name", ""),
            scope=attr_scope,
            builtin=attribute.get("builtin", False),
            type=attr_data.get("type", ""),
            title=attr_data.get("title"),
            description=attr_data.get("description"),
            enum=attr_data.get("enum"),
        ))
    return AttributeList(count=len(items), items=items)


# ---------------------------------------------------------------------------
# addon settings schema
# ---------------------------------------------------------------------------


def _summarize_property(name: str, prop: dict[str, Any]) -> dict[str, Any]:
    """Build the one-level summary of a single schema property.

    Args:
        name: Property name (used in the drill-down hint).
        prop: The property's schema node.

    Returns:
        A compact dict with type, title, defaults, enums and pointers
        for nested objects.

    """
    entry: dict[str, Any] = {}
    for key in ("type", "title", "description", "default", "enum"):
        if prop.get(key) is not None:
            entry[key] = prop[key]
    items = prop.get("items")
    if isinstance(items, dict) and items.get("type"):
        entry["items"] = {"type": items["type"]}
        if isinstance(items.get("properties"), dict):
            entry["items"]["properties"] = sorted(items["properties"])
    if isinstance(prop.get("properties"), dict):
        entry["properties"] = sorted(prop["properties"])
        entry["note"] = (
            "Nested object; drill down with "
            f"path=[..., {name!r}] for detail."
        )
    return entry


def _summarize_schema(node: dict[str, Any]) -> dict[str, Any]:
    """Return a one-level summary of a JSON schema object.

    Args:
        node: Resolved JSON schema node to summarize.

    Returns:
        A dict with the node's title/type/description and a compact
        per-property overview.

    """
    summary: dict[str, Any] = {}
    for key in ("title", "type", "description"):
        if node.get(key):
            summary[key] = node[key]
    properties = node.get("properties")
    if isinstance(properties, dict):
        summary["properties"] = {
            name: _summarize_property(name, prop)
            for name, prop in properties.items()
            if isinstance(prop, dict)
        }
    return summary


def get_addon_settings_schema(
    addon_name: str,
    addon_version: str,
    project_name: str | None = None,
    path: list[str] | None = None,
    *,
    full: bool = False,
) -> dict[str, Any]:
    """Get the JSON schema of an addon's settings.

    Use this before `set_addon_settings` to learn which keys exist,
    their types, defaults and enums, instead of guessing from a
    settings dump. By default returns a compact one-level summary;
    drill into nested objects with `path`, or set `full=True` for the
    raw schema of the selected node.

    Args:
        addon_name: Addon name, e.g. "core" (see `list_addons`).
        addon_version: Addon version, e.g. "1.2.3".
        project_name: Optional project for the project-level schema.
        path: Property names to descend into, e.g.
            ["publish", "ExtractReview"].
        full: Return the complete resolved schema of the selected node
            instead of a summary.

    Returns:
        dict: Schema summary (default) or full schema (`full=True`)
        of the selected node.

    Raises:
        RuntimeError: If a `path` element does not exist; the error
            lists the valid keys at that level.

    """
    schema = api().get_addon_settings_schema(
        addon_name, addon_version, project_name
    )
    node: Any = resolve_refs(schema, schema)
    for step in path or []:
        properties = node.get("properties") if isinstance(node, dict) else None
        if not isinstance(properties, dict) or step not in properties:
            available = sorted(properties) if properties else []
            msg = (
                f"Settings schema of {addon_name} {addon_version} has no "
                f"property {step!r} at path {path!r}. "
                f"Available keys here: {', '.join(available) or '(none)'}."
            )
            raise RuntimeError(msg)
        node = properties[step]
    if full:
        return node
    return _summarize_schema(node)


# ---------------------------------------------------------------------------
# GraphQL schema
# ---------------------------------------------------------------------------

_INTROSPECTION_QUERY = """
query IntrospectionQuery {
  __schema {
    queryType { name }
    types {
      kind
      name
      fields(includeDeprecated: false) {
        name
        args { name type { ...TypeRef } }
        type { ...TypeRef }
      }
      inputFields { name type { ...TypeRef } }
      enumValues(includeDeprecated: false) { name }
      possibleTypes { name }
    }
  }
}
fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType { kind name }
    }
  }
}
"""

_GRAPHQL_SCHEMA_CACHE: dict[str, Any] | None = None


def _fetch_graphql_schema() -> dict[str, Any]:
    """Fetch (and cache) the GraphQL schema via introspection.

    Returns:
        The ``__schema`` introspection document.

    Raises:
        RuntimeError: If the introspection query fails or returns no
            schema.

    """
    global _GRAPHQL_SCHEMA_CACHE  # noqa: PLW0603 - process-wide cache
    if _GRAPHQL_SCHEMA_CACHE is not None:
        return _GRAPHQL_SCHEMA_CACHE
    response = api().query_graphql(_INTROSPECTION_QUERY)
    if response.errors:
        messages = "; ".join(
            error.get("message", str(error)) for error in response.errors
        )
        msg = f"GraphQL introspection failed: {messages}"
        raise RuntimeError(msg)
    schema = response.data.get("data", {}).get("__schema") or None
    if schema is None:
        msg = "GraphQL introspection returned no schema."
        raise RuntimeError(msg)
    _GRAPHQL_SCHEMA_CACHE = schema
    return schema


def set_graphql_schema_cache(schema: dict[str, Any] | None) -> None:
    """Set or clear the cached GraphQL schema (used by tests).

    Args:
        schema: Introspection document to cache, or ``None`` to clear.

    """
    global _GRAPHQL_SCHEMA_CACHE  # noqa: PLW0603 - process-wide cache
    _GRAPHQL_SCHEMA_CACHE = schema


def _type_ref(type_node: dict[str, Any] | None) -> str:
    """Render a type reference like ``[FolderNode!]!``.

    Args:
        type_node: Introspection type reference (with ``ofType`` chain).

    Returns:
        The SDL rendering of the reference.

    """
    if not type_node:
        return "?"
    kind = type_node.get("kind")
    if kind == "NON_NULL":
        return f"{_type_ref(type_node.get('ofType'))}!"
    if kind == "LIST":
        return f"[{_type_ref(type_node.get('ofType'))}]"
    return str(type_node.get("name") or "?")


def _render_field(field: dict[str, Any]) -> str:
    """Render one field like ``tasks(first: Int): TasksConnection!``.

    Args:
        field: Introspection field object.

    Returns:
        The SDL rendering of the field.

    """
    args = field.get("args") or []
    rendered_args = ", ".join(
        f"{arg['name']}: {_type_ref(arg.get('type'))}" for arg in args
    )
    signature = f"({rendered_args})" if rendered_args else ""
    return f"{field['name']}{signature}: {_type_ref(field.get('type'))}"


def _render_type(type_node: dict[str, Any]) -> str:
    """Render one type as a compact SDL-like block.

    Args:
        type_node: Introspection type object.

    Returns:
        The SDL rendering of the whole type.

    """
    kind = type_node.get("kind", "")
    name = type_node.get("name", "")
    if kind == "ENUM":
        values = ", ".join(
            value["name"] for value in type_node.get("enumValues") or []
        )
        return f"enum {name} {{ {values} }}"
    if kind == "UNION":
        possible = " | ".join(
            possible_type["name"]
            for possible_type in type_node.get("possibleTypes") or []
        )
        return f"union {name} = {possible}"
    if kind == "SCALAR":
        return f"scalar {name}"
    if kind == "INPUT_OBJECT":
        lines = [
            f"  {input_field['name']}: {_type_ref(input_field.get('type'))}"
            for input_field in type_node.get("inputFields") or []
        ]
        return f"input {name} {{\n" + "\n".join(lines) + "\n}"
    keyword = "interface" if kind == "INTERFACE" else "type"
    lines = [
        f"  {_render_field(field)}"
        for field in type_node.get("fields") or []
    ]
    return f"{keyword} {name} {{\n" + "\n".join(lines) + "\n}"


def _named_types(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Return schema types without GraphQL-internal ``__*`` types.

    Args:
        schema: Introspection document.

    Returns:
        The user-facing type objects.

    """
    return [
        type_node
        for type_node in schema.get("types") or []
        if type_node.get("name") and not type_node["name"].startswith("__")
    ]


def _render_named_type(
    types: list[dict[str, Any]], type_name: str,
) -> str | None:
    """Render one named type when it exists.

    Args:
        types: User-facing type objects.
        type_name: Requested type name (case-insensitive).

    Returns:
        The SDL rendering of the matching type, or None when no type
        matches.

    """
    wanted = type_name.strip().lower()
    for type_node in types:
        if type_node["name"].lower() == wanted:
            return _render_type(type_node)
    return None


def _search_types(types: list[dict[str, Any]], search: str) -> str:
    """Find types and fields whose name contains a substring.

    Args:
        types: User-facing type objects.
        search: Case-insensitive substring.

    Returns:
        Matching type and field lines, truncated when too many.

    """
    needle = search.strip().lower()
    lines: list[str] = []
    for type_node in types:
        if needle in type_node["name"].lower():
            lines.append(f"{type_node['kind']}: {type_node['name']}")
        lines.extend(
            f"{type_node['name']}.{_render_field(field)}"
            for field in type_node.get("fields") or []
            if needle in field["name"].lower()
        )
    if not lines:
        return f"No types or fields match {search!r}."
    truncated = len(lines) > _MAX_SEARCH_RESULTS
    lines = lines[:_MAX_SEARCH_RESULTS]
    result = "\n".join(lines)
    if truncated:
        result += "\n... (truncated, refine the search)"
    return result


def _schema_overview(
    schema: dict[str, Any], types: list[dict[str, Any]],
) -> str:
    """Render the query root plus a grouped list of type names.

    Args:
        schema: Introspection document.
        types: User-facing type objects.

    Returns:
        The overview text.

    """
    query_type_name = (schema.get("queryType") or {}).get("name", "Query")
    query_block = ""
    for type_node in types:
        if type_node["name"] == query_type_name:
            query_block = _render_type(type_node)
            break
    grouped: dict[str, list[str]] = {}
    for type_node in types:
        if type_node["name"] == query_type_name:
            continue
        grouped.setdefault(type_node.get("kind", "?"), []).append(
            type_node["name"]
        )
    overview_lines = [query_block, ""]
    overview_lines.extend(
        f"{kind}: {', '.join(sorted(grouped[kind]))}"
        for kind in sorted(grouped)
    )
    overview_lines.append(
        "\nUse get_graphql_schema(type_name=...) for a full definition."
    )
    return "\n".join(overview_lines)


def get_graphql_schema(
    type_name: str | None = None,
    search: str | None = None,
) -> str:
    """Explore the AYON GraphQL schema before writing `query_graphql`.

    Without arguments, returns the query root (all entry points with
    their arguments) plus an overview of available type names. Use
    `type_name` to get one type's full definition, or `search` to find
    types and fields by substring.

    Args:
        type_name: Exact type name to render, e.g. "FolderNode"
            (case-insensitive).
        search: Case-insensitive substring matched against type and
            field names.

    Returns:
        str: SDL-like schema text.

    Raises:
        RuntimeError: If `type_name` does not exist; the error lists
            close matches when available.

    """
    schema = _fetch_graphql_schema()
    types = _named_types(schema)
    if type_name:
        rendered = _render_named_type(types, type_name)
        if rendered is None:
            wanted = type_name.strip().lower()
            close = [
                t["name"] for t in types if wanted in t["name"].lower()
            ]
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            msg = f"GraphQL type {type_name!r} was not found.{hint}"
            raise RuntimeError(msg)
        return rendered
    if search:
        return _search_types(types, search)
    return _schema_overview(schema, types)
