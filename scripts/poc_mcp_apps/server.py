"""POC: MCP Apps (interactive UI cards) on top of the real ayon-mcp server.

Not production code. Wraps ``create_mcp_server`` and adds three tools that
render HTML cards inside the chat (claude.ai custom connector):

- ``show_project_insights``      - read-only, renders ui://ayon/insights.html
- ``propose_task_status_change`` - read-only, renders ui://ayon/confirm.html
- ``apply_task_status_change``   - app-only, called by the card's Approve
                                   button with a single-use token, performs
                                   the actual write

See README.md in this folder. Run (from the repo root)::

    AYON_SERVER_URL=http://localhost:5100 AYON_API_KEY=... \
    POC_MCP_PATH=/mcp-<secret> .venv/bin/python scripts/poc_mcp_apps/server.py
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, NamedTuple

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "services" / "mcp"))

from ayon_mcp.client import get_ayon_api  # noqa: E402
from ayon_mcp.client import get_global_ayon_client as api  # noqa: E402
from ayon_mcp.guardrails import read_only_enabled  # noqa: E402
from ayon_mcp.server import (  # noqa: E402
    StaticApiKeyMiddleware,
    create_mcp_server,
)
from fastmcp.apps.config import AppConfig  # noqa: E402
from fastmcp.server.middleware import (  # noqa: E402
    CallNext,
    Middleware,
    MiddlewareContext,
)
from fastmcp.tools import ToolResult  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[poc] %(message)s")
log = logging.getLogger("poc_mcp_apps")

BASE_URL = os.getenv("AYON_SERVER_URL", "http://localhost:5100")
API_KEY = os.getenv("AYON_API_KEY", "")
MCP_PATH = os.getenv("POC_MCP_PATH", "/mcp")
PORT = int(os.getenv("AYON_MCP_PORT", "8088"))

mcp = create_mcp_server(BASE_URL, api_key=API_KEY)
mcp.add_middleware(StaticApiKeyMiddleware(BASE_URL, API_KEY))


# --------------------------------------------------------------------------
# AYON look: design tokens, Nunito Sans and a Material Symbols subset are
# inlined into every card (the iframe cannot load the AYON frontend assets).
# --------------------------------------------------------------------------

YNPUT = HERE.parents[2]
ARC_FONTS = Path(os.getenv(
    "POC_ARC_FONTS", YNPUT / "ayon-react-components" / "dist" / "fonts"
))
AYON_LOGO = Path(os.getenv(
    "POC_AYON_LOGO", YNPUT / "ayon-frontend" / "public" / "AYON.svg"
))
UI_ICONS = {
    "arrow_forward", "check", "close", "insights", "person", "error",
    "help", "sync", "keyboard_double_arrow_right", "task_alt",
    "play_arrow", "back_hand",
}
# ARC: .material-symbols-outlined uses FILL 0, wght 200, GRAD 200, opsz 20
SYMBOLS_CSS_URL = (
    "https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:"
    "opsz,wght,FILL,GRAD@20,200,0,200&display=block&icon_names={names}"
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _collect_icons() -> set[str]:
    """Icons used by any project's statuses, task types and folder types.

    Returns:
        Material Symbols icon names, including the ones the cards use.

    """
    icons = set(UI_ICONS)
    try:
        con = get_ayon_api(BASE_URL, API_KEY)
        for project in con.get_projects(fields={"name"}):
            anatomy = con.get_project(project["name"]) or {}
            for key in ("statuses", "taskTypes", "folderTypes"):
                icons.update(
                    i["icon"] for i in anatomy.get(key, []) if i.get("icon")
                )
    except Exception as exc:  # noqa: BLE001 - POC, keep starting
        log.warning("icon collection failed: %r", exc)
    return icons


def _symbols_font_face(icons: set[str]) -> str:
    """Download a Material Symbols subset with only ``icons`` (woff2).

    Returns:
        An @font-face rule with the font inlined, or "" when it failed.

    """
    ua = {"User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/120 Safari"}
    try:
        css = httpx.get(
            SYMBOLS_CSS_URL.format(names=",".join(sorted(icons))),
            headers=ua, timeout=20,
        ).text
        url = re.search(r"url\((https://[^)]+)\)", css).group(1)
        font = httpx.get(url, timeout=20).content
    except Exception as exc:  # noqa: BLE001 - POC, cards work without icons
        log.warning("Material Symbols subset failed: %r", exc)
        return ""
    log.info(
        "Material Symbols subset: %d icons, %d KB",
        len(icons), len(font) // 1024,
    )
    return (
        "@font-face{font-family:'Material Symbols Outlined';font-style:normal;"
        "font-weight:200;font-display:block;"
        f"src:url(data:font/woff2;base64,{_b64(font)}) format('woff2');}}"
    )


def _nunito_font_faces() -> str:
    faces = []
    for weight, name in ((400, "regular"), (600, "600"), (700, "700"),
                         (800, "800")):
        path = ARC_FONTS / f"nunito-sans-v15-latin_latin-ext-{name}.woff2"
        if path.exists():
            faces.append(
                "@font-face{font-family:'Nunito Sans';font-style:normal;"
                f"font-weight:{weight};font-display:swap;"
                f"src:url(data:font/woff2;base64,{_b64(path.read_bytes())})"
                " format('woff2');}"
            )
    return "".join(faces)


def _logo_svg() -> str:
    if not AYON_LOGO.exists():
        return "<b>AYON</b>"
    svg = AYON_LOGO.read_text(encoding="utf-8")
    return svg[svg.index("<svg"):]


def _build_assets() -> dict[str, str]:
    return {
        "<!--AYON_FONTS-->": (
            f"<style>{_nunito_font_faces()}"
            f"{_symbols_font_face(_collect_icons())}</style>"
        ),
        "<!--AYON_TOKENS-->": (
            f"<style>{(HERE / 'ayon.css').read_text(encoding='utf-8')}</style>"
        ),
        "<!--AYON_LOGO-->": _logo_svg(),
        "<!--AYON_BRIDGE-->": (
            f"<script>{(HERE / 'bridge.js').read_text(encoding='utf-8')}"
            "</script>"
        ),
    }


ASSETS = _build_assets()


def _render(name: str) -> str:
    html = (HERE / name).read_text(encoding="utf-8")
    for placeholder, value in ASSETS.items():
        html = html.replace(placeholder, value)
    return html


# --------------------------------------------------------------------------
# UI resources (served as text/html;profile=mcp-app, inline, no CDN)
#
# The URI carries a content hash so a changed card gets a new URI. claude.ai
# still caches the HTML per connector URL and ignores this (see README), but
# hosts that cache by URI pick up the change.
# --------------------------------------------------------------------------

INSIGHTS_HTML = _render("insights.html")
CONFIRM_HTML = _render("confirm.html")


def _versioned(name: str, html: str) -> str:
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()[:10]
    return f"ui://ayon/{name}-{digest}.html"


INSIGHTS_URI = _versioned("insights", INSIGHTS_HTML)
CONFIRM_URI = _versioned("confirm", CONFIRM_HTML)
log.info("UI resources: %s %s", INSIGHTS_URI, CONFIRM_URI)


@mcp.resource(INSIGHTS_URI, name="ayon_insights_card")
def insights_card() -> str:
    """AYON project insights card.

    Returns:
        The card HTML.

    """
    return INSIGHTS_HTML


@mcp.resource(CONFIRM_URI, name="ayon_confirm_card")
def confirm_card() -> str:
    """AYON change confirmation card.

    Returns:
        The card HTML.

    """
    return CONFIRM_HTML


class LogMethods(Middleware):
    """Log every MCP method so we can see what the host actually calls."""

    async def on_message(  # noqa: PLR6301 - Middleware hook signature
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:  # noqa: ANN401 - passes through whatever the handler returns
        """Log the method (and resource URI or tool name), then continue.

        Returns:
            The result of the next handler.

        """
        target = ""
        params = getattr(context.message, "uri", None) or getattr(
            context.message, "name", None
        )
        if params:
            target = f" {params}"
        log.info("<- %s%s", context.method, target)
        return await call_next(context)


mcp.add_middleware(LogMethods())


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}")
_ICON_NAME = re.compile(r"[a-z0-9_]+")


def _color(value: object) -> str:
    """Anatomy colours are free strings; only hex may reach a style attr.

    Returns:
        ``value`` when it is a hex colour, else a neutral grey.

    """
    if isinstance(value, str) and _HEX_COLOR.fullmatch(value):
        return value
    return "#888888"


def _icon(value: object, default: str) -> str:
    """Only plain Material Symbols names may reach the cards.

    Returns:
        ``value`` when it is a valid icon name, else ``default``.

    """
    if isinstance(value, str) and _ICON_NAME.fullmatch(value):
        return value
    return default


def _project_anatomy(project_name: str) -> dict[str, Any]:
    """Statuses usable on tasks and task types, sanitised for the cards.

    Returns:
        ``{"statuses": [...], "task_types": [...]}``.

    Raises:
        ValueError: The project does not exist.

    """
    project = api().get_project(project_name)
    if not project:
        msg = f"Project '{project_name}' not found."
        raise ValueError(msg)
    return {
        "statuses": [
            {
                "name": s["name"],
                "color": _color(s.get("color")),
                "state": s.get("state") or "",
                "icon": _icon(s.get("icon"), "help"),
            }
            for s in project.get("statuses", [])
            # a status without scope applies to every entity type
            if "task" in (s.get("scope") or ["task"])
        ],
        "task_types": [
            {
                "name": t["name"],
                "color": _color(t.get("color")),
                "icon": _icon(t.get("icon"), "task_alt"),
            }
            for t in project.get("taskTypes", [])
        ],
    }


def _status_by_name(anatomy: dict[str, Any], name: str) -> dict[str, Any]:
    for status in anatomy["statuses"]:
        if status["name"] == name:
            return status
    return {"name": name, "color": "#888888", "state": "", "icon": "help"}


def _task_card_data(project_name: str, task_id: str) -> dict[str, Any]:
    task = api().get_task_by_id(
        project_name,
        task_id,
        fields={"id", "name", "label", "taskType", "status", "assignees",
                "folderId"},
    )
    if not task:
        msg = f"Task '{task_id}' not found in '{project_name}'."
        raise ValueError(msg)
    folder = api().get_folder_by_id(
        project_name, task["folderId"], fields={"id", "path"}
    )
    return {
        "id": task["id"],
        "name": task.get("label") or task["name"],
        "task_type": task.get("taskType"),
        "status": task.get("status"),
        "assignees": task.get("assignees") or [],
        "folder_path": (folder or {}).get("path", ""),
    }


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool(
    app=AppConfig(resource_uri=INSIGHTS_URI, prefers_border=False),
    annotations={"readOnlyHint": True},
)
def show_project_insights(project_name: str) -> ToolResult:
    """Show an interactive insights card for an AYON project.

    The card shows task progress by status, a task type x status breakdown
    and the busiest assignees. Use it when the user asks how a project is
    doing, for an overview, progress or workload.

    Args:
        project_name: AYON project name, e.g. "demo_Big_Episodic".

    Returns:
        A short text summary for the model plus the card data.

    """
    anatomy = _project_anatomy(project_name)
    by_status: Counter[str] = Counter()
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    open_by_assignee: Counter[str] = Counter()
    done_states = {"done"}
    state_of = {s["name"]: s["state"] for s in anatomy["statuses"]}

    total = 0
    for task in api().get_tasks(
        project_name, fields={"status", "taskType", "assignees"}
    ):
        total += 1
        status = task.get("status") or "?"
        by_status[status] += 1
        by_type[task.get("taskType") or "?"][status] += 1
        if state_of.get(status) not in done_states:
            for user in task.get("assignees") or []:
                open_by_assignee[user] += 1

    done = sum(
        count for name, count in by_status.items()
        if state_of.get(name) in done_states
    )
    data = {
        "project": project_name,
        "total_tasks": total,
        "done_tasks": done,
        "statuses": anatomy["statuses"],
        "task_types": anatomy["task_types"],
        "by_status": dict(by_status),
        "by_type": {k: dict(v) for k, v in by_type.items()},
        "top_assignees": open_by_assignee.most_common(8),
    }
    top = ", ".join(f"{s} {c}" for s, c in by_status.most_common(4))
    text = (
        f"{project_name}: {total} tasks, {done} done "
        f"({round(100 * done / total) if total else 0}%). "
        f"Top statuses: {top}. The user sees this as an interactive card."
    )
    return ToolResult(content=text, structured_content=data)


@mcp.tool(
    app=AppConfig(resource_uri=CONFIRM_URI, prefers_border=False),
    annotations={"readOnlyHint": True},
)
def propose_task_status_change(
    project_name: str,
    task_id: str,
    new_status: str,
) -> ToolResult:
    """Propose a task status change and show the user a confirmation card.

    This does NOT change anything. The user approves or cancels with the
    buttons on the card; the change is applied only on Approve. Do not ask
    the user "should I do it?" in text - call this tool instead and wait.

    Args:
        project_name: AYON project name.
        task_id: ID of the task to change.
        new_status: Target status name, must exist in the project.

    Returns:
        The proposal text for the model, the card data and, unless the
        server is read-only, an approval token in ``_meta``.

    Raises:
        ValueError: The status is not a task status of the project.

    """
    anatomy = _project_anatomy(project_name)
    if new_status not in {s["name"] for s in anatomy["statuses"]}:
        names = ", ".join(s["name"] for s in anatomy["statuses"])
        msg = f"Unknown task status '{new_status}'. Valid: {names}"
        raise ValueError(msg)
    task = _task_card_data(project_name, task_id)
    type_icon = next(
        (t["icon"] for t in anatomy["task_types"]
         if t["name"] == task["task_type"]),
        "task_alt",
    )
    data = {
        "project": project_name,
        "task": {**task, "task_type_icon": type_icon},
        "from_status": _status_by_name(anatomy, task["status"]),
        "to_status": _status_by_name(anatomy, new_status),
    }
    text = (
        f"Proposed: {task['folder_path']}/{task['name']} status "
        f"'{task['status']}' -> '{new_status}'. Nothing changed yet; the "
        "user confirms or cancels on the card."
    )
    meta = None
    if not read_only_enabled():
        # The approval token travels in the result _meta, which the host
        # hands to the card; the model gets only the text content.
        token = secrets.token_urlsafe(24)
        _PENDING[token] = _PendingChange(
            project_name, task_id, task["status"], new_status,
            time.monotonic() + PENDING_TTL_SECONDS,
        )
        meta = {"ayon_poc": {"token": token}}
    return ToolResult(content=text, structured_content=data, meta=meta)


# --------------------------------------------------------------------------
# Approval: propose issues a single-use token, only the card can redeem it.
#
# ``visibility: ["app"]`` hides apply_task_status_change only in hosts that
# honour it; fastmcp still lists the tool. The token makes sure a model that
# does see the tool cannot write: it never gets a token, and each token is
# bound to one proposed change and works once.
# --------------------------------------------------------------------------

PENDING_TTL_SECONDS = 30 * 60


class _PendingChange(NamedTuple):
    project_name: str
    task_id: str
    from_status: str
    to_status: str
    expires_at: float


_PENDING: dict[str, _PendingChange] = {}


def _apply_task_status_change(token: str) -> ToolResult:
    """Apply a task status change approved on the confirmation card.

    Called by the card UI only, with the token from the proposal. Each token
    works once. Refuses if the task status changed since the card was shown.

    Args:
        token: Approval token issued by propose_task_status_change.

    Returns:
        What happened, as text and ``{"ok": ...}``.

    """
    change = _PENDING.pop(token, None)
    if change is None or change.expires_at < time.monotonic():
        return ToolResult(
            content="Not applied: this approval expired or was already used.",
            structured_content={"ok": False},
            is_error=True,
        )
    task = _task_card_data(change.project_name, change.task_id)
    if task["status"] != change.from_status:
        return ToolResult(
            content=(
                f"Not applied: status is now '{task['status']}', "
                f"the card showed '{change.from_status}'."
            ),
            structured_content={"ok": False, "current": task["status"]},
            is_error=True,
        )
    # Writes as the configured service user: the activity feed shows that
    # user, not the person who clicked Approve.
    api().update_task(
        change.project_name, change.task_id, status=change.to_status
    )
    return ToolResult(
        content=(
            f"Applied: {task['folder_path']}/{task['name']} status "
            f"'{change.from_status}' -> '{change.to_status}'."
        ),
        structured_content={"ok": True, "status": change.to_status},
    )


# AYON_MCP_READ_ONLY drops every mutating tool of the base server; the card
# must not become a write path around it.
if not read_only_enabled():
    mcp.tool(
        name="apply_task_status_change",
        app=AppConfig(visibility=["app"]),
        annotations={"destructiveHint": False},
    )(_apply_task_status_change)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=PORT,
        path=MCP_PATH,
        show_banner=False,
    )
