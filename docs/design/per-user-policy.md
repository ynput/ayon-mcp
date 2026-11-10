# Design: per-user / per-team MCP policy from addon settings

Status: phases 1 to 3 implemented (issue #33), decisions taken 2026-11-10 (see section 7)
Date: 2026-11-10
Builds on: `guardrails.md` section 3.8 (per-tool access policy, proposed)

## 1. Goal

Today every policy knob is a service-wide environment variable, read once
at startup:

| Variable | Read in | Effect |
| --- | --- | --- |
| `AYON_MCP_GUARDS` | `guardrails.guards_mode()` | `off` / `default` / `strict` guard chain |
| `AYON_MCP_READ_ONLY` | `guardrails.read_only_enabled()` | mutating tools dropped at startup + refused in provider |
| `AYON_MCP_ADMIN_TOOLS` | `rest_policy.admin_tools_enabled()` | admin/security REST tools dropped at startup unless set |
| `AYON_MCP_ENABLE_OPENAPI_TOOLS` | `tools.openapi_tools_enabled()` | REST tool layer on/off |

All callers of one service get the same policy. The goal is to let a studio
admin set these per AYON user and/or team in the MCP addon's settings, plus
a tool allowlist and an OpenAPI (REST tool) allowlist per user/team.

## 2. Findings that shape the design

### 2.1 AYON has no global "team"

Teams are project data: `project.data.teams[].members[].name`
(`ayon_server/entities/user.py:526`, `UserEntity.get_teams(project)`).
The same team name in two projects is two unrelated teams. An MCP tool
call is not tied to one project (`list_projects`, `query_graphql`, REST
tools span projects), so "the user's team" is undefined without a project.

Identity attributes that *are* global:

- user name
- user level: `isAdmin`, `isManager`, `isDeveloper`, `isGuest`, `isService`
- `defaultAccessGroups` (list) — per-project `accessGroups` also exist

So a rule can match globally on: user names, user level, default access
groups. Team matching has two possible meanings, see 4.2.

### 2.2 Identity is available, differently per transport

| Transport | How the caller is known | Cost to learn level/groups |
| --- | --- | --- |
| tunnel (ASH default) | `x-ayon-mcp-user` set by the addon (`ServiceUserMiddleware`) | one `GET /api/users/{name}` with the service key, cacheable |
| remote http | `x-api-key` per request (`RemoteApiKeyMiddleware`) | `GET /api/users/me` with that key, cacheable per key hash |
| stdio (local) | single `AYON_API_KEY` | one `GET /api/users/me` at startup |

`guardrails.caller_identity()` already distinguishes callers for the retry
guard; the new resolver extends this to a full identity record.

### 2.3 The policy is applied at startup today

`server.create_mcp_server` calls `drop_admin_tools` and
`drop_mutating_tools` before registering anything, and
`AyonDynamicToolProvider` builds one guard chain with one
`ExecutionMode`. Per-user policy means the registered superset must be
filtered per request instead.

### 2.4 Identity is only set for `tools/call`

All three auth middlewares implement only `on_call_tool`. In `discovery`
exposure mode that is enough: `list_ayon_tools`, `search_ayon_tools` and
schema lookups are themselves tool calls. In `direct` mode, MCP
`tools/list` runs without identity, so per-user listing needs the
middlewares to also hook `on_list_tools` (or `on_request`).

### 2.5 chuk's discovery base class caches and indexes globally

`BaseDynamicToolProvider` (chuk-tool-processor):

- `search_tools` re-indexes when `len(get_all_tools())` changes, but the
  engine scores the tool list it is given (`search(tools=...)`); the
  inverted index it builds is never read. A per-user `get_all_tools()`
  therefore can't leak or drop tools between callers, even with
  equal-sized visible sets; it only costs a re-index when the size
  changes.
- `get_tool_schema` caches schemas by name in `_tool_cache`, shared by all
  users.
- `filter_search_results` is a hook, but runs after the engine took
  `limit * 2` candidates, so heavy filtering returns too few results.

So `get_all_tools()` returns the caller's visible subset, which covers
`list_tools`, `search_tools` and alias resolution of the base class.
`get_tool_schema`, `call_tool` and `execute_tool` check access themselves,
because the schema cache is shared.

### 2.6 Addon settings are readable by the service

The service user's key can read
`GET /api/addons/mcp/{version}/settings?variant=<variant>`. The version is
known under ASH (`AYON_ADDON_VERSION`) and otherwise resolvable from the
production bundle — the same lookup `tunnel.resolve_tunnel_url` already
does. Variant: reuse `server.addon_tools_variant()`.

`SettingsField` supports `enum_resolver` (async callable), so the settings
UI can offer pickers for user names and access groups.

## 3. Options considered

### 3.1 Where the policy is resolved

**A. In the service (recommended).** The service fetches addon settings
(TTL cache) and the caller's user record (TTL cache), and resolves the
effective policy itself. Works for all three transports with one code path.

**B. In the server addon, tunnel only.** `MCPAddon.mcp_endpoint` already
has the `UserEntity` and its own settings; it could resolve the policy and
forward it as a header next to `x-ayon-mcp-user` (dropping any client copy,
like `USER_HEADER`). Cheaper (no extra round trips) and resolves teams with
direct DB access, but covers tunnel mode only and duplicates the resolver
across the `server/` and `services/` packages.

**C. Policy on the user entity** (`user.data` / a custom attribute).
Rejected: scattered across users, no team rules, no settings UI.

Recommendation: A for v1. B is an optional later optimization for tunnel
mode, sharing the resolver as a small pure module vendored into both.

### 3.2 How env vars and settings combine

**Ceiling (chosen).** The env var is the most permissive value the
service allows; settings can only tighten within it. Each settings knob is
tri-state with `inherit` (= env value) as the default.

- `read_only`: effective = env OR settings.
- `guards`: effective = stricter of env and settings
  (`off` < `default` < `strict`). Settings can set `off` only if env is
  `off`.
- `admin_tools`: effective = env AND settings. To give admin tools to
  some users only, set `AYON_MCP_ADMIN_TOOLS=true` on the service and
  `admin_tools: off` in the rules for everyone else (and the default
  rule). No extra MCP-side admin check: AYON itself refuses admin
  endpoints for non-admin callers.
- `openapi.rest_tools`: effective = env AND settings.

Consequences: startup filtering by env stays correct as-is (it removes what
nobody can ever get), existing deployments behave identically while
settings are all `inherit`, and a compromised or careless settings change
can never exceed what the operator configured on the service.

**Settings authoritative** (alternative): new
`AYON_MCP_POLICY_SOURCE=settings` makes settings override env both ways.
Simpler mental model for admins, but removes the operator's hard ceiling
and forces registering admin tools always. Not recommended.

### 3.3 Combining multiple matching rules

**Most-restrictive merge (chosen).** Every matching rule applies and the
strictest value of each knob wins. Safe by construction: adding a rule can
never widen anyone's access. Grants come from the env ceiling, not from
rules — a rule can only take away. The default rule applies only to
callers no other rule matches; otherwise it would restrict everyone and
no rule could undo it.

**Ordered rules, first match wins.** Rejected: precedence depends on list
order, and a broad rule placed first silently widens everyone in it.
**Most-permissive merge.** Rejected for the same reason.

Per knob, over the matched rules (`inherit` in a rule = no opinion, it
does not take part):

| Knob | Merge |
| --- | --- |
| `guards` | strictest (`off` < `default` < `strict`) |
| `read_only` | any `on` wins |
| `admin_tools` | any `off` wins |
| `openapi.rest_tools` | any `off` wins |
| `openapi.max_side_effect` | lowest |
| `tools` / `openapi` filters | a tool must pass every matched rule's filter: allowlists intersect, denylists add up |

The result is then capped by the env ceiling (3.2).

## 4. Proposed design

### 4.1 Settings model (`server/settings.py`)

```python
class Inherit(StrEnum): inherit / on / off            # bool knobs
class GuardsChoice(StrEnum): inherit / off / default / strict

class ListMode(StrEnum):
    all = "all"               # no filtering (no opinion)
    allowlist = "allowlist"   # only matching patterns
    denylist = "denylist"     # all except matching patterns

class ToolFilter(BaseSettingsModel):
    mode: ListMode = "all"
    patterns: list[str] = []  # globs or re: regexes, see 4.3

class OpenApiFilter(ToolFilter):
    rest_tools: Inherit = "inherit"  # not `enabled`: the frontend renders that as a header toggle
    max_side_effect: inherit / read_only / write / destructive

class PolicyEffect(BaseSettingsModel):
    guards: GuardsChoice = "inherit"
    read_only: Inherit = "inherit"
    admin_tools: Inherit = "inherit"
    tools: ToolFilter
    openapi: OpenApiFilter

class PolicyRule(PolicyEffect):
    name: str
    enabled: bool = True
    # subject - rule matches if ANY listed criterion matches
    users: list[str]           # enum_resolver: users
    user_levels: list[str]     # admin / manager / user / guest / service
    # patterns in tools/openapi filters: glob, or regex with `re:` (4.3)
    access_groups: list[str]   # enum_resolver: access groups

class MCPSettings(BaseSettingsModel):
    default_policy: PolicyEffect   # only for callers no rule matches
    rules: list[PolicyRule] = []   # all matching rules merge, strictest wins
    policy_refresh_seconds: int = 60
```

Defaults are all `inherit` / `all` with no rules, so installing the new
addon version changes nothing: empty settings mean the env vars decide.
`scope=["studio"]` on everything.

### 4.2 Group matching

v1 matches on access groups only, no teams. A caller's access groups are
`defaultAccessGroups`; per-project `accessGroups` are not used, since most
calls have no project. Access groups are the AYON-native way to group
users globally, and the settings UI can list them with an enum resolver.

Teams are deferred. When added, the two possible meanings are "member of
team T in any project" (loose: the same name in unrelated projects
matches) and project-scoped rules evaluated per call from the tool's
`project_name` argument (correct, but listing and execution can disagree,
and many tools have no project).

### 4.3 Tool and OpenAPI patterns

Two pattern syntaxes:

- **Glob** (default): `fnmatch`-style, case-sensitive (`fnmatchcase`),
  e.g. `set_*`.
- **Regex**: prefixed with `re:`, e.g. `re:^(get|list)_.*$`. Matched with
  `re.fullmatch`, so `re:get_.*` does not match `forget_x`. Use `.*`
  explicitly for partial matches.

Patterns are validated in the settings model (a pydantic validator
compiles every `re:` pattern), so an invalid regex cannot be saved. If
one still reaches the service (settings edited through the API around
the validator), that rule's filter matches no tool — for an allowlist
this hides everything, for a denylist nothing extra is hidden — and the
service logs an error. Compiled patterns are cached per settings load.

A pattern matches if it matches any of:

- tool name: `get_folders`, `set_*`
- namespace: `ayon.addon.shotgrid`, `ayon.openapi.projects`
- qualified name `<namespace>.<name>`: `ayon.entities.*`,
  `ayon.write.delete_*`
- REST endpoint (OpenAPI filter only): `GET /api/projects/*`,
  `* /api/addons/*/settings` — uses `rest_policy.endpoint_of`

`tools` applies to curated and addon tools, `openapi` to REST tools
(identified by `rest_tool_of`). `openapi.max_side_effect` caps REST tools
separately from curated ones, e.g. curated writes allowed but REST
read-only.

Filter mode has no `inherit`: `all` is "no opinion", and a rule's
filter adds to the others (a tool must pass every matched rule's
filter), so there is nothing to inherit. An allowlist without patterns
allows no tool.

Tool names are dynamic (server's OpenAPI spec, addon tools), so the
settings UI cannot validate them. Mitigation: the "explain" output in 4.7.

### 4.4 Resolution

New module `services/mcp/ayon_mcp/policy.py`:

```python
@dataclass(frozen=True)
class Identity:
    name: str; level: str; access_groups: frozenset[str]

@dataclass(frozen=True)
class EffectivePolicy:
    guards: GuardsMode; read_only: bool; admin_tools: bool
    openapi_enabled: bool; openapi_max_side_effect: SideEffectClass
    tools: Filter; openapi: Filter
    matched_rules: tuple[str, ...]   # empty = default policy

def resolve(identity, settings, env) -> EffectivePolicy   # pure, unit-tested
def is_visible(tool: AyonTool, policy) -> bool             # pure
```

`PolicyStore` holds the TTL-cached settings and identity caches
(`policy_refresh_seconds`, keyed by user name / API key hash, bounded LRU
like `ServiceUserMiddleware._clients`). The auth middlewares resolve the
`EffectivePolicy` and set it in a contextvar next to the client, so
`asyncio.to_thread` calls see it (as `_invoke` already relies on).

### 4.5 Enforcement points

- **Startup** (`create_mcp_server`): unchanged — env ceiling drops what
  nobody may use.
- **Provider** (`AyonDynamicToolProvider`):
  - `get_all_tools()` returns the caller's visible subset (2.5), so the
    inherited `list_tools` and `search_tools` only see allowed tools;
  - `get_tool_schema` / `call_tool` / `execute_tool`: a hidden admin
    tool behaves exactly like an unknown one ("was not found"). A write
    tool hidden by read-only mode is refused with the reason instead, so
    the model explains the refusal rather than hunting for a workaround
    (write tools existing is no secret);
  - `_policy_error`: use `policy.read_only` instead of `self._read_only`;
  - guard chain: cache one chain per `(guards, read_only)` pair (at most
    5 variants; with the env ceiling usually fewer), all sharing the one
    `CallerScopedRetrySafetyGuard` instance — extends the existing retry-guard carry-over in `_set_tools`.
- **Direct mode** (phase 3): the auth middlewares also bind the caller
  in `on_list_tools` (without failing when a listing carries no
  credentials), and `DirectPolicyMiddleware`, added right after the auth
  middleware, filters `tools/list` and refuses calls to hidden tools with
  the provider's messages. Only visibility applies: guards and mutation
  confirmation stay discovery-mode only, the stance `guardrails.md` 3.4
  takes.
- **Instructions**: server instructions are sent once at `initialize`,
  before the policy matters per call. Keep them generic; refusals carry
  the reason ("read-only for your user by MCP policy rules 'X', 'Y'").

### 4.6 Failure behaviour

- The server always provides the addon settings, possibly empty (all
  `inherit`, no rules). Empty settings mean the env vars decide alone.
- A transient fetch error during refresh: keep the last loaded copy.
- User lookup fails: treat as matching only the default policy.
- `AYON_MCP_POLICY_SETTINGS=false` skips the settings entirely; every
  caller gets the env var policy (also the test default).
- Which key reads the settings: always the service's startup
  `AYON_API_KEY` (already used for the OpenAPI spec and addon
  discovery), never the caller's key — in remote mode a non-admin
  caller's key may not be allowed to read addon settings.

### 4.7 Observability

- `get_server_info` returns the caller's effective policy
  (`mcpPolicy`: settings, filters, matched rules) when called through the
  discovery provider, which puts the policy in a contextvar.
- CLI: `ayon-mcp --explain-policy <user>` prints the user's identity,
  resolved policy and visible tools by namespace — the answer to "why
  can't X see tool Y". Mirrors the service's configuration: curated, REST
  and (in discovery mode) addon tools, one addon discovery run; the addon
  settings apply unless `AYON_MCP_POLICY_SETTINGS` is off, reported as
  `settings_used`. Needs an admin or service key (reads
  `/api/users/{name}`).
- Log the matched rules per call at debug level.

## 5. Security notes

- The policy narrows what the model sees and attempts. AYON access groups
  on the caller stay the real authority, as in `guardrails.md` 3.8.
- Settings alone can never loosen anything: `guards: off` in a rule has
  effect only if the env is `AYON_MCP_GUARDS=off` too (ceiling), and
  rules only restrict each other (strictest merge).
- Admin tools need no MCP-side admin check: AYON refuses admin endpoints
  for non-admin callers. The policy only decides whether they are shown.
- In tunnel mode the user name is trusted because the addon sets it and
  strips client copies. In remote mode identity comes from the key the
  client sends — a user cannot pick another user's policy without that
  user's key.
- Changing the settings model is a schema change of the addon; keep all
  defaults `inherit` and bump the addon minor version.

## 6. Plan

**Phase 1 — resolver and global knobs (discovery mode)**

1. `server/settings.py`: models from 4.1 without `tools`/`openapi`
   filters; enum resolvers for users and access groups.
2. `policy.py`: `Identity`, `EffectivePolicy`, pure `resolve()` with
   strictest merge (3.3) and env ceiling (3.2); unit tests covering every
   env × settings combination, overlapping rules and empty settings.
3. `PolicyStore`: settings + identity fetch with TTL, addon version
   lookup shared with `tunnel.py`.
4. Middlewares set the policy contextvar.
5. Provider: per-request `read_only`, guard chain variants, admin-tool
   visibility; overrides from 4.5.
6. Extend `tests/fake_ayon_server.py` with settings, `users/me`,
   `users/{name}` endpoints; tests per transport.

**Phase 2 — tool and OpenAPI allowlists**

7. `ToolFilter` / `OpenApiFilter` models and `is_visible()` pattern
   matching (name, namespace, `METHOD /path`; glob and `re:` regex,
   regex validated in the settings model).
8. `openapi.max_side_effect`.
9. Explain tool + CLI (4.7).

**Phase 3 — direct exposure mode**

10. `on_list_tools` / `on_call_tool` filtering: auth middlewares bind
    the caller for listings too; `DirectPolicyMiddleware` filters.

**Later (not planned yet)**

11. Team matching or project-scoped rules (4.2).
12. Addon-side resolution for tunnel mode (3.1 B).

**Docs**: README configuration section, `guardrails.md` 3.8 updated to
point here.

## 7. Decisions

1. Env vars stay a ceiling (3.2); settings can only tighten.
2. Groups: access groups only (`defaultAccessGroups`); teams deferred
   (4.2).
3. Overlapping rules: the strictest value of each knob applies (3.3).
4. Admin tools: no MCP-side admin check, AYON enforces it (5).
5. Settings always load from the server; empty settings mean the env vars
   decide. No fail-closed switch (4.6).
6. Service users are matched like any other caller: by name, by the
   `service` user level, or by access groups. In tunnel mode the caller
   is the user the AYON server authenticated, which may itself be a
   service user; the MCP service's own key is never the caller.
7. Filter patterns: globs by default, regex with a `re:` prefix (4.3).
