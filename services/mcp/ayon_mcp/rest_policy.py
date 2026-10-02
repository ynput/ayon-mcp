"""Side-effect and admin classification for generated OpenAPI REST tools.

Generated tools (``tools/openapi_generated``) wrap one AYON REST operation
each. Their side effect is derived from the HTTP method, with a reviewed
override table for operations where the method lies about the effect (a
POST that only reads, a POST that restarts the server, ...). A separate set
of path patterns marks admin/security endpoints, which are not registered
at all unless ``AYON_MCP_ADMIN_TOOLS`` is truthy.

This module lives outside ``openapi_generated/`` so regenerating the tools
never touches it. Every override and admin pattern must match a path in
``ayon_openapi.json`` - ``tests/test_rest_policy.py`` enforces that, so a
renamed endpoint fails loudly instead of silently losing its override.

``scripts/suggest_rest_policy.py`` can propose additions using a local LLM;
its output is a suggestion for review, never read at runtime.
"""

from __future__ import annotations

import inspect
import os
import re
from typing import TYPE_CHECKING, Any

from chuk_tool_processor.guards import SideEffectClass

if TYPE_CHECKING:
    from collections.abc import Callable

TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})

HTTP_METHOD_ATTR = "__ayon_http_method__"
HTTP_PATH_ATTR = "__ayon_http_path__"

# Matches the docstring line written by older codegen versions, for
# generated modules that predate the explicit attributes above.
_DOCSTRING_ENDPOINT_RE = re.compile(
    r"^Endpoint: (?P<method>[A-Z]+) (?P<path>\S+)$", re.MULTILINE
)

# Strictness order, used to compare classifications.
SIDE_EFFECT_RANK: dict[SideEffectClass, int] = {
    SideEffectClass.READ_ONLY: 0,
    SideEffectClass.WRITE: 1,
    SideEffectClass.DESTRUCTIVE: 2,
}

METHOD_SIDE_EFFECTS: dict[str, SideEffectClass] = {
    "GET": SideEffectClass.READ_ONLY,
    "HEAD": SideEffectClass.READ_ONLY,
    "OPTIONS": SideEffectClass.READ_ONLY,
    "POST": SideEffectClass.WRITE,
    "PUT": SideEffectClass.WRITE,
    "PATCH": SideEffectClass.WRITE,
    "DELETE": SideEffectClass.DESTRUCTIVE,
}

# (METHOD, path template) -> side effect, where the method-based default
# is wrong. Keys must match ``ayon_openapi.json`` path templates exactly.
SIDE_EFFECT_OVERRIDES: dict[tuple[str, str], SideEffectClass] = {
    # POST used for queries with a request body - no state change.
    ("POST", "/api/query"): SideEffectClass.READ_ONLY,
    ("POST", "/api/resolve"): SideEffectClass.READ_ONLY,
    ("POST", "/api/actions/list"): SideEffectClass.READ_ONLY,
    ("POST", "/api/bundles/check"): SideEffectClass.READ_ONLY,
    ("POST", "/api/csv/export/{entity_type}"): SideEffectClass.READ_ONLY,
    (
        "POST",
        "/api/projects/{project_name}/folders/search",
    ): SideEffectClass.READ_ONLY,
    (
        "POST",
        "/api/projects/{project_name}/suggest",
    ): SideEffectClass.READ_ONLY,
    (
        "POST",
        "/api/projects/{project_name}/{entity_type}/reviewables/list",
    ): SideEffectClass.READ_ONLY,
    # Not DELETE, but can't be undone through AYON.
    ("POST", "/api/system/restart"): SideEffectClass.DESTRUCTIVE,
    # Ends the session the MCP server itself is using.
    ("POST", "/api/auth/logout"): SideEffectClass.DESTRUCTIVE,
    # Bulk operations - a batch may contain delete operations.
    (
        "POST",
        "/api/projects/{project_name}/operations",
    ): SideEffectClass.DESTRUCTIVE,
    (
        "POST",
        "/api/projects/{project_name}/operations/background",
    ): SideEffectClass.DESTRUCTIVE,
    # Overwrites the previous value with no history.
    ("PUT", "/api/secrets/{secret_name}"): SideEffectClass.DESTRUCTIVE,
    ("PATCH", "/api/users/{user_name}/password"): SideEffectClass.DESTRUCTIVE,
}

_ANY = None
_WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# (methods or None for all, path regex). Admin/security endpoints: secrets,
# credentials, sessions, user and access management, server/service
# lifecycle and addon/installer distribution. Reads are included where the
# response itself is sensitive (secret values, API keys, sessions).
AdminPattern = tuple[frozenset[str] | None, re.Pattern[str]]

ADMIN_PATTERNS: tuple[AdminPattern, ...] = tuple(
    (methods, re.compile(pattern))
    for methods, pattern in (
        (_ANY, r"^/api/secrets(/|$)"),
        (_ANY, r"^/api/auth/"),
        (_ANY, r"^/api/system/"),
        (_ANY, r"^/api/services(/|$)"),
        (_ANY, r"^/api/enroll$"),
        (_ANY, r"^/api/onboarding/"),
        (_ANY, r"^/api/access$"),
        (_ANY, r"^/api/users/\{user_name\}/apikeys(/|$)"),
        (_ANY, r"^/api/users/\{user_name\}/sessions(/|$)"),
        (_ANY, r"^/api/users/\{user_name\}/(password|checkPassword)$"),
        (_ANY, r"^/api/users/passwordReset"),
        (_ANY, r"^/api/users/\{user_name\}/(accessGroups|rename|invite)$"),
        (_WRITES, r"^/api/users/\{user_name\}$"),
        (_WRITES, r"^/api/accessGroups/"),
        (_WRITES, r"^/api/projects/\{project_name\}/users/\{user_name\}$"),
        (_WRITES, r"^/api/addons$"),
        (_WRITES, r"^/api/addons/install$"),
        (
            frozenset({"DELETE"}),
            r"^/api/addons/\{addon_name\}(/\{addon_version\})?$",
        ),
        (_WRITES, r"^/api/desktop/(installers|dependencyPackages)(/|$)"),
    )
)


def admin_tools_enabled() -> bool:
    """Return True if admin/security REST tools should be registered.

    Returns:
        Whether ``AYON_MCP_ADMIN_TOOLS`` is set to a truthy value.

    """
    value = (os.getenv("AYON_MCP_ADMIN_TOOLS", "false") or "").strip().lower()
    return value in TRUTHY_VALUES


def side_effect_for(method: str, path: str) -> SideEffectClass:
    """Classify a REST operation by its side effect.

    Unknown methods fail closed as ``WRITE``.

    Returns:
        The override for ``(method, path)`` if any, else the method default.

    """
    method = method.upper()
    override = SIDE_EFFECT_OVERRIDES.get((method, path))
    if override is not None:
        return override
    return METHOD_SIDE_EFFECTS.get(method, SideEffectClass.WRITE)


def is_admin(method: str, path: str) -> bool:
    """Return True if the REST operation is an admin/security endpoint.

    Returns:
        Whether any ``ADMIN_PATTERNS`` entry matches ``(method, path)``.

    """
    method = method.upper()
    return any(
        (methods is None or method in methods) and pattern.search(path)
        for methods, pattern in ADMIN_PATTERNS
    )


def endpoint_of(function: Callable[..., Any]) -> tuple[str, str] | None:
    """Return the ``(METHOD, path)`` a generated REST tool calls.

    Reads the attributes set by the ``endpoint`` decorator in generated
    code, falling back to the ``Endpoint:`` docstring line written by older
    codegen versions.

    Returns:
        The endpoint, or ``None`` if ``function`` is not a REST tool.

    """
    method = getattr(function, HTTP_METHOD_ATTR, None)
    path = getattr(function, HTTP_PATH_ATTR, None)
    if isinstance(method, str) and isinstance(path, str):
        return method.upper(), path
    match = _DOCSTRING_ENDPOINT_RE.search(inspect.getdoc(function) or "")
    if match is None:
        return None
    return match["method"], match["path"]
