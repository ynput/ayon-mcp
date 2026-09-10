"""On-demand documentation of AYON concepts for assistants.

Keeps the server instructions short: depth lives here and is fetched
only when needed (progressive disclosure).
"""

from __future__ import annotations

_CONCEPTS = """\
# AYON core concepts

AYON organizes production data per **project**:

- **Folder** — hierarchy node (asset, shot, sequence, episode...).
  Has a `folderType`, a path like `/assets/characters/hero`, and
  optional attributes (fps, resolution, frame ranges...).
- **Task** — unit of work under a folder (e.g. `modeling`, `comp`).
  Has a `taskType` and `assignees` (AYON user names).
- **Product** — a published output group under a folder, typed by
  `productType` (model, rig, render, pointcache...). A product groups
  versions of "the same thing".
- **Version** — one numbered publish of a product (v001, v002...).
- **Representation** — one file format of a version (exr, mov, abc...),
  holding the actual file list.

So the chain is: folder → product → version → representation, and
folder → task for work management. **Workfiles** (DCC scene files per
task) are tracked too; reach them via `query_graphql` or the REST
gateway. Entity ids are 32-char hex strings.

Statuses, tags, folder/task types are **defined by each studio and
project** — there is no fixed set, and any names in examples are just
that. Read the actual values with `get_project_anatomy` before
writing. Attribute definitions
(names, types, enums) are studio-wide — read them with
`list_attributes`. Attributes with `inherit` enabled take their value
from the parent entity when not set explicitly, so a folder's fps can
come from the project.
"""

_QUERYING = """\
# Reading data effectively

1. `list_projects` → pick the project.
2. `get_folder_hierarchy` → one call for the whole folder tree with
   task names; best starting point.
3. `list_*` tools for filtered, paginated listings. When a response
   has `truncated: true`, pass `offset=next_offset` to get the next
   page. Keep `include_attrib` off unless attributes are needed.
4. `get_entity` for one entity with full detail.
5. `query_graphql` for anything the list tools cannot express
   (cross-entity joins, links, custom field selections). Explore the
   schema first with `get_graphql_schema` — no arguments for the query
   root, `type_name=...` for one type, `search=...` to find fields.
6. The REST gateway (`list_rest_endpoints`, `get_rest_endpoint`,
   `call_rest_endpoint`) reaches every other server endpoint.
"""

_WRITING = """\
# Writing data safely

Before any write, gather the vocabulary:

- `get_project_anatomy` — valid folderType/taskType/status/tags for
  the project.
- `list_attributes` — valid attribute names, types and enum values
  for `attrib` dicts.

Then:

- `create_entity` — requires per type: folder(name, folderType,
  parentId?), task(name, taskType, folderId), product(name,
  productType, folderId), version(version, productId).
- `update_entity` — send only the fields to change; `attrib` updates
  merge, other fields replace. `{"active": false}` archives.
- `delete_entity` — permanent; prefer archiving unless deletion was
  explicitly requested. The server refuses to delete entities with
  children.
- `add_comment` — Markdown comment on an entity's activity feed;
  @mentions use AYON user names.

All writes go through the AYON operations endpoint, so server-side
permissions, validation and events apply. Only write when the user
explicitly asked for a change.
"""

_SETTINGS = """\
# Addons, bundles and settings

- **Addon** — a versioned extension installed on the server
  (`list_addons`).
- **Bundle** — pins one version of each addon, plus the launcher
  version and dependency packages (`list_bundles`). Exactly one
  bundle is marked production and one staging at a time.
- **Variant** — settings are stored per addon version and variant,
  not per bundle: `production` and `staging` variants resolve through
  the bundle with that status; a dev bundle gets its own variant
  named after the bundle.
- **Levels** — studio settings are the defaults for everything;
  projects add overrides on top. (Site settings — per-machine — exist
  as a further level but are not covered by these tools.)

Reading: `get_addon_settings` returns resolved values (studio +
project overrides). Schema: `get_addon_settings_schema` returns the
JSON schema — check it before writing to learn keys, types and enums;
drill into nested objects with `path=[...]`.

Writing: `set_addon_settings` REPLACES all overrides at the chosen
level. To change one value: read current settings, keep what should
stay overridden, modify, then submit the whole object.

New projects are initialized from an **anatomy preset** (studio-level
template with roots, path templates, types and statuses); the preset
is copied at creation time, not linked. Presets are reachable via the
REST gateway under `/api/anatomy/presets`.
"""

_EVENTS = """\
# The event stream

Every change on the server emits an event (`entity.folder.created`,
`settings.changed`, `log.error`...). Services communicate through
events too.

- `list_events` — filter by topics (wildcards allowed, e.g.
  `entity.task.*`), project, user, state and time window; paginated
  via `offset`/`next_offset`.
- `get_event` — full payload of one event.
- `dispatch_event` — emit a custom event; namespace custom topics
  (e.g. `mytool.sync.finished`). `finished=False` creates a pending
  event another service may pick up.

Services process work by **enrollment**: they poll for pending events
of a source topic and create a dependent target event (`dependsOn`
links them), moving it pending → in_progress → finished/failed.

Useful for debugging: after a failed publish or sync, list recent
events with `statuses=["failed"]` or topic `log.error`.
"""

TOPICS: dict[str, tuple[str, str]] = {
    "concepts": (
        "Entity model: folders, tasks, products, versions, representations",
        _CONCEPTS,
    ),
    "querying": (
        "How to read data: hierarchy, list tools, pagination, GraphQL, REST",
        _QUERYING,
    ),
    "writing": (
        "How to write safely: required fields, anatomy checks, archiving",
        _WRITING,
    ),
    "settings": (
        "Addons, bundles, variants, settings levels and override semantics",
        _SETTINGS,
    ),
    "events": (
        "Event stream: topics, filtering, dispatching custom events",
        _EVENTS,
    ),
}


def get_documentation(topic: str | None = None) -> str:
    """Get concise documentation of AYON concepts and tool workflows.

    Call without arguments to list available topics; call with a topic
    name for the full text. Read the relevant topic before working on
    an unfamiliar area (e.g. "writing" before entity writes,
    "settings" before changing addon settings).

    Args:
        topic: One of "concepts", "querying", "writing", "settings",
            "events". Omit to list all topics.

    Returns:
        str: Topic documentation in Markdown, or the topic index.

    Raises:
        RuntimeError: If the topic is unknown; the error lists valid
            topics.

    """
    if topic is None:
        lines = ["Available topics (fetch one with `topic=...`):", ""]
        lines += [
            f"- {name}: {summary}"
            for name, (summary, _) in TOPICS.items()
        ]
        return "\n".join(lines)
    entry = TOPICS.get(topic.strip().lower())
    if entry is None:
        valid = ", ".join(TOPICS)
        msg = f"Unknown topic {topic!r}. Available topics: {valid}."
        raise RuntimeError(msg)
    return entry[1]
