"""Discover MCP tools from AYON addons."""

from __future__ import annotations

import asyncio
import logging
import posixpath
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import httpx

from .rest_client import RestApiClient
from .tool_discovery import MUTATING_HTTP_METHODS

logger = logging.getLogger(__name__)


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


def is_within_prefix(path: str, prefix: str) -> bool:
    """Return True if ``path`` stays under ``prefix`` after normalisation.

    Rejects ``..`` segments outright so a template such as
    ``/api/addons/x/1.0/../../users`` cannot escape the addon even when the
    HTTP client normalises the URL.
    """
    if not path.startswith("/"):
        return False
    if ".." in path.split("/"):
        return False
    normalised = posixpath.normpath(path)
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


def parse_addon_tools(
    mcp_spec: dict[str, Any],
    addon_name: str,
    addon_version: str,
) -> list[AddonTool]:
    """Parse MCP tool definitions from addon spec.

    Args:
        mcp_spec: The MCP tools spec from the addon.
        addon_name: Name of the addon.
        addon_version: Version of the addon.

    Returns:
        List of parsed AddonTool instances.

    """
    namespace = mcp_spec.get("namespace", addon_name)
    prefix = allowed_endpoint_prefix(addon_name, addon_version)
    tools = []

    for tool_def in mcp_spec.get("tools", []):
        name = tool_def.get("name")
        if not name:
            continue

        endpoint_def = tool_def.get("endpoint", {})
        endpoint_path = endpoint_def.get("path", "")
        # Replace {version} placeholder with actual version
        endpoint_path = endpoint_path.replace("{version}", addon_version)

        if not is_within_prefix(endpoint_path, prefix):
            logger.warning(
                "Skipping addon tool %s_%s: endpoint %r is outside %r",
                namespace,
                name,
                endpoint_path,
                prefix,
            )
            continue

        read_only = tool_def.get("readOnly")
        tools.append(
            AddonTool(
                name=name,
                description=tool_def.get("description", ""),
                parameters=tool_def.get("parameters", {}),
                endpoint=AddonToolEndpoint(
                    method=endpoint_def.get("method", "GET"),
                    path=endpoint_path,
                ),
                addon_name=addon_name,
                addon_version=addon_version,
                namespace=namespace,
                read_only=read_only if isinstance(read_only, bool) else None,
            )
        )

    return tools


def _resolve_version(addon_info: dict[str, Any]) -> str | None:
    """Return the production version, or the first listed version."""
    version = addon_info.get("productionVersion")
    if version:
        return str(version)
    versions = addon_info.get("versions") or {}
    if isinstance(versions, dict) and versions:
        return str(next(iter(versions)))
    return None


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

    tools = parse_addon_tools(mcp_spec, addon_name, version)
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
) -> list[AddonTool]:
    """Discover MCP tools from all installed addons.

    Addons are queried concurrently, each with its own timeout, so one slow
    addon cannot drop the tools of the others.

    Args:
        client: REST client configured with AYON server URL.
        api_key: API key used for discovery requests at startup.
        timeout: Per-request timeout in seconds.

    Returns:
        List of all discovered addon tools.

    """
    addons = await asyncio.wait_for(
        fetch_addons(client, api_key), timeout=timeout
    )

    targets: list[tuple[str, str]] = []
    for addon_info in addons:
        addon_name = addon_info.get("name")
        version = _resolve_version(addon_info)
        if addon_name and version:
            targets.append((str(addon_name), version))

    results = await asyncio.gather(
        *(
            _discover_one_addon(client, name, version, api_key, timeout)
            for name, version in targets
        )
    )

    all_tools: list[AddonTool] = []
    discovered_addons: set[str] = set()
    for tools in results:
        all_tools.extend(tools)
        discovered_addons.update(t.addon_name for t in tools)

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
            return await discover_addon_tools(client, api_key, timeout)
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
