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
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from .client import get_global_ayon_client
from .guardrails import (
    GUARDS_RANK,
    GuardsMode,
    caller_identity,
    guards_mode,
    read_only_enabled,
)
from .rest_policy import admin_tools_enabled

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


@dataclass(frozen=True)
class PolicyEffect:
    """One policy's settings; ``None`` means inherit (no opinion)."""

    guards: GuardsMode | None = None
    read_only: bool | None = None
    admin_tools: bool | None = None


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


def _parse_effect(item: Mapping[str, Any], name: str) -> PolicyEffect:
    guards_value = item.get("guards", INHERIT)
    guards = None
    if guards_value not in {None, INHERIT}:
        try:
            guards = GuardsMode(guards_value)
        except ValueError:
            logger.warning(
                "Unknown MCP policy guards %r in %s, inheriting.",
                guards_value,
                name,
            )
    return PolicyEffect(
        guards=guards,
        read_only=_parse_switch(item.get("read_only"), f"{name} read_only"),
        admin_tools=_parse_switch(
            item.get("admin_tools"), f"{name} admin_tools"
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

    @classmethod
    def from_env(cls) -> EffectivePolicy:
        """Return the service-wide ceiling set by the env vars.

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


def merge(
    ceiling: EffectivePolicy,
    effects: Iterable[PolicyEffect],
    matched_rules: tuple[str, ...] = (),
) -> EffectivePolicy:
    """Apply policy effects on top of the env ceiling, strictest wins.

    Returns:
        The ceiling narrowed by every effect.

    """
    guards = ceiling.guards
    read_only = ceiling.read_only
    admin_tools = ceiling.admin_tools
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
    return EffectivePolicy(
        guards=guards,
        read_only=read_only,
        admin_tools=admin_tools,
        matched_rules=matched_rules,
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
