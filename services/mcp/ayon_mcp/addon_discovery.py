"""Discover MCP tools from AYON addons."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import posixpath
import re
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

import httpx
from chuk_tool_processor.guards import SideEffectClass

from .rest_client import RestApiClient
from .rest_policy import METHOD_SIDE_EFFECTS, SIDE_EFFECT_RANK

logger = logging.getLogger(__name__)

SUPPORTED_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
# MCP tool names: ^[a-zA-Z0-9_-]{1,64}$ (applies to the namespaced name).
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_DECODE_ROUNDS = 3
# call_ayon_tool consumes these before the call reaches the addon.
RESERVED_PARAMETER_NAMES = frozenset({"confirm_mutation"})
# Manifest text goes into the model's context through the discovery
# tools, so an addon cannot flood it.
MAX_TOOLS_PER_ADDON = 50
MAX_DESCRIPTION_CHARS = 2_000
MAX_PARAMETERS_BYTES = 20_000
_SIDE_EFFECTS = {effect.value: effect for effect in SideEffectClass}


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
    side_effect: SideEffectClass = SideEffectClass.WRITE

    @property
    def full_name(self) -> str:
        """The namespaced tool name."""
        return f"{self.namespace}_{self.name}"


# Methods whose default is a minimum a manifest cannot relax: PUT / PATCH
# stay at least write, DELETE stays destructive. Only POST may be declared
# lower than its default (query endpoints that carry a body).
_MINIMUM_METHODS = frozenset({"PUT", "PATCH", "DELETE"})


def declared_side_effect(
    tool_def: dict[str, Any],
    method: str,
    full_name: str,
) -> SideEffectClass:
    """Classify an addon tool from its manifest entry.

    The manifest is the addon's own policy table: the HTTP method alone
    misclassifies AYON endpoints (a POST query that only reads), so the
    addon declares the effect per tool. ``sideEffect`` (``read_only`` /
    ``write`` / ``destructive``) wins, then ``readOnly``; without either
    the method default from ``rest_policy`` applies, which fails closed
    for anything but GET. The method is a minimum: ``PUT`` and ``PATCH``
    can never be declared read-only and ``DELETE`` is always destructive,
    so they stay behind ``confirm_mutation`` and out of read-only mode.

    Returns:
        The tool's ``SideEffectClass``.

    """
    default = METHOD_SIDE_EFFECTS[method]
    effect = _declared_effect(tool_def, default, full_name)
    if method in _MINIMUM_METHODS and (
        SIDE_EFFECT_RANK[effect] < SIDE_EFFECT_RANK[default]
    ):
        logger.warning(
            "Addon tool %s: %s cannot be %s, using %s",
            full_name,
            method,
            effect.value,
            default.value,
        )
        return default
    return effect


def _declared_effect(
    tool_def: dict[str, Any],
    default: SideEffectClass,
    full_name: str,
) -> SideEffectClass:
    """Apply the manifest's ``sideEffect`` / ``readOnly`` over ``default``.

    Returns:
        The declared ``SideEffectClass``, or ``default`` when none applies.

    """
    declared = tool_def.get("sideEffect")
    if declared is not None:
        effect = (
            _SIDE_EFFECTS.get(declared.lower())
            if isinstance(declared, str)
            else None
        )
        if effect is not None:
            return effect
        logger.warning(
            "Addon tool %s: unknown sideEffect %r, using %s",
            full_name,
            declared,
            default.value,
        )
        return default
    read_only = tool_def.get("readOnly")
    if read_only is True:
        return SideEffectClass.READ_ONLY
    if read_only is False and default is SideEffectClass.READ_ONLY:
        return SideEffectClass.WRITE
    return default


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
        MCP tools spec if addon exposes it, None if it does not (404 or an
        unexpected body).

    Raises:
        httpx.HTTPStatusError: The addon answered with an error other than
            404 (5xx, 403); it may expose tools, so the caller should retry.
        httpx.HTTPError: The request failed (network error, timeout).
        ValueError: The response was not valid JSON.

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
            raise
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "Addon %s/%s MCP tools request failed: %s",
            addon_name,
            addon_version,
            exc,
        )
        raise
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


def _parse_parameters(
    tool_def: dict[str, Any], full_name: str
) -> dict[str, Any] | None:
    """Validate a tool's ``parameters`` schema, logging why it was skipped.

    Returns:
        The schema (``{}`` when missing or not an object), or None when the
        tool must be skipped.

    """
    parameters = tool_def.get("parameters", {})
    if not isinstance(parameters, dict):
        parameters = {}
    try:
        parameters_size = len(json.dumps(parameters))
    except (TypeError, ValueError):
        logger.warning(
            "Skipping addon tool %s: parameters are not JSON", full_name
        )
        return None
    if parameters_size > MAX_PARAMETERS_BYTES:
        logger.warning(
            "Skipping addon tool %s: parameters schema exceeds %d bytes",
            full_name,
            MAX_PARAMETERS_BYTES,
        )
        return None
    properties = parameters.get("properties")
    if isinstance(properties, dict) and any(
        key in properties for key in RESERVED_PARAMETER_NAMES
    ):
        logger.warning(
            "Skipping addon tool %s: parameters use a reserved name (%s)",
            full_name,
            ", ".join(sorted(RESERVED_PARAMETER_NAMES)),
        )
        return None
    return parameters


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
    if not isinstance(description, str):
        description = ""
    if len(description) > MAX_DESCRIPTION_CHARS:
        logger.warning(
            "Addon tool %s: description truncated to %d characters",
            full_name,
            MAX_DESCRIPTION_CHARS,
        )
        description = description[:MAX_DESCRIPTION_CHARS]
    parameters = _parse_parameters(tool_def, full_name)
    if parameters is None:
        return None
    return AddonTool(
        name=name,
        description=description,
        parameters=parameters,
        endpoint=endpoint,
        addon_name=addon_name,
        addon_version=addon_version,
        namespace=namespace,
        side_effect=declared_side_effect(tool_def, endpoint.method, full_name),
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

    if len(tool_defs) > MAX_TOOLS_PER_ADDON:
        logger.warning(
            "Addon %s/%s lists %d tools, only the first %d are used",
            addon_name,
            addon_version,
            len(tool_defs),
            MAX_TOOLS_PER_ADDON,
        )
        tool_defs = tool_defs[:MAX_TOOLS_PER_ADDON]

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
) -> list[AddonTool] | None:
    """Fetch and parse the MCP tools of one addon version.

    Returns:
        The addon's tools (empty when it exposes none), or ``None`` when
        the request failed and should be retried later.

    """
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
        return None
    except (httpx.HTTPError, ValueError):
        # Already logged by fetch_addon_mcp_tools.
        return None
    if mcp_spec is None:
        return []

    try:
        tools = parse_addon_tools(mcp_spec, addon_name, version)
    except Exception:
        # One broken addon must not take the others down with it. A broken
        # manifest is not transient, so it is not retried.
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


async def resolve_addon_targets(
    client: RestApiClient,
    api_key: str = "",
    timeout: float = 10.0,  # ruff: ignore[async-function-with-timeout]
    variant: str = "production",
) -> list[tuple[str, str]]:
    """Return the ``(addon_name, version)`` pairs active in ``variant``.

    Returns:
        The addon versions to ask for MCP tools, in server order.

    """
    addons = await asyncio.wait_for(
        fetch_addons(client, api_key), timeout=timeout
    )
    return await asyncio.wait_for(
        _resolve_targets(client, addons, variant, api_key), timeout=timeout
    )


async def discover_addon_tools(
    client: RestApiClient,
    api_key: str = "",
    timeout: float = 10.0,  # ruff: ignore[async-function-with-timeout]
    variant: str = "production",
    targets: list[tuple[str, str]] | None = None,
) -> list[AddonTool]:
    """Discover MCP tools from addons active in the given variant.

    Addons are queried concurrently, each with its own timeout, so one slow
    addon cannot drop the tools of the others.

    Args:
        client: REST client configured with AYON server URL.
        api_key: API key used for discovery requests.
        timeout: Per-request timeout in seconds.
        variant: ``production``, ``staging`` or a dev bundle name.
        targets: Already resolved ``(addon_name, version)`` pairs; resolved
            from ``variant`` when omitted.

    Returns:
        List of all discovered addon tools.

    """
    if targets is None:
        targets = await resolve_addon_targets(
            client, api_key, timeout, variant
        )

    results = await _discover_targets(
        client, api_key, timeout, targets
    )
    return _merge_addon_tools(
        [results.get(target) or [] for target in targets]
    )


async def _discover_targets(
    client: RestApiClient,
    api_key: str,
    timeout: float,  # ruff: ignore[async-function-with-timeout]
    targets: list[tuple[str, str]],
) -> dict[tuple[str, str], list[AddonTool] | None]:
    """Query ``targets`` concurrently.

    Returns:
        Tools per target; ``None`` marks a failed request.

    """
    results = await asyncio.gather(
        *(
            _discover_one_addon(client, name, version, api_key, timeout)
            for name, version in targets
        )
    )
    return dict(zip(targets, results, strict=True))


def _merge_addon_tools(per_addon: list[list[AddonTool]]) -> list[AddonTool]:
    """Flatten per-addon tools, keeping the first of duplicate names.

    Returns:
        All tools in addon order.

    """
    all_tools: list[AddonTool] = []
    seen_names: set[str] = set()
    discovered_addons: set[str] = set()
    for tools in per_addon:
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


# A model asking for a tool this replica does not know triggers a re-check,
# at most this often, so unknown names cannot hammer the server.
FORCED_REFRESH_INTERVAL = 10.0


class AddonCatalog:
    """Addon MCP tools of one bundle variant, kept in sync with the server.

    In tunnel mode several stateless replicas serve the same MCP endpoint,
    each with its own catalog. A replica re-checks which addon versions
    the variant uses at most every ``refresh_interval`` seconds (one
    ``GET /api/addons``) and re-reads the manifests only when they
    changed, bumping ``generation`` so providers rebuild their catalog.
    Discovery always runs with the service API key, never as the user
    whose request triggered the check.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        variant: str = "production",
        refresh_interval: float = 60.0,
        timeout: float = 10.0,
    ) -> None:
        """Initialize an empty catalog; call ``load_sync`` at startup.

        Args:
            base_url: AYON server URL.
            api_key: Service API key used for discovery.
            variant: ``production``, ``staging`` or a dev bundle name.
            refresh_interval: Seconds between bundle checks; ``0``
                disables periodic checks (forced checks still run).
            timeout: Per-request timeout in seconds.

        """
        self.tools: list[AddonTool] = []
        self.generation = 0
        self._base_url = base_url
        self._api_key = api_key
        self._variant = variant
        self._refresh_interval = refresh_interval
        self._timeout = timeout
        self._targets: list[tuple[str, str]] | None = None
        self._tools_by_target: dict[tuple[str, str], list[AddonTool]] = {}
        # Targets whose last request failed; retried on every refresh.
        self._failed_targets: set[tuple[str, str]] = set()
        self._checked_at = float("-inf")
        self._forced_at = float("-inf")
        self._lock: asyncio.Lock | None = None

    def load_sync(self) -> None:
        """Run the first discovery from synchronous startup code.

        Failures are logged and leave the catalog empty: discovery must
        never prevent the MCP server from starting.
        """
        self._checked_at = time.monotonic()
        asyncio.run(self._load_logged())

    async def refresh(self, *, force: bool = False) -> None:
        """Re-discover addon tools if the bundle check is due.

        Args:
            force: Check now (rate limited by ``FORCED_REFRESH_INTERVAL``)
                instead of waiting for ``refresh_interval``.

        """
        started = time.monotonic()
        if force:
            if started - self._forced_at < FORCED_REFRESH_INTERVAL:
                return
        elif (
            self._refresh_interval <= 0
            or started - self._checked_at < self._refresh_interval
        ):
            return

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._checked_at >= started:
                # A concurrent call refreshed while this one waited.
                return
            if force:
                self._forced_at = started
            self._checked_at = time.monotonic()
            # An empty context drops the caller's x-as-user identity.
            await asyncio.create_task(
                self._load_logged(), context=contextvars.Context()
            )

    async def _load(self) -> None:
        client = RestApiClient(self._base_url)
        try:
            targets = await resolve_addon_targets(
                client, self._api_key, self._timeout, self._variant
            )
            if targets == self._targets and not self._failed_targets:
                return
            # Re-ask only new versions and addons that failed before.
            to_fetch = [
                target
                for target in targets
                if target not in self._tools_by_target
                or target in self._failed_targets
            ]
            fetched = await _discover_targets(
                client, self._api_key, self._timeout, to_fetch
            )
        finally:
            await client.close()
        recovered = any(
            target in self._failed_targets and fetched[target] is not None
            for target in fetched
        )
        changed = targets != self._targets
        by_target = {
            target: self._tools_by_target.get(target, [])
            for target in targets
        }
        for target, tools in fetched.items():
            by_target[target] = tools or []
        self._failed_targets = {
            target for target, tools in fetched.items() if tools is None
        }
        tools = _merge_addon_tools([by_target[t] for t in targets])
        if self._targets is not None and changed:
            logger.info(
                "Addon versions of %r changed, %d addon MCP tools now",
                self._variant,
                len(tools),
            )
        self._targets = targets
        self._tools_by_target = by_target
        if changed or recovered:
            self.tools = tools
            self.generation += 1

    async def _load_logged(self) -> None:
        """Run ``_load``, keeping the current tools when it fails."""
        try:
            await self._load()
        except (httpx.HTTPError, TimeoutError) as exc:
            logger.warning(
                "Addon MCP tool discovery failed for %s: %s",
                self._base_url,
                exc,
            )
        except Exception:
            # Discovery must never break the MCP server.
            logger.exception("Addon MCP tool discovery crashed")
