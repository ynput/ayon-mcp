"""Discover MCP tools from AYON addons."""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

import httpx

from .rest_client import RestApiClient
from .tool_discovery import MUTATING_HTTP_METHODS

logger = logging.getLogger(__name__)

SUPPORTED_HTTP_METHODS = frozenset({"GET", *MUTATING_HTTP_METHODS})
# MCP tool names: ^[a-zA-Z0-9_-]{1,64}$ (applies to the namespaced name).
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_DECODE_ROUNDS = 3
# call_ayon_tool consumes these before the call reaches the addon.
RESERVED_PARAMETER_NAMES = frozenset({"confirm_mutation"})


@dataclass
class AddonToolEndpoint:
    """Endpoint configuration for an addon tool."""

    method: str
    path: str


@dataclass
class AddonTool:
    """An MCP tool exposed by an AYON addon."""

    name: str
    description: str
    parameters: dict[str, Any]
    endpoint: AddonToolEndpoint
    addon_name: str
    addon_version: str
    namespace: str
    read_only: bool | None = None

    @property
    def full_name(self) -> str:
        """The namespaced tool name."""
        return f"{self.namespace}_{self.name}"

    @property
    def requires_confirmation(self) -> bool:
        """Whether this tool mutates data.

        An explicit ``readOnly`` flag from the addon wins. Without it the
        HTTP method decides, so a POST used for a read-only query must be
        declared ``readOnly: true`` to skip the confirmation step.
        """
        if self.read_only is not None:
            return not self.read_only
        return self.endpoint.method.upper() in MUTATING_HTTP_METHODS


def allowed_endpoint_prefix(addon_name: str, addon_version: str) -> str:
    """Return the only path prefix an addon tool may target."""
    return f"/api/addons/{addon_name}/{addon_version}/"


def _canonical_path(path: str) -> str:
    """Percent-decode ``path`` until it stops changing.

    Servers and proxies decode before routing, so ``..%2F`` or ``%252e%252e``
    must be judged in their decoded form.

    Returns:
        The decoded path.

    """
    decoded = path
    for _ in range(_MAX_DECODE_ROUNDS):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def is_within_prefix(path: str, prefix: str) -> bool:
    """Return True if ``path`` stays under ``prefix`` after normalisation.

    The check runs on the fully percent-decoded path and rejects ``..``
    segments and backslashes outright, so neither a template such as
    ``/api/addons/x/1.0/../../users`` nor an encoded argument such as
    ``..%2F..%2Fusers`` can escape the addon once a server decodes it.
    """
    decoded = _canonical_path(path)
    if not decoded.startswith("/") or "\\" in decoded:
        return False
    if ".." in decoded.split("/"):
        return False
    normalised = posixpath.normpath(decoded)
    return normalised.startswith(prefix)


def _auth_headers(api_key: str) -> dict[str, str] | None:
    return {"x-api-key": api_key} if api_key else None


async def fetch_addons(
    client: RestApiClient,
    api_key: str = "",
) -> list[dict[str, Any]]:
    """Fetch list of installed addons from AYON server.

    ``GET /api/addons`` returns ``{"addons": [AddonListItem, ...]}`` where
    each item carries ``name``, ``productionVersion`` and ``versions``.

    Returns:
        List of addon info dictionaries.

    """
    response = await client.request(
        "GET", "/api/addons", headers=_auth_headers(api_key)
    )
    if not isinstance(response, dict):
        return []
    addons = response.get("addons", [])
    if isinstance(addons, dict):
        # Tolerate a name-keyed mapping as well.
        addons = [
            {"name": name, **(info if isinstance(info, dict) else {})}
            for name, info in addons.items()
        ]
    return [addon for addon in addons if isinstance(addon, dict)]


async def fetch_addon_mcp_tools(
    client: RestApiClient,
    addon_name: str,
    addon_version: str,
    api_key: str = "",
) -> dict[str, Any] | None:
    """Fetch MCP tool definitions from an addon.

    Returns:
        MCP tools spec if addon exposes it, None otherwise.

    """
    path = f"/api/addons/{addon_name}/{addon_version}/mcp/tools"
    try:
        response = await client.request(
            "GET", path, headers=_auth_headers(api_key)
        )
        if isinstance(response, dict) and "tools" in response:
            return response
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == HTTPStatus.NOT_FOUND:
            logger.debug(
                "Addon %s/%s does not expose MCP tools",
                addon_name,
                addon_version,
            )
        else:
            logger.warning(
                "Addon %s/%s MCP tools request failed with %s "
                "(check the discovery API key permissions)",
                addon_name,
                addon_version,
                status,
            )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "Addon %s/%s MCP tools request failed: %s",
            addon_name,
            addon_version,
            exc,
        )
    return None


def _parse_endpoint(
    endpoint_def: Any,  # ruff: ignore[any-type]
    addon_version: str,
    prefix: str,
) -> AddonToolEndpoint | str:
    """Validate an endpoint definition.

    Returns:
        The endpoint, or a human-readable reason why it was rejected.

    """
    if not isinstance(endpoint_def, dict):
        return "endpoint is not an object"
    method = endpoint_def.get("method", "GET")
    if (
        not isinstance(method, str)
        or method.upper() not in SUPPORTED_HTTP_METHODS
    ):
        return f"unsupported method {method!r}"
    path = endpoint_def.get("path", "")
    if not isinstance(path, str):
        return "endpoint path is not a string"
    path = path.replace("{version}", addon_version)
    if not is_within_prefix(path, prefix):
        return f"endpoint {path!r} is outside {prefix!r}"
    return AddonToolEndpoint(method=method.upper(), path=path)


def _parse_tool(
    tool_def: Any,  # ruff: ignore[any-type]
    namespace: str,
    addon_name: str,
    addon_version: str,
) -> AddonTool | None:
    """Validate a single tool definition, logging why it was skipped.

    Returns:
        The parsed tool, or None when the definition is unusable.

    """
    if not isinstance(tool_def, dict):
        logger.warning("Skipping addon %s tool: not an object", addon_name)
        return None
    name = tool_def.get("name")
    full_name = f"{namespace}_{name}"
    if not isinstance(name, str) or not _TOOL_NAME_RE.match(full_name):
        logger.warning(
            "Skipping addon %s tool %r: invalid tool name", addon_name, name
        )
        return None

    prefix = allowed_endpoint_prefix(addon_name, addon_version)
    endpoint = _parse_endpoint(tool_def.get("endpoint"), addon_version, prefix)
    if isinstance(endpoint, str):
        logger.warning("Skipping addon tool %s: %s", full_name, endpoint)
        return None

    description = tool_def.get("description", "")
    parameters = tool_def.get("parameters", {})
    properties = parameters.get("properties") if isinstance(
        parameters, dict
    ) else None
    if isinstance(properties, dict) and any(
        key in properties for key in RESERVED_PARAMETER_NAMES
    ):
        logger.warning(
            "Skipping addon tool %s: parameters use a reserved name (%s)",
            full_name,
            ", ".join(sorted(RESERVED_PARAMETER_NAMES)),
        )
        return None
    read_only = tool_def.get("readOnly")
    return AddonTool(
        name=name,
        description=description if isinstance(description, str) else "",
        parameters=parameters if isinstance(parameters, dict) else {},
        endpoint=endpoint,
        addon_name=addon_name,
        addon_version=addon_version,
        namespace=namespace,
        read_only=read_only if isinstance(read_only, bool) else None,
    )


def parse_addon_tools(
    mcp_spec: dict[str, Any],
    addon_name: str,
    addon_version: str,
) -> list[AddonTool]:
    """Parse MCP tool definitions from addon spec.

    Malformed entries are skipped with a warning instead of failing the
    whole addon, and a malformed ``tools`` collection yields no tools.

    Args:
        mcp_spec: The MCP tools spec from the addon.
        addon_name: Name of the addon.
        addon_version: Version of the addon.

    Returns:
        List of parsed AddonTool instances.

    """
    tool_defs = mcp_spec.get("tools")
    if not isinstance(tool_defs, list):
        logger.warning(
            "Addon %s/%s: 'tools' is not a list, ignoring",
            addon_name,
            addon_version,
        )
        return []
    namespace = mcp_spec.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        namespace = addon_name

    tools: list[AddonTool] = []
    for tool_def in tool_defs:
        tool = _parse_tool(tool_def, namespace, addon_name, addon_version)
        if tool is not None:
            tools.append(tool)
    return tools


_VARIANT_VERSION_FIELDS = {
    "production": "productionVersion",
    "staging": "stagingVersion",
}


async def _resolve_targets(
    client: RestApiClient,
    addons: list[dict[str, Any]],
    variant: str,
    api_key: str,
) -> list[tuple[str, str]]:
    """Return ``(addon_name, version)`` pairs active in ``variant``.

    ``production`` and ``staging`` use the versions the addon list already
    reports for those bundles. Any other variant is treated as a dev bundle
    name and resolved through ``GET /api/bundles``. Addons that are not part
    of the variant's bundle are skipped, so discovery never picks an
    arbitrary installed version.
    """
    field = _VARIANT_VERSION_FIELDS.get(variant)
    if field is not None:
        versions = {
            addon.get("name"): addon.get(field) for addon in addons
        }
    else:
        versions = await _fetch_bundle_versions(client, variant, api_key)

    targets: list[tuple[str, str]] = []
    for addon_info in addons:
        addon_name = addon_info.get("name")
        version = versions.get(addon_name)
        if addon_name and version:
            targets.append((str(addon_name), str(version)))
    return targets


async def _fetch_bundle_versions(
    client: RestApiClient,
    bundle_name: str,
    api_key: str,
) -> dict[str, Any]:
    """Return the addon versions pinned by the bundle named ``bundle_name``."""
    response = await client.request(
        "GET", "/api/bundles", headers=_auth_headers(api_key)
    )
    bundles = response.get("bundles") if isinstance(response, dict) else None
    for bundle in bundles if isinstance(bundles, list) else []:
        if isinstance(bundle, dict) and bundle.get("name") == bundle_name:
            addons = bundle.get("addons")
            return addons if isinstance(addons, dict) else {}
    logger.warning(
        "Bundle %r not found, no addon MCP tools discovered", bundle_name
    )
    return {}


async def _discover_one_addon(
    client: RestApiClient,
    addon_name: str,
    version: str,
    api_key: str,
    timeout: float,  # ruff: ignore[async-function-with-timeout]
) -> list[AddonTool]:
    try:
        mcp_spec = await asyncio.wait_for(
            fetch_addon_mcp_tools(client, addon_name, version, api_key),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning(
            "Addon %s/%s MCP tools request timed out after %.1fs",
            addon_name,
            version,
            timeout,
        )
        return []
    if mcp_spec is None:
        return []

    try:
        tools = parse_addon_tools(mcp_spec, addon_name, version)
    except Exception:
        # One broken addon must not take the others down with it.
        logger.exception(
            "Addon %s/%s MCP tools could not be parsed", addon_name, version
        )
        return []
    if tools:
        logger.info(
            "Discovered %d MCP tools from addon %s/%s",
            len(tools),
            addon_name,
            version,
        )
    return tools


async def discover_addon_tools(
    client: RestApiClient,
    api_key: str = "",
    timeout: float = 10.0,  # ruff: ignore[async-function-with-timeout]
    variant: str = "production",
) -> list[AddonTool]:
    """Discover MCP tools from addons active in the given variant.

    Addons are queried concurrently, each with its own timeout, so one slow
    addon cannot drop the tools of the others.

    Args:
        client: REST client configured with AYON server URL.
        api_key: API key used for discovery requests at startup.
        timeout: Per-request timeout in seconds.
        variant: ``production``, ``staging`` or a dev bundle name.

    Returns:
        List of all discovered addon tools.

    """
    addons = await asyncio.wait_for(
        fetch_addons(client, api_key), timeout=timeout
    )

    targets = await asyncio.wait_for(
        _resolve_targets(client, addons, variant, api_key), timeout=timeout
    )

    results = await asyncio.gather(
        *(
            _discover_one_addon(client, name, version, api_key, timeout)
            for name, version in targets
        )
    )

    all_tools: list[AddonTool] = []
    seen_names: set[str] = set()
    discovered_addons: set[str] = set()
    for tools in results:
        for tool in tools:
            if tool.full_name in seen_names:
                logger.warning(
                    "Skipping addon tool %s from %s: name already taken",
                    tool.full_name,
                    tool.addon_name,
                )
                continue
            seen_names.add(tool.full_name)
            all_tools.append(tool)
            discovered_addons.add(tool.addon_name)

    logger.info(
        "Total: discovered %d addon tools from %d addons",
        len(all_tools),
        len(discovered_addons),
    )

    return all_tools


def discover_addon_tools_sync(
    base_url: str,
    api_key: str = "",
    timeout: float = 10.0,
    variant: str = "production",
) -> list[AddonTool]:
    """Run addon discovery from synchronous startup code.

    Mirrors ``fetch_openapi_spec``: opens a short-lived client, runs the
    discovery coroutine and closes the client again. ``timeout`` applies per
    request, not to the whole discovery.

    Returns:
        List of all discovered addon tools, empty on failure.

    """
    async def _runner() -> list[AddonTool]:
        client = RestApiClient(base_url)
        try:
            return await discover_addon_tools(
                client, api_key, timeout, variant
            )
        finally:
            await client.close()

    try:
        return asyncio.run(_runner())
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning(
            "Addon MCP tool discovery failed for %s: %s", base_url, exc
        )
        return []
    except Exception:
        # Discovery must never prevent the MCP server from starting.
        logger.exception("Addon MCP tool discovery crashed")
        return []
