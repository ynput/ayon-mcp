"""Per-caller MCP policy from the MCP addon's settings.

The env vars ``AYON_MCP_GUARDS``, ``AYON_MCP_READ_ONLY`` and
``AYON_MCP_ADMIN_TOOLS`` set the service-wide ceiling: the most permissive
policy any caller can get. The addon settings (``server/settings.py``) can
only narrow it per caller, by user name, user level or default access
group. Every rule matching the caller applies and the strictest value of
each setting wins; the default policy applies only to callers no rule
matches. Empty settings leave the env vars in charge.

See ``docs/design/per-user-policy.md``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote

import httpx
from chuk_tool_processor.guards import SideEffectClass

from .client import get_global_ayon_client
from .guardrails import (
    GUARDS_RANK,
    GuardsMode,
    caller_identity,
    guards_mode,
    read_only_enabled,
)
from .rest_policy import SIDE_EFFECT_RANK, admin_tools_enabled

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)

ADDON_NAME = "mcp"
INHERIT = "inherit"
DEFAULT_REFRESH_SECONDS = 60.0
MIN_REFRESH_SECONDS = 5.0
# Callers whose user record is kept, like ServiceUserMiddleware's clients.
MAX_CACHED_IDENTITIES = 256

USER_LEVELS = frozenset({"admin", "manager", "user", "guest", "service"})
REGEX_PREFIX = "re:"

_EnumT = TypeVar("_EnumT", bound=StrEnum)


@dataclass(frozen=True)
class Identity:
    """The AYON user a tool call acts as, as far as policy rules care."""

    name: str
    level: str
    access_groups: frozenset[str] = frozenset()

    @classmethod
    def from_user(cls, user: Mapping[str, Any]) -> Identity:
        """Build an identity from an AYON user record (``/api/users/me``).

        Returns:
            The identity; its level is the user's highest role.

        """
        data = user.get("data") or {}
        if data.get("isService"):
            level = "service"
        elif data.get("isGuest"):
            # AYON never treats a guest as admin, whatever else is set.
            level = "guest"
        elif data.get("isAdmin"):
            level = "admin"
        elif data.get("isManager"):
            level = "manager"
        else:
            level = "user"
        groups = data.get("defaultAccessGroups") or []
        return cls(
            name=str(user.get("name", "")),
            level=level,
            access_groups=frozenset(str(group) for group in groups),
        )


class FilterMode(StrEnum):
    """How a tool filter's patterns are used."""

    ALL = "all"
    ALLOWLIST = "allowlist"
    DENYLIST = "denylist"


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile one filter pattern.

    A glob (``fnmatch``-style, case-sensitive) by default; with a ``re:``
    prefix a regular expression, matched against the whole value.

    Returns:
        The compiled pattern, used with ``fullmatch``; ``re.compile``
        raises ``re.error`` for an invalid ``re:`` pattern.

    """
    if pattern.startswith(REGEX_PREFIX):
        return re.compile(pattern[len(REGEX_PREFIX):])
    return re.compile(fnmatch.translate(pattern))


@dataclass(frozen=True)
class ToolFilter:
    """An allowlist or denylist of tool patterns.

    A pattern matches a tool if it matches the tool's name, its namespace,
    its qualified name (``<namespace>.<name>``) or, for REST tools, its
    endpoint (``METHOD /path``).
    """

    mode: FilterMode = FilterMode.ALL
    patterns: tuple[str, ...] = ()
    _compiled: tuple[re.Pattern[str], ...] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Compile the patterns once."""
        try:
            compiled = tuple(compile_pattern(p) for p in self.patterns)
        except re.error as exc:
            # The settings validator rejects invalid regexes, so this is
            # settings written around it. The filter then matches no tool.
            logger.error(  # noqa: TRY400
                "Invalid MCP policy pattern in %s: %s", self.patterns, exc
            )
            compiled = None
        object.__setattr__(self, "_compiled", compiled)

    def matches(self, candidates: Iterable[str]) -> bool:
        """Return True if any pattern matches any candidate value."""
        if self._compiled is None:
            return False
        values = tuple(candidates)
        return any(
            pattern.fullmatch(value) is not None
            for pattern in self._compiled
            for value in values
        )

    def allows(self, candidates: Iterable[str]) -> bool:
        """Return True if a tool with these candidate values passes.

        An allowlist without patterns allows nothing. A filter with an
        invalid pattern matches no tool: as an allowlist it hides
        everything, as a denylist nothing.
        """
        if self.mode is FilterMode.ALL:
            return True
        matched = self.matches(candidates)
        return matched if self.mode is FilterMode.ALLOWLIST else not matched


@dataclass(frozen=True)
class PolicyEffect:
    """One policy's settings; ``None`` means inherit (no opinion)."""

    guards: GuardsMode | None = None
    read_only: bool | None = None
    admin_tools: bool | None = None
    tools: ToolFilter = field(default_factory=ToolFilter)
    openapi: ToolFilter = field(default_factory=ToolFilter)
    openapi_enabled: bool | None = None
    openapi_max_side_effect: SideEffectClass | None = None


@dataclass(frozen=True)
class PolicyRule:
    """A policy applied to the callers matching any of its criteria."""

    name: str
    effect: PolicyEffect
    users: frozenset[str] = frozenset()
    user_levels: frozenset[str] = frozenset()
    access_groups: frozenset[str] = frozenset()
    enabled: bool = True

    def matches(self, identity: Identity) -> bool:
        """Return True if the rule applies to ``identity``.

        A rule without any criteria matches nobody.
        """
        return self.enabled and (
            identity.name in self.users
            or identity.level in self.user_levels
            or not self.access_groups.isdisjoint(identity.access_groups)
        )


@dataclass(frozen=True)
class PolicySettings:
    """The MCP addon's policy settings, parsed."""

    default: PolicyEffect = field(default_factory=PolicyEffect)
    rules: tuple[PolicyRule, ...] = ()
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS

    @classmethod
    def parse(cls, raw: Mapping[str, Any] | None) -> PolicySettings:
        """Parse the addon's settings JSON.

        Tolerant: unknown or malformed values count as ``inherit``, with a
        warning, so a settings model mismatch never loosens anything.

        Returns:
            The parsed settings; empty settings for ``None``.

        """
        if not raw:
            return cls()
        rules = []
        for index, item in enumerate(raw.get("rules") or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or f"rule {index + 1}")
            rules.append(
                PolicyRule(
                    name=name,
                    effect=_parse_effect(item, name),
                    users=_names(item.get("users")),
                    user_levels=_names(item.get("user_levels")),
                    access_groups=_names(item.get("access_groups")),
                    enabled=item.get("enabled", True) is not False,
                )
            )
        try:
            refresh = float(
                raw.get("policy_refresh_seconds", DEFAULT_REFRESH_SECONDS)
            )
        except (TypeError, ValueError):
            refresh = DEFAULT_REFRESH_SECONDS
        return cls(
            default=_parse_effect(
                raw.get("default_policy") or {}, "default policy"
            ),
            rules=tuple(rules),
            refresh_seconds=max(refresh, MIN_REFRESH_SECONDS),
        )


def _names(value: object) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(str(item) for item in value)


def _parse_switch(value: object, what: str) -> bool | None:
    if value in {None, INHERIT}:
        return None
    if value == "on":
        return True
    if value == "off":
        return False
    logger.warning(
        "Unknown MCP policy value %r for %s, inheriting.", value, what
    )
    return None


def _parse_enum(
    enum: type[_EnumT], value: object, what: str
) -> _EnumT | None:
    if value in {None, INHERIT}:
        return None
    try:
        return enum(value)
    except ValueError:
        logger.warning(
            "Unknown MCP policy value %r for %s, inheriting.", value, what
        )
        return None


def _parse_filter(value: object, what: str) -> ToolFilter:
    """Parse a tool filter; a malformed one hides nothing extra.

    Returns:
        The filter.

    """
    if not isinstance(value, dict):
        return ToolFilter()
    mode = _parse_enum(FilterMode, value.get("mode", "all"), f"{what} mode")
    patterns = value.get("patterns") or []
    if not isinstance(patterns, list):
        patterns = []
    return ToolFilter(
        mode=mode or FilterMode.ALL,
        patterns=tuple(str(pattern) for pattern in patterns),
    )


def _parse_effect(item: Mapping[str, Any], name: str) -> PolicyEffect:
    openapi = item.get("openapi")
    if not isinstance(openapi, dict):
        openapi = {}
    return PolicyEffect(
        guards=_parse_enum(GuardsMode, item.get("guards"), f"{name} guards"),
        read_only=_parse_switch(item.get("read_only"), f"{name} read_only"),
        admin_tools=_parse_switch(
            item.get("admin_tools"), f"{name} admin_tools"
        ),
        tools=_parse_filter(item.get("tools"), f"{name} tools"),
        openapi=_parse_filter(openapi, f"{name} openapi"),
        openapi_enabled=_parse_switch(
            openapi.get("rest_tools"), f"{name} openapi rest_tools"
        ),
        openapi_max_side_effect=_parse_enum(
            SideEffectClass,
            openapi.get("max_side_effect"),
            f"{name} openapi max_side_effect",
        ),
    )


@dataclass(frozen=True)
class EffectivePolicy:
    """The policy a single tool call runs under."""

    guards: GuardsMode
    read_only: bool
    admin_tools: bool
    # Names of the rules that applied; empty for the default policy.
    matched_rules: tuple[str, ...] = ()
    # Every filter a tool must pass: curated and addon tools, REST tools.
    tool_filters: tuple[ToolFilter, ...] = ()
    openapi_filters: tuple[ToolFilter, ...] = ()
    openapi_enabled: bool = True
    openapi_max_side_effect: SideEffectClass = SideEffectClass.DESTRUCTIVE

    @classmethod
    def from_env(cls) -> EffectivePolicy:
        """Return the service-wide ceiling set by the env vars.

        REST tools disabled by ``AYON_MCP_ENABLE_OPENAPI_TOOLS`` are never
        loaded, so the ceiling always allows them.

        Returns:
            The policy for ``AYON_MCP_GUARDS``, ``AYON_MCP_READ_ONLY`` and
            ``AYON_MCP_ADMIN_TOOLS``.

        """
        return cls(
            guards=guards_mode(),
            read_only=read_only_enabled(),
            admin_tools=admin_tools_enabled(),
        )

    def describe(self) -> str:
        """Name where the policy came from, for refusal messages.

        Returns:
            Which rules or settings produced this policy.

        """
        if self.matched_rules:
            names = ", ".join(repr(name) for name in self.matched_rules)
            return f"MCP policy rules {names}"
        return "the MCP default policy"

    def allows_tool(
        self,
        name: str,
        namespace: str,
        side_effect: SideEffectClass,
        *,
        admin: bool = False,
        endpoint: tuple[str, str] | None = None,
    ) -> bool:
        """Return True if a caller with this policy may see a tool.

        Args:
            name: Tool name.
            namespace: Discovery namespace.
            side_effect: The tool's side-effect class.
            admin: Whether it calls an admin/security endpoint.
            endpoint: ``(METHOD, path)`` for REST tools, else ``None``.

        Returns:
            Whether the tool is visible and callable.

        """
        if admin and not self.admin_tools:
            return False
        if self.read_only and side_effect is not SideEffectClass.READ_ONLY:
            return False
        candidates = [name, namespace, f"{namespace}.{name}"]
        filters = self.tool_filters
        if endpoint is not None:
            if not self.openapi_enabled or (
                SIDE_EFFECT_RANK[side_effect]
                > SIDE_EFFECT_RANK[self.openapi_max_side_effect]
            ):
                return False
            candidates.append(f"{endpoint[0]} {endpoint[1]}")
            filters = self.openapi_filters
        return all(f.allows(candidates) for f in filters)

    def summary(self) -> dict[str, Any]:
        """Return the policy as JSON-friendly data, for explanations.

        Returns:
            Every setting of the policy.

        """
        def filters(items: tuple[ToolFilter, ...]) -> list[dict[str, Any]]:
            return [
                {"mode": item.mode.value, "patterns": list(item.patterns)}
                for item in items
            ]

        return {
            "source": self.describe(),
            "matched_rules": list(self.matched_rules),
            "guards": self.guards.value,
            "read_only": self.read_only,
            "admin_tools": self.admin_tools,
            "tool_filters": filters(self.tool_filters),
            "openapi_enabled": self.openapi_enabled,
            "openapi_max_side_effect": self.openapi_max_side_effect.value,
            "openapi_filters": filters(self.openapi_filters),
        }


def merge(
    ceiling: EffectivePolicy,
    effects: Iterable[PolicyEffect],
    matched_rules: tuple[str, ...] = (),
) -> EffectivePolicy:
    """Apply policy effects on top of the env ceiling, strictest wins.

    Filters add up: a tool must pass every effect's filter.

    Returns:
        The ceiling narrowed by every effect.

    """
    guards = ceiling.guards
    read_only = ceiling.read_only
    admin_tools = ceiling.admin_tools
    tool_filters = list(ceiling.tool_filters)
    openapi_filters = list(ceiling.openapi_filters)
    openapi_enabled = ceiling.openapi_enabled
    max_side_effect = ceiling.openapi_max_side_effect
    for effect in effects:
        if (
            effect.guards is not None
            and GUARDS_RANK[effect.guards] > GUARDS_RANK[guards]
        ):
            guards = effect.guards
        if effect.read_only is True:
            read_only = True
        if effect.admin_tools is False:
            admin_tools = False
        if effect.tools.mode is not FilterMode.ALL:
            tool_filters.append(effect.tools)
        if effect.openapi.mode is not FilterMode.ALL:
            openapi_filters.append(effect.openapi)
        if effect.openapi_enabled is False:
            openapi_enabled = False
        if (
            effect.openapi_max_side_effect is not None
            and SIDE_EFFECT_RANK[effect.openapi_max_side_effect]
            < SIDE_EFFECT_RANK[max_side_effect]
        ):
            max_side_effect = effect.openapi_max_side_effect
    return EffectivePolicy(
        guards=guards,
        read_only=read_only,
        admin_tools=admin_tools,
        matched_rules=matched_rules,
        tool_filters=tuple(tool_filters),
        openapi_filters=tuple(openapi_filters),
        openapi_enabled=openapi_enabled,
        openapi_max_side_effect=max_side_effect,
    )


def resolve(
    identity: Identity | None,
    settings: PolicySettings,
    ceiling: EffectivePolicy,
) -> EffectivePolicy:
    """Return the policy for one caller.

    Args:
        identity: The caller; ``None`` if it could not be looked up, in
            which case only the default policy applies.
        settings: The addon's policy settings.
        ceiling: The env var policy, which settings can only narrow.

    Returns:
        The effective policy.

    """
    matched = (
        [rule for rule in settings.rules if rule.matches(identity)]
        if identity is not None
        else []
    )
    if not matched:
        return merge(ceiling, [settings.default])
    return merge(
        ceiling,
        [rule.effect for rule in matched],
        tuple(rule.name for rule in matched),
    )


# The policy of the tool call being executed, set by the discovery provider
# so tools like get_server_info can report it.
_CURRENT_POLICY: ContextVar[EffectivePolicy | None] = ContextVar(
    "mcp_policy", default=None
)


def set_current_policy(policy: EffectivePolicy | None) -> None:
    """Set the policy of the tool call running in this context."""
    _CURRENT_POLICY.set(policy)


def get_current_policy() -> EffectivePolicy | None:
    """Return the policy of the tool call running in this context.

    Returns:
        The policy, or ``None`` outside a discovery-mode tool call.

    """
    return _CURRENT_POLICY.get()


def policy_settings_enabled() -> bool:
    """Return True unless ``AYON_MCP_POLICY_SETTINGS`` turns them off.

    Off, every caller gets the env var policy and the addon settings are
    never read.

    Returns:
        Whether the per-caller policy is read from the addon settings.

    """
    value = (os.getenv("AYON_MCP_POLICY_SETTINGS", "true") or "").strip()
    return value.lower() not in {"0", "false", "no", "off"}


def policy_variant() -> str:
    """Return the settings variant the policy is read from.

    Same as the addon tools' variant: ``AYON_MCP_ADDON_VARIANT``, then
    ``AYON_DEFAULT_SETTINGS_VARIANT``, then ``production``.

    Returns:
        ``production``, ``staging`` or a dev bundle name.

    """
    for name in ("AYON_MCP_ADDON_VARIANT", "AYON_DEFAULT_SETTINGS_VARIANT"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return "production"


def _bundle_matches(bundle: Mapping[str, Any], variant: str) -> bool:
    if variant == "production":
        return bool(bundle.get("isProduction"))
    if variant == "staging":
        return bool(bundle.get("isStaging"))
    return bundle.get("name") == variant


class PolicyStore:
    """Caches the addon's policy settings and callers' user records.

    Settings are always read with the service's own API key, never the
    caller's: a non-admin caller may not be allowed to read them. Both
    caches expire after the settings' ``policy_refresh_seconds``. A failed
    refresh keeps the last settings; before the first successful load the
    settings are empty, so the env vars decide.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        variant: str | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Initialize an empty store; the first policy lookup loads it.

        Args:
            base_url: AYON server URL.
            api_key: Service API key used to read the addon settings.
            variant: Settings variant; ``policy_variant()`` when omitted.
            timeout: Per-request timeout in seconds.
            transport: HTTP transport for the settings requests (tests).

        """
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._api_key = api_key
        self._variant = variant or policy_variant()
        self._timeout = timeout
        self._settings = PolicySettings()
        self._loaded_at = float("-inf")
        self._lock = asyncio.Lock()
        self._identities: OrderedDict[
            str, tuple[Identity | None, float]
        ] = OrderedDict()

    @property
    def _ttl(self) -> float:
        return self._settings.refresh_seconds

    async def settings(self) -> PolicySettings:
        """Return the policy settings, reloading them when expired.

        Returns:
            The current settings, possibly empty.

        """
        if time.monotonic() - self._loaded_at < self._ttl:
            return self._settings
        async with self._lock:
            if time.monotonic() - self._loaded_at >= self._ttl:
                await self._reload()
        return self._settings

    async def _reload(self) -> None:
        try:
            raw = await self._fetch_settings()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "Can't read the MCP policy settings from %s, keeping the "
                "previous policy: %s",
                self._base_url,
                exc,
            )
        else:
            self._settings = PolicySettings.parse(raw)
            self._identities.clear()
        # Also after a failure, so an unreachable server isn't asked on
        # every tool call.
        self._loaded_at = time.monotonic()

    async def _fetch_settings(self) -> dict[str, Any] | None:
        headers = {"x-api-key": self._api_key}
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers=headers,
            transport=self._transport,
        ) as http:
            version = await self._addon_version(http)
            if version is None:
                return None
            response = await http.get(
                f"/api/addons/{ADDON_NAME}/{version}/settings",
                params={"variant": self._variant},
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            msg = "MCP addon settings are not a JSON object"
            raise ValueError(msg)  # noqa: TRY004
        return payload

    async def _addon_version(self, http: httpx.AsyncClient) -> str | None:
        """Return the MCP addon version whose settings apply.

        ASH sets ``AYON_ADDON_NAME``/``AYON_ADDON_VERSION`` for the
        services it runs; otherwise the version comes from the bundle of
        the settings variant.

        Returns:
            The version, or ``None`` if the bundle has no MCP addon.

        """
        if (
            os.getenv("AYON_ADDON_NAME", ADDON_NAME).strip() == ADDON_NAME
            and (version := os.getenv("AYON_ADDON_VERSION", "").strip())
        ):
            return version
        response = await http.get("/api/bundles")
        response.raise_for_status()
        for bundle in response.json().get("bundles", []):
            if _bundle_matches(bundle, self._variant):
                version = (bundle.get("addons") or {}).get(ADDON_NAME)
                return str(version) if version else None
        return None

    async def identity(self) -> Identity | None:
        """Return the current caller's identity, from cache when fresh.

        Uses the AYON client the auth middleware set for this call, so
        ``/api/users/me`` answers for the caller (``x-as-user`` in tunnel
        mode).

        Returns:
            The identity, or ``None`` when it can't be looked up.

        """
        key = caller_identity()
        if not key:
            return None
        now = time.monotonic()
        cached = self._identities.get(key)
        if cached is not None and now - cached[1] < self._ttl:
            self._identities.move_to_end(key)
            return cached[0]
        identity = await self._fetch_identity()
        self._identities[key] = (identity, now)
        self._identities.move_to_end(key)
        while len(self._identities) > MAX_CACHED_IDENTITIES:
            self._identities.popitem(last=False)
        return identity

    async def identity_of(self, user_name: str) -> Identity | None:
        """Look up any user with the service key, uncached.

        For explaining another user's policy; tool calls use
        ``identity()``.

        Returns:
            The identity, or ``None`` if the user can't be read.

        """
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"x-api-key": self._api_key},
            transport=self._transport,
        ) as http:
            response = await http.get(f"/api/users/{quote(user_name)}")
        if response.status_code != httpx.codes.OK:
            logger.warning(
                "Can't read AYON user %r: HTTP %s",
                user_name,
                response.status_code,
            )
            return None
        return Identity.from_user(response.json())

    @staticmethod
    async def _fetch_identity() -> Identity | None:
        try:
            client = get_global_ayon_client()
        except RuntimeError:
            return None
        try:
            response = await asyncio.to_thread(client.get, "users/me")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Can't look up the MCP caller: %s", exc)
            return None
        if response.status != httpx.codes.OK or not isinstance(
            response.data, dict
        ):
            logger.warning(
                "Can't look up the MCP caller: HTTP %s", response.status
            )
            return None
        return Identity.from_user(response.data)

    async def policy(self) -> EffectivePolicy:
        """Return the policy for the current caller.

        Returns:
            The env ceiling narrowed by the matching settings.

        """
        settings = await self.settings()
        ceiling = EffectivePolicy.from_env()
        if not settings.rules and settings.default == PolicyEffect():
            return ceiling
        return resolve(await self.identity(), settings, ceiling)
