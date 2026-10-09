"""Guard chain configuration for AYON MCP tool execution.

Wraps chuk-tool-processor's ``chuk_tool_processor.guards`` package into a
single chain applied to every tool call routed through
``AyonDynamicToolProvider`` (see ``tool_discovery.py``). See
``docs/design/guardrails.md`` for the rationale and scope of what is (and is
deliberately not) covered here.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from chuk_tool_processor.guards import (
    EnforcementLevel,
    ExecutionMode,
    GuardChain,
    NetworkPolicyConfig,
    NetworkPolicyGuard,
    OutputSizeConfig,
    OutputSizeGuard,
    RetrySafetyConfig,
    RetrySafetyGuard,
    SchemaStrictnessGuard,
    SensitiveDataConfig,
    SensitiveDataGuard,
    SideEffectClass,
    SideEffectConfig,
    SideEffectGuard,
)

if TYPE_CHECKING:
    from .tool_discovery import AyonTool

logger = logging.getLogger(__name__)

TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
FALSEY_VALUES = frozenset({"0", "false", "no", "off"})

# Tools whose effect can't be undone through AYON - blocked outright in
# ``strict`` guard mode, regardless of confirm_mutation.
DESTRUCTIVE_TOOL_NAMES = frozenset({"delete_entity"})


class GuardsMode(StrEnum):
    """Guard chain profile selected by ``AYON_MCP_GUARDS``."""

    OFF = "off"
    DEFAULT = "default"
    STRICT = "strict"


def guards_mode() -> GuardsMode:
    """Return the guard chain profile configured via ``AYON_MCP_GUARDS``.

    Accepts ``default`` / ``strict`` / ``off``. Plain truthy/falsey values
    (``true``, ``0``, ...) map to ``default`` / ``off`` for compatibility.
    Unrecognized values fall back to ``default`` with a warning, so a typo
    never silently disables the chain.

    Returns:
        The configured ``GuardsMode``.

    """
    value = (
        (os.getenv("AYON_MCP_GUARDS", GuardsMode.DEFAULT) or "")
        .strip()
        .lower()
    )
    if value in FALSEY_VALUES:
        return GuardsMode.OFF
    if not value or value in TRUTHY_VALUES:
        return GuardsMode.DEFAULT
    try:
        return GuardsMode(value)
    except ValueError:
        logger.warning(
            "Unknown AYON_MCP_GUARDS value %r, using %r.",
            value,
            GuardsMode.DEFAULT.value,
        )
        return GuardsMode.DEFAULT


def guards_enabled() -> bool:
    """Return True unless guardrails are explicitly disabled.

    Returns:
        Whether the guard chain should be built and applied at all.

    """
    return guards_mode() is not GuardsMode.OFF


def read_only_enabled() -> bool:
    """Return True if the server should refuse every write/destructive call.

    Enforced independently of ``AYON_MCP_GUARDS`` - mutating tools are not
    registered at all, and ``AyonDynamicToolProvider`` refuses them even
    when the guard chain is off.

    Returns:
        Whether ``AYON_MCP_READ_ONLY`` is set to a truthy value.

    """
    value = (os.getenv("AYON_MCP_READ_ONLY", "false") or "").strip().lower()
    return value in TRUTHY_VALUES


def classify_side_effects(
    tools: list[AyonTool],
) -> dict[str, SideEffectClass]:
    """Map each tool to its read-only / write / destructive classification.

    The classification itself is computed once per tool in
    ``tool_discovery.tool_side_effect`` (curated tools by name, generated
    REST tools via ``rest_policy``).

    Returns:
        A mapping of tool name to its ``SideEffectClass``.

    """
    return {tool.name: tool.side_effect for tool in tools}


def build_guard_chain(
    tools: list[AyonTool],
    server_url: str | None = None,
) -> GuardChain | None:
    """Build the guard chain applied to every AYON tool call.

    Args:
        tools: The catalogued AYON tools the chain guards.
        server_url: The AYON server URL the MCP server talks to. Its
            hostname becomes the network allowlist; without it, URL
            arguments are not fenced to a single host.

    Returns:
        A configured ``GuardChain`` for the ``AYON_MCP_GUARDS`` profile
        (``default`` or ``strict``), or ``None`` when it is ``off``.

    """
    mode = guards_mode()
    if mode is GuardsMode.OFF:
        return None

    tools_by_name = {tool.name: tool for tool in tools}

    def get_schema(tool_name: str) -> dict[str, object] | None:
        tool = tools_by_name.get(tool_name)
        return tool.parameters if tool is not None else None

    if read_only_enabled():
        execution_mode = ExecutionMode.READ_ONLY
    elif mode is GuardsMode.STRICT:
        execution_mode = ExecutionMode.WRITE_ALLOWED
    else:
        execution_mode = ExecutionMode.DESTRUCTIVE_ALLOWED

    side_effect = SideEffectGuard(
        SideEffectConfig(
            mode=execution_mode,
            explicit_classifications=classify_side_effects(tools),
        )
    )

    hostname = urlparse(server_url or "").hostname
    network = NetworkPolicyGuard(
        NetworkPolicyConfig(
            # The AYON server URL is routinely localhost or a private IP in
            # dev/on-prem deployments - that's the intended target, not the
            # SSRF this guard defends against. The allowlist (when a
            # hostname is known) is the real fence: only argument values
            # pointing at AYON's own host are allowed through.
            allowed_domains={hostname} if hostname else None,
            block_localhost=False,
            block_private_ips=False,
            block_metadata_ips=True,
        )
    )

    sensitive_data = SensitiveDataGuard(
        SensitiveDataConfig(
            # WARN, not BLOCK: addon settings (e.g. ayon-shotgrid) can
            # legitimately contain secret-shaped values, both as arguments
            # to set_addon_settings and in get_addon_settings output.
            # Blocking those would break real functionality; warning still
            # surfaces an AYON_API_KEY that leaks into an error message.
            mode=EnforcementLevel.WARN,
        )
    )

    output_size = OutputSizeGuard(
        OutputSizeConfig(
            # Generous ceiling: paginated list tools already cap their own
            # page size, so this only catches unpaginated/generated-tool
            # responses that would otherwise blow out the model's context.
            max_bytes=2_000_000,
        )
    )

    chain = GuardChain(
        [
            ("schema", SchemaStrictnessGuard(get_schema=get_schema)),
            ("side_effect", side_effect),
            ("network", network),
            ("sensitive_data", sensitive_data),
            ("output_size", output_size),
        ]
    )
    if mode is GuardsMode.STRICT:
        # Stops the model from thrashing on the same failing call:
        # identical (tool, arguments) calls are blocked after repeated
        # failures. Attempts are recorded by AyonDynamicToolProvider.
        chain.add(
            "retry_safety",
            RetrySafetyGuard(RetrySafetyConfig(max_same_signature_retries=3)),
        )
    return chain
