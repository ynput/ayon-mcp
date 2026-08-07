"""OpenAPI sync and MCP tool code-generation utilities."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import keyword
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

from .rest_client import RestApiClient

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


@dataclass
class Parameter:
    """Represents a parameter in an OpenAPI operation."""

    original_name: str
    py_name: str
    location: str
    required: bool


@dataclass
class Operation:
    """Represents an OpenAPI operation (endpoint) with its details."""

    operation_id: str
    method: str
    path: str
    summary: str
    description: str
    group: str
    path_params: list[Parameter]
    query_params: list[Parameter]
    header_params: list[Parameter]
    cookie_params: list[Parameter]
    request_body_required: bool
    request_body_content_type: str | None


def _to_snake(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = value.strip("_").lower()
    if not value:
        value = "value"
    if value[0].isdigit():
        value = f"v_{value}"
    if keyword.iskeyword(value):
        value = f"{value}_"
    return value


def _sanitize_group(path: str) -> str:
    parts = [part for part in path.strip("/").split("/") if part]
    if not parts:
        return "root"
    if parts[0] == "api":
        if len(parts) == 1:
            return "api"
        group = parts[1]
    else:
        group = parts[0]
    group = group.strip("{}")
    return _to_snake(group)


def _resolve_ref(ref: str, spec: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        return {}
    node: Any = spec
    for token in ref[2:].split("/"):
        if not isinstance(node, dict):
            return {}
        node = node.get(token)
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def _iter_parameters(
    raw_parameters: list[dict[str, Any]],
    spec: dict[str, Any],
) -> Generator[dict[str, Any], None, None]:
    for item in raw_parameters:
        if "$ref" in item:
            resolved = _resolve_ref(item["$ref"], spec)
            if resolved:
                yield resolved
            continue
        yield item


def _choose_body_content_type(
    request_body: dict[str, Any] | None,
) -> str | None:
    if not request_body:
        return None
    content = request_body.get("content") or {}
    if "application/json" in content:
        return "application/json"
    for preferred in (
        "application/x-www-form-urlencoded",
        "multipart/form-data",
        "text/plain",
    ):
        if preferred in content:
            return preferred
    for key in content:
        return key
    return None


def _unique_py_name(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _collect_operations(spec: dict[str, Any]) -> list[Operation]:
    paths = spec.get("paths") or {}
    operations: list[Operation] = []

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        path_level_parameters = list(path_item.get("parameters") or [])

        for method, operation in path_item.items():
            method_lower = str(method).lower()
            if method_lower not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                continue

            operation_id = operation.get("operationId") or _to_snake(
                f"{method_lower}_{path}"
            )
            operation_id = _to_snake(operation_id)

            summary = str(operation.get("summary") or "").strip()
            description = str(operation.get("description") or "").strip()

            used_names: set[str] = set()
            path_params: list[Parameter] = []
            query_params: list[Parameter] = []
            header_params: list[Parameter] = []
            cookie_params: list[Parameter] = []

            merged_parameters = [
                *path_level_parameters,
                *(operation.get("parameters") or []),
            ]
            for raw_param in _iter_parameters(merged_parameters, spec):
                location = str(raw_param.get("in") or "query")
                original_name = str(raw_param.get("name") or "param")
                py_base = _to_snake(original_name)
                py_name = _unique_py_name(py_base, used_names)
                param = Parameter(
                    original_name=original_name,
                    py_name=py_name,
                    location=location,
                    required=bool(raw_param.get("required", False)),
                )
                if location == "path":
                    param.required = True
                    path_params.append(param)
                elif location == "header":
                    header_params.append(param)
                elif location == "cookie":
                    cookie_params.append(param)
                else:
                    query_params.append(param)

            request_body = operation.get("requestBody")
            if isinstance(request_body, dict) and "$ref" in request_body:
                request_body = _resolve_ref(request_body["$ref"], spec)
            body_required = bool((request_body or {}).get("required", False))
            body_content_type = _choose_body_content_type(request_body)

            operations.append(
                Operation(
                    operation_id=operation_id,
                    method=method_lower.upper(),
                    path=path,
                    summary=summary,
                    description=description,
                    group=_sanitize_group(path),
                    path_params=path_params,
                    query_params=query_params,
                    header_params=header_params,
                    cookie_params=cookie_params,
                    request_body_required=body_required,
                    request_body_content_type=body_content_type,
                )
            )

    return operations


def _build_docstring(op: Operation) -> str:
    parts = []
    if op.summary:
        parts.append(op.summary)
    else:
        parts.append(f"{op.method} {op.path}")
    parts.extend(("", f"Endpoint: {op.method} {op.path}"))
    if op.description:
        parts.extend(("", op.description))
    return "\n".join(parts)


def _build_function_source(op: Operation) -> str:
    signature_parts: list[str] = []
    for param in (
        op.path_params
        + op.query_params
        + op.header_params
        + op.cookie_params
    ):
        if param.required:
            signature_parts.append(f"{param.py_name}: Any")
        else:
            signature_parts.append(f"{param.py_name}: Any | None = None")

    if op.request_body_content_type is not None:
        if op.request_body_required:
            signature_parts.append("body: Any")
        else:
            signature_parts.append("body: Any | None = None")

    if signature_parts:
        signature = "*,\n    " + ",\n    ".join(signature_parts)
    else:
        signature = ""

    def dict_block(name: str, params: list[Parameter]) -> str:
        if not params:
            return f"{name}: dict[str, Any] = {{}}"
        items = ",\n        ".join(
            f"{param.original_name!r}: {param.py_name}" for param in params
        )
        return f"{name}: dict[str, Any] = {{\n        {items},\n    }}"

    path_block = dict_block("path_params", op.path_params)
    query_block = dict_block("query_params", op.query_params)
    header_block = dict_block("header_params", op.header_params)
    cookie_block = dict_block("cookie_params", op.cookie_params)

    body_arg = "body" if op.request_body_content_type is not None else "None"
    content_type = (
        repr(op.request_body_content_type)
        if op.request_body_content_type is not None
        else "None"
    )

    if signature:
        def_line = f"async def {op.operation_id}(\n    {signature}\n) -> Any:"
    else:
        def_line = f"async def {op.operation_id}() -> Any:"

    lines = [
        def_line,
        f'    """{_build_docstring(op)}"""',
        f"    {path_block}",
        f"    {query_block}",
        f"    {header_block}",
        f"    {cookie_block}",
        "    return await call_openapi(",
        f"        method={op.method!r},",
        f"        path_template={op.path!r},",
        "        path_params=path_params,",
        "        query_params=query_params,",
        "        header_params=header_params,",
        "        cookie_params=cookie_params,",
        f"        body={body_arg},",
        f"        body_content_type={content_type},",
        "    )",
        "",
    ]
    return "\n".join(lines)


def _build_module_source(operations: list[Operation]) -> str:
    ops_sorted = sorted(operations, key=lambda op: op.operation_id)
    function_defs = "\n".join(_build_function_source(op) for op in ops_sorted)
    all_names = ",\n    ".join(repr(op.operation_id) for op in ops_sorted)
    lines = [
        '"""Auto-generated MCP tools from AYON OpenAPI spec."""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        "from ._runtime import call_openapi",
        "",
        function_defs,
        "__all__ = [",
        f"    {all_names}",
        "]",
        "",
    ]
    return "\n".join(lines)


def _runtime_source() -> str:
    return '''"""Runtime helpers for generated OpenAPI MCP tools."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ayon_mcp.rest_client import get_global_rest_client


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _build_cookie_header(cookie_params: dict[str, Any]) -> str | None:
    cookie_parts = [
        f"{key}={value}"
        for key, value in cookie_params.items()
        if value is not None
    ]
    if not cookie_parts:
        return None
    return "; ".join(cookie_parts)


async def call_openapi(  # ruff: ignore[too-many-arguments]
    method: str,
    path_template: str,
    path_params: dict[str, Any],
    query_params: dict[str, Any],
    header_params: dict[str, Any],
    cookie_params: dict[str, Any],
    body: Any,
    body_content_type: str | None,
) -> Any:
    """Call an AYON endpoint via the shared RestApiClient."""
    encoded_path_params = {
        key: quote(str(value), safe="")
        for key, value in path_params.items()
    }
    path = path_template.format(**encoded_path_params)

    headers = _drop_none(header_params)
    cookie_header = _build_cookie_header(cookie_params)
    if cookie_header:
        headers["cookie"] = cookie_header

    json_body = body if body_content_type == "application/json" else None
    data_body = body if (body is not None and json_body is None) else None
    if body_content_type and "content-type" not in {
        k.lower() for k in headers
    }:
        headers["content-type"] = body_content_type

    return await get_global_rest_client().request(
        method=method,
        path=path,
        params=_drop_none(query_params),
        json=json_body,
        data=data_body,
        headers=headers,
    )
'''


def _package_init_source(groups: list[str]) -> str:
    import_lines = []
    extend_lines = []
    for group in groups:
        import_lines.extend(
            (
                f"from . import {group} as _{group}",
                f"from .{group} import *  # noqa: F403",
            )
        )
        extend_lines.append(
            "ALL_OPENAPI_TOOLS.extend("
            f"getattr(_{group}, name) for name in _{group}.__all__"
            ")"
        )

    imports = "\n".join(import_lines)
    extend_block = "\n".join(extend_lines)
    return (
        '"""Auto-generated OpenAPI MCP tools package."""\n\n'
        "from __future__ import annotations\n\n"
        f"{imports}\n\n"
        "ALL_OPENAPI_TOOLS = []\n"
        f"{extend_block}\n"
    )


def _load_existing_tool_names(tools_init_path: Path) -> set[str]:
    """Return names of all tools imported in tools/__init__.py."""
    source = tools_init_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _resolve_project_root(project_root: Path | None = None) -> Path:
    if project_root is not None:
        return project_root.resolve()
    # .../services/mcp/ayon_mcp/openapi_codegen.py -> .../services/mcp
    return Path(__file__).resolve().parents[1]


def canonical_spec_hash(spec: dict[str, Any]) -> str:
    """Return deterministic SHA256 hash for an OpenAPI spec object."""
    payload = json.dumps(
        spec,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def _fetch_openapi_spec_with_client(
    client: RestApiClient,
    *,
    timeout: float,
    api_key: str,
) -> dict[str, Any]:
    headers = {"x-api-key": api_key} if api_key else None
    payload = await asyncio.wait_for(
        client.request("GET", "/openapi.json", headers=headers),
        timeout=timeout,
    )
    if not isinstance(payload, dict):
        msg = "OpenAPI payload must be a JSON object"
        raise TypeError(msg)
    return payload


def fetch_openapi_spec(
    base_url: str,
    timeout: float = 10.0,
    api_key: str = "",
) -> dict[str, Any]:
    """Fetch OpenAPI JSON from <base_url>/openapi.json using RestApiClient.

    Args:
        base_url: Base URL of the AYON server (e.g. "https://ayon.example.com")
        timeout: Timeout in seconds for the HTTP request.
        api_key: Optional AYON API key used for authenticated fetch.

    Returns:
        Parsed OpenAPI spec as a dictionary.

    """
    async def _runner() -> dict[str, Any]:
        client = RestApiClient(base_url)
        try:
            return await _fetch_openapi_spec_with_client(
                client,
                timeout=timeout,
                api_key=api_key,
            )
        finally:
            await client.close()

    return asyncio.run(_runner())


def _load_local_spec_hash(spec_path: Path) -> str | None:
    if not spec_path.exists():
        return None
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(spec, dict):
        return None
    return canonical_spec_hash(spec)


def generate_openapi_tools(
    *,
    spec: dict[str, Any] | None = None,
    project_root: Path | None = None,
) -> tuple[int, int]:
    """Generate OpenAPI tool wrappers.

    Args:
        spec: Optional already-loaded OpenAPI spec. If omitted,
            ``ayon_openapi.json`` is read from the project root.
        project_root: Optional path to ``services/mcp`` root.

    Returns:
        Tuple ``(operation_count, group_count)``.

    Raises:
        TypeError: If the local OpenAPI payload is not a JSON object.
    """
    resolved_root = _resolve_project_root(project_root)
    spec_path = resolved_root / "ayon_openapi.json"
    output_dir = resolved_root / "ayon_mcp" / "tools" / "openapi_generated"

    if spec is None:
        with spec_path.open("r", encoding="utf-8") as stream:
            loaded_spec = json.load(stream)
        if not isinstance(loaded_spec, dict):
            msg = "ayon_openapi.json must contain a JSON object"
            raise TypeError(msg)
        spec = loaded_spec

    existing_tool_names = _load_existing_tool_names(
        resolved_root / "ayon_mcp" / "tools" / "__init__.py"
    )

    operations = _collect_operations(spec)
    for operation in operations:
        if operation.operation_id in existing_tool_names:
            operation.operation_id = f"openapi_{operation.operation_id}"

    groups: dict[str, list[Operation]] = defaultdict(list)
    for operation in operations:
        groups[operation.group].append(operation)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "_runtime.py").write_text(
        _runtime_source(),
        encoding="utf-8",
    )

    group_names = sorted(groups)
    for group in group_names:
        source = _build_module_source(groups[group])
        (output_dir / f"{group}.py").write_text(source, encoding="utf-8")

    (output_dir / "__init__.py").write_text(
        _package_init_source(group_names), encoding="utf-8"
    )

    return len(operations), len(group_names)


def sync_openapi_tools_from_server(
    base_url: str,
    *,
    project_root: Path | None = None,
    timeout: float = 10.0,
    api_key: str = "",
) -> bool:
    """Sync local spec from server and regenerate tools only when changed.

    Returns:
        ``True`` when generated files were updated, otherwise ``False``.

    """
    resolved_root = _resolve_project_root(project_root)
    spec_path = resolved_root / "ayon_openapi.json"
    generated_init = (
        resolved_root
        / "ayon_mcp"
        / "tools"
        / "openapi_generated"
        / "__init__.py"
    )

    remote_spec = fetch_openapi_spec(
        base_url,
        timeout=timeout,
        api_key=api_key,
    )
    remote_hash = canonical_spec_hash(remote_spec)
    local_hash = _load_local_spec_hash(spec_path)

    if remote_hash == local_hash and generated_init.exists():
        return False

    spec_text = json.dumps(remote_spec, indent=2, sort_keys=True) + "\n"
    spec_path.write_text(spec_text, encoding="utf-8")
    generate_openapi_tools(spec=remote_spec, project_root=resolved_root)
    return True


def _main() -> None:
    operation_count, group_count = generate_openapi_tools()
    print(  # ruff: ignore[print]
        "Generated",
        operation_count,
        "operations in",
        group_count,
        "module(s).",
    )


if __name__ == "__main__":
    _main()
