"""Settings for the addon.

Per-user MCP policy: the MCP service reads these settings and narrows its
service-wide env var policy (``AYON_MCP_GUARDS``, ``AYON_MCP_READ_ONLY``,
``AYON_MCP_ADMIN_TOOLS``) for each caller. Settings can only make the
policy stricter than the env vars, never looser. See
``docs/design/per-user-policy.md``.
"""
from typing import Any

from ayon_server.enum import EnumItem
from ayon_server.lib.postgres import Postgres
from ayon_server.settings import (
    BaseSettingsModel,
    SettingsField,
    ensure_unique_names,
)
from pydantic import field_validator

INHERIT = "inherit"


def _switch_enum() -> list[dict[str, str]]:
    return [
        {"value": INHERIT, "label": "Inherit"},
        {"value": "on", "label": "On"},
        {"value": "off", "label": "Off"},
    ]


def _guards_enum() -> list[dict[str, str]]:
    return [
        {"value": INHERIT, "label": "Inherit"},
        {"value": "off", "label": "Off"},
        {"value": "default", "label": "Default"},
        {"value": "strict", "label": "Strict"},
    ]


def _user_levels_enum() -> list[dict[str, str]]:
    return [
        {"value": "admin", "label": "Admin"},
        {"value": "manager", "label": "Manager"},
        {"value": "user", "label": "User"},
        {"value": "guest", "label": "Guest"},
        {"value": "service", "label": "Service"},
    ]


async def _users_enum() -> list[EnumItem]:
    rows = await Postgres.fetch(
        "SELECT name, attrib->>'fullName' AS full_name "
        "FROM public.users ORDER BY name"
    )
    return [
        EnumItem(
            value=row["name"],
            label=(
                f"{row['full_name']} ({row['name']})"
                if row["full_name"] else row["name"]
            ),
        )
        for row in rows
    ]


async def _access_groups_enum() -> list[EnumItem]:
    rows = await Postgres.fetch(
        "SELECT name FROM public.access_groups ORDER BY name"
    )
    return [EnumItem(value=row["name"], label=row["name"]) for row in rows]


class PolicyEffectModel(BaseSettingsModel):
    """What a policy does to the callers it applies to.

    "Inherit" keeps the value of the MCP service's env var. Other values
    can only make it stricter: a rule can turn read-only on but not off,
    and can hide admin tools but not show them when the service hides
    them.
    """

    guards: str = SettingsField(
        INHERIT,
        title="Guards",
        enum_resolver=_guards_enum,
        description=(
            "Guard chain profile. The stricter of this and the service's "
            "AYON_MCP_GUARDS applies (off < default < strict)."
        ),
    )
    read_only: str = SettingsField(
        INHERIT,
        title="Read-only",
        enum_resolver=_switch_enum,
        description=(
            "On hides and refuses every tool that changes AYON data. Off "
            "has no effect if the service runs with AYON_MCP_READ_ONLY."
        ),
    )
    admin_tools: str = SettingsField(
        INHERIT,
        title="Admin tools",
        enum_resolver=_switch_enum,
        description=(
            "Off hides admin/security REST tools. On has no effect unless "
            "the service runs with AYON_MCP_ADMIN_TOOLS."
        ),
    )


class PolicyRuleModel(PolicyEffectModel):
    """Policy for the callers matching any of the listed criteria."""

    _layout = "expanded"

    name: str = SettingsField("", title="Name")
    enabled: bool = SettingsField(default=True, title="Enabled")
    users: list[str] = SettingsField(
        default_factory=list,
        title="Users",
        enum_resolver=_users_enum,
    )
    user_levels: list[str] = SettingsField(
        default_factory=list,
        title="User levels",
        enum_resolver=_user_levels_enum,
    )
    access_groups: list[str] = SettingsField(
        default_factory=list,
        title="Access groups",
        enum_resolver=_access_groups_enum,
        description="Matches the user's default access groups.",
    )


class MCPSettings(BaseSettingsModel):
    """Settings for the addon."""

    default_policy: PolicyEffectModel = SettingsField(
        default_factory=PolicyEffectModel,
        title="Default policy",
        description="Applies to callers that no rule matches.",
        scope=["studio"],
    )
    rules: list[PolicyRuleModel] = SettingsField(
        default_factory=list,
        title="Policy rules",
        scope=["studio"],
        description=(
            "Every enabled rule matching the caller applies, and the "
            "strictest value of each setting wins. A rule matches a "
            "caller listed in Users, with one of the User levels, or in "
            "one of the Access groups; a rule with none of them matches "
            "nobody."
        ),
    )
    policy_refresh_seconds: int = SettingsField(
        60,
        title="Policy refresh (seconds)",
        scope=["studio"],
        ge=5,
        le=3600,
        description=(
            "How long the MCP service caches these settings and each "
            "caller's user record."
        ),
    )

    @field_validator("rules")
    @classmethod
    def validate_unique_rule_names(
        cls, value: list[PolicyRuleModel]
    ) -> list[PolicyRuleModel]:
        """Require unique rule names.

        Returns:
            The rules, unchanged.

        """
        ensure_unique_names(value)
        return value


DEFAULT_VALUES: dict[str, Any] = {}
