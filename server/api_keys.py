"""The "Create my MCP key" project action.

AYON lets every user create API keys for themselves through the API
(``POST /api/users/{name}/apikeys``), but the web app shows no screen for
it. This action creates one for the user who clicks it and shows a ready
MCP client configuration with the key in it: the Claude Code command, an
entry for ``.mcp.json`` or VS Code's ``mcp.json``, or a Claude Desktop
entry.

The key is stored exactly as that endpoint stores it: hashed, with an id,
a label, a preview and an expiry, in the user's ``apiKeys`` list.

The first click answers with a form (format, machine). Submitting it runs
the action again with ``context.form_data``, and only then is a key
created. The answer is a second dialog with the configuration to copy.
"""
from __future__ import annotations

import json
import re
import secrets
import shlex
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ayon_server.actions import SimpleActionManifest
from ayon_server.auth.utils import hash_password
from ayon_server.forms import SimpleForm
from ayon_server.lib.postgres import Postgres
from ayon_server.utils import create_uuid

if TYPE_CHECKING:
    from ayon_server.actions import ActionExecutor, ExecuteResponseModel
    from ayon_server.addons import BaseServerAddon

    from .settings import ApiKeyActionSettings

ACTION_ID = "mcp.create-api-key"
LABEL_PREFIX = "mcp"
RESULT_FIELD = "config"
SERVER_NAME = "ayon"
SECONDS_PER_DAY = 86400

FORMATS = {
    "command": "Claude Code: terminal command (claude mcp add)",
    "mcp_json": "Claude Code: entry for a project's .mcp.json",
    "vscode": "VS Code: entry for .vscode/mcp.json",
    "desktop": "Claude Desktop: entry for claude_desktop_config.json",
}
MACHINE_RE = re.compile(r"^[a-z0-9-]{0,24}$")

# Appended in one statement, so two clicks at once cannot drop a key.
# COALESCE also covers a user who never had a key, where AYON's own
# endpoint fails on the missing list.
APPEND_KEY_SQL = """
    UPDATE users
    SET data = jsonb_set(
        data, '{apiKeys}',
        COALESCE(data->'apiKeys', '[]'::jsonb) || $1::jsonb
    )
    WHERE name = $2
"""

# A dev bundle's name is its variant.
DEV_BUNDLE_SQL = "SELECT 1 FROM bundles WHERE name = $1 AND is_dev"


def _until(expires: float | None) -> str:
    """Return an expiry timestamp as a date, or "never".

    Args:
        expires: Unix timestamp, or None for a key that never expires.

    Returns:
        The date as YYYY-MM-DD, or "never".

    """
    if not expires:
        return "never"
    return datetime.fromtimestamp(expires, UTC).strftime("%Y-%m-%d")


def snippet(fmt: str, url: str, key: str) -> tuple[str, str]:
    """Return the client configuration for one format.

    Args:
        fmt: One of ``FORMATS``.
        url: MCP endpoint URL.
        key: The new API key.

    Returns:
        The text to copy, and what to do with it.

    """
    if fmt in {"mcp_json", "vscode"}:
        entry = {"type": "http", "url": url, "headers": {"x-api-key": key}}
        text = f'"{SERVER_NAME}": ' + json.dumps(entry, indent=2)
        parent = "mcpServers" if fmt == "mcp_json" else "servers"
        where = (
            "your project's .mcp.json, then start a new Claude Code session"
            if fmt == "mcp_json"
            else ".vscode/mcp.json, then reload the window"
        )
        how = (
            f'Paste it inside "{parent}" in {where} (add a comma if '
            "another entry follows). The file now holds your key: keep it "
            "out of git."
        )
        return text, how
    if fmt == "desktop":
        args = ["-y", "mcp-remote", url, "--header", "x-api-key:${AYON_KEY}"]
        how = (
            'Paste it inside "mcpServers" in claude_desktop_config.json '
            "(Settings, Developer, Edit Config), then quit and reopen "
            "Claude Desktop. It needs Node.js 18 or newer."
        )
        # mcp-remote refuses plain HTTP except on localhost / 127.0.0.1.
        if url.startswith("http://"):
            args.append("--allow-http")
            how += (
                " This server uses plain HTTP, so the key travels "
                "unencrypted: prefer HTTPS outside a trusted network."
            )
        entry = {"command": "npx", "args": args, "env": {"AYON_KEY": key}}
        text = f'"{SERVER_NAME}": ' + json.dumps(entry, indent=2)
        return text, how
    # Quoted: an IPv6 host ("[::1]") is a glob pattern, and zsh refuses
    # to run a command whose glob matches nothing.
    text = (
        f"claude mcp add --scope user --transport http {SERVER_NAME} "
        f'{shlex.quote(url)} --header "x-api-key: {key}"'
    )
    how = (
        "Paste it in a terminal, then start a new Claude Code session. "
        "The command holds your key, so it also lands in your shell history."
    )
    return text, how


async def _settings(
    addon: BaseServerAddon, variant: str
) -> ApiKeyActionSettings:
    """Return the action settings for a variant.

    The variant comes from the request, and a variant without overrides
    gets the defaults, so only production, staging and existing dev
    bundles are trusted; anything else uses the production settings.

    Args:
        addon: The MCP addon.
        variant: Bundle variant (production, staging, ...).

    Returns:
        The ``api_key_action`` settings.

    """
    if variant not in {"production", "staging"} and not (
        await Postgres.fetchrow(DEV_BUNDLE_SQL, variant)
    ):
        variant = "production"
    settings = await addon.get_studio_settings(variant)
    return settings.api_key_action  # type: ignore[union-attr]


def _project_allowed(
    settings: ApiKeyActionSettings, project_name: str | None
) -> bool:
    """Return True if the action is offered on ``project_name``.

    Args:
        settings: The action settings.
        project_name: The project the action runs on.

    Returns:
        Whether the project may use the action.

    """
    if not project_name:
        return False
    return not settings.projects or project_name in settings.projects


async def get_actions(
    addon: BaseServerAddon,
    project_name: str | None,
    variant: str,
) -> list[SimpleActionManifest]:
    """Return the action for a project, if it is enabled there.

    Args:
        addon: The MCP addon.
        project_name: The project whose actions are listed.
        variant: Bundle variant.

    Returns:
        The action manifest, or an empty list.

    """
    settings = await _settings(addon, variant)
    if not settings.enabled:
        return []
    # Without a project AYON enumerates actions for the access group
    # "Restrict actions" picker, so the action must be listed there.
    # Running it still needs an allowed project (see _refusal).
    if project_name and not _project_allowed(settings, project_name):
        return []
    return [
        SimpleActionManifest(
            identifier=ACTION_ID,
            label="Create my MCP key",
            category="MCP",
            description=(
                "Create an API key for yourself and copy what connects "
                "an MCP client (Claude, VS Code) to this server"
            ),
            icon={"type": "material-symbols", "name": "key"},
            entity_type="project",
        )
    ]


def _refusal(settings: ApiKeyActionSettings, executor: ActionExecutor) -> str:
    """Return why the user may not create a key, or an empty string.

    Args:
        settings: The action settings.
        executor: The action executor, with the user who clicked.

    Returns:
        The refusal message, or "" when the user may create a key.

    """
    if not settings.enabled:
        return "Creating MCP keys is turned off on this server."
    if not _project_allowed(settings, executor.context.project_name):
        return "Creating MCP keys is not offered on this project."
    user = executor.user
    if user.is_service or user.is_guest:
        return "Service and guest users cannot create an MCP key."
    allowed = {
        d.strip().lower().lstrip("@")
        for d in settings.allowed_email_domains
        if d.strip()
    }
    if not allowed:
        return ""
    email = (user.attrib.email or "").strip().lower()
    if not email or email.rpartition("@")[2] not in allowed:
        return "Your account is not allowed to create MCP keys here."
    return ""


def _form(user_name: str, days: int) -> SimpleForm:
    """Return the form the first click shows.

    Args:
        user_name: The user the key is for.
        days: Key lifetime in days, 0 for never.

    Returns:
        The form.

    """
    expires = time.time() + days * SECONDS_PER_DAY if days else None
    form = SimpleForm()
    form.label(
        f"A new API key for {user_name} on this server, valid until "
        f"{_until(expires)}. It is shown once, in the next dialog."
    )
    form.select(
        "format",
        [{"value": v, "label": label} for v, label in FORMATS.items()],
        label="Copy as",
        value="command",
    )
    form.text(
        "machine",
        label="Machine (optional)",
        placeholder="laptop",
        regex=MACHINE_RE.pattern,
    )
    return form


async def _create_key(
    user_name: str, machine: str, days: int
) -> tuple[str, dict[str, Any]]:
    """Create an API key and append it to the user's keys.

    Args:
        user_name: The user the key is for.
        machine: Machine name for the label, may be empty.
        days: Key lifetime in days, 0 for never.

    Returns:
        The key, and the record stored for it.

    """
    key = secrets.token_hex(16)
    now = time.time()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    # The last four characters match the preview AYON shows, and keep two
    # clicks in one second from sharing a label (AYON's own endpoint
    # refuses duplicate labels).
    parts = [LABEL_PREFIX, machine, stamp, key[-4:]]
    record = {
        "id": create_uuid(),
        "label": "-".join(p for p in parts if p),
        "key": hash_password(key),
        "preview": key[:4] + "*****" + key[-4:],
        "created": now,
        "expires": now + days * SECONDS_PER_DAY if days else None,
    }
    await Postgres.execute(APPEND_KEY_SQL, [record], user_name)
    return key, record


def _result(
    record: dict[str, Any], user_name: str, url: str, text: str, how: str
) -> SimpleForm:
    """Return the dialog that shows the new key.

    Args:
        record: The stored key record.
        user_name: The user the key is for.
        url: MCP endpoint URL the configuration points at.
        text: The configuration to copy, with the key in it.
        how: What to do with it.

    Returns:
        The form.

    """
    form = SimpleForm()
    form.label(
        f"Key **{record['label']}** created for {user_name}, valid until "
        f"{_until(record['expires'])}. It is shown only now: copy it "
        "before you close this dialog."
    )
    # The URL comes from the browser's Referer, and AYON falls back to
    # localhost when there is none, so say where the client will connect.
    form.label(
        f"It connects to {url}. If you reach AYON at another address, "
        "change the URL after pasting."
    )
    form.label(how)
    form.text(RESULT_FIELD, value=text, multiline=True)
    return form


async def execute(
    addon: BaseServerAddon, executor: ActionExecutor
) -> ExecuteResponseModel:
    """Run the action: show the form, or create the key.

    Args:
        addon: The MCP addon.
        executor: The action executor.

    Returns:
        The form, a refusal, or the key in the chosen format.

    """
    settings = await _settings(addon, executor.variant)
    user = executor.user

    # Checked on both steps: the form is not a gate on its own.
    if refusal := _refusal(settings, executor):
        return await executor.get_simple_response(refusal, success=False)

    form_data: dict[str, Any] = executor.context.form_data or {}
    if not form_data:
        return await executor.get_form_response(
            title="Create my MCP key",
            fields=_form(user.name, settings.key_valid_days),
            submit_label="Create key",
            submit_icon="key",
        )
    if RESULT_FIELD in form_data:
        return await executor.get_simple_response(
            "Nothing more to do: no new key was created."
        )

    fmt = str(form_data.get("format") or "command")
    if fmt not in FORMATS:
        return await executor.get_simple_response(
            f"Unknown format {fmt!r}. Nothing was created.", success=False
        )
    machine = str(form_data.get("machine") or "").strip().lower()
    if not MACHINE_RE.match(machine):
        return await executor.get_simple_response(
            "The machine name may use a to z, 0 to 9 and dashes, up to 24. "
            "Nothing was created.",
            success=False,
        )

    key, record = await _create_key(
        user.name, machine, settings.key_valid_days
    )
    url = f"{executor.server_url.rstrip('/')}/api/mcp"
    text, how = snippet(fmt, url, key)
    # Shown in a dialog: the web app drops a clipboard write the browser
    # refuses (Safari, plain-HTTP servers) without a word. It ignores
    # extra_clipboard on a form answer today; kept for when it does not.
    return await executor.get_form_response(
        title="Your MCP key",
        fields=_result(record, user.name, url, text, how),
        submit_label=None,
        cancel_label="Close",
        extra_clipboard=text,
    )
