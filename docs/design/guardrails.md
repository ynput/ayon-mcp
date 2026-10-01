# Design: chuk-tool-processor guardrails for ayon-mcp

Status: draft
Author: Ondrej Samohel (with Claude Code)
Date: 2026-09-23 (revised 2026-09-24)

## 1. Problem

`ayon-mcp` exposes an AYON server (real production data, by default) to an
LLM through MCP tools. The README already carries a warning that this is not
recommended for production use, because an LLM-driven assistant can call the
wrong tool, or the right tool with wrong arguments, and write tools go
through AYON's operations endpoint with the calling user's real permissions.

Today the only safety mechanism is hand-rolled, in `tool_discovery.py`:

- `MUTATING_TOOL_NAMES` / `MUTATING_HTTP_METHODS` classify a tool as
  mutating (`services/mcp/ayon_mcp/tool_discovery.py:16-24`).
- `AyonDynamicToolProvider.execute_tool` requires the caller to pass
  `confirm_mutation=true` before running a mutating tool
  (`tool_discovery.py:159-195`).

That is one guard, of one kind (a confirmation flag), applied only to the
curated tool set. Gaps found while researching this:

1. **No enforced read-only mode.** `AYON_MCP_READ_ONLY` is documented in the
   repo's `CLAUDE.md` as a supported env var, but a repo-wide grep found no
   code that reads it. `confirm_mutation` is a client-supplied flag, not a
   server-side policy — a client can just always pass `true`.
2. **The generated OpenAPI/REST gateway tools bypass this entirely.**
   `call_rest_endpoint` and friends (README.md:146-179) build requests from
   the server's OpenAPI spec at runtime. They are not in
   `MUTATING_TOOL_NAMES`, and their target host is only ever
   `AYON_SERVER_URL`, but there's no explicit guard pinning that — a future
   change to `RestApiClient` or a crafted argument could point elsewhere
   (SSRF-shaped risk).
3. **No output-side protection.** A tool result can carry arbitrarily large
   payloads (unpaginated REST responses) or leak the API key/bearer token
   back into an error string; nothing catches that before it reaches the
   model.
4. **No loop/thrash control.** Nothing stops the model from hammering
   `search_ayon_tools` or repeatedly calling the same read tool in one turn.

The project already depends on `chuk-tool-processor>=0.26.1`
(`services/mcp/pyproject.toml:19`) for `BaseDynamicToolProvider`
(`AyonDynamicToolProvider` in `tool_discovery.py:88`), and that library ships
a `chuk_tool_processor.guards` package that covers all four gaps as
composable, independently testable units, with `GuardChain` to run them in
sequence. This doc proposes using it instead of growing more bespoke policy
code in `tool_discovery.py`.

## 2. Goals / non-goals

Goals:

- Make `AYON_MCP_READ_ONLY` actually enforced, server-side, not just
  documented.
- Classify every tool (curated + generated OpenAPI) by side effect
  (`read_only` / `write` / `destructive`) and gate execution on that
  classification plus the configured guard profile (`default`/`strict`).
- Constrain the REST gateway tools to `AYON_SERVER_URL` only.
- Cap output size and redact obvious secrets in tool results before they
  reach the model.
- Keep the existing `call_ayon_tool(tool_name, arguments, confirm_mutation)`
  contract stable — this is additive policy underneath it, not a new
  client-facing API.

Non-goals (not in this pass):

- Rewriting the discovery protocol (`list_tools`/`search_tools`/etc.) itself.
- Guards aimed at math/statistics workflows that ship with
  chuk-tool-processor as examples (`AssumptionTraceGuard`,
  `SaturationGuard`, `PreconditionGuard`) — not applicable to AYON's tool
  shape, skip them.
- Per-project or per-folder authorization — AYON's own permission system on
  the API key already governs that; guards operate one layer up, on the
  tool-call shape, not on AYON business rules.

## 3. Design

### 3.1 Where guards run

Single integration point: `AyonDynamicToolProvider.execute_tool` in
`tool_discovery.py:159-195`, which is the one place every curated tool call
already funnels through (`call_ayon_tool` → `provider.call_tool` →
`execute_tool`). The generated OpenAPI tools are registered as plain
functions today when `AYON_MCP_TOOL_EXPOSURE=direct`
(`server.py:186-187`), bypassing the provider entirely — see 3.4 for how
that path gets covered too.

```python
class AyonDynamicToolProvider(BaseDynamicToolProvider[AyonTool]):
    def __init__(self, tools: list[AyonTool], guard_chain: GuardChain) -> None:
        ...
        self._guard_chain = guard_chain

    async def execute_tool(self, tool_name, arguments) -> dict[str, Any]:
        tool = self._tools_by_name.get(tool_name)
        if tool is None:
            return {"success": False, "error": f"Tool '{tool_name}' was not found."}

        arguments = dict(arguments)
        confirmed = arguments.pop("confirm_mutation", False)

        guard_result = await self._guard_chain.check_all_async(tool_name, arguments)
        if guard_result.blocked:
            return {"success": False, "error": guard_result.final_reason}

        if tool.requires_confirmation and confirmed is not True:
            return {"success": False, "error": (...)}  # unchanged

        try:
            result = tool.function(**arguments)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        output_check = self._guard_chain.check_output_all(tool_name, arguments, result)
        if output_check.blocked:
            return {"success": False, "error": output_check.final_reason}

        return {"success": True, "result": result}
```

`confirm_mutation` stays exactly as-is — it is the user's explicit
per-call authorization and belongs at the application layer. Guards are the
policy layer underneath it: they decide whether this *kind* of call is
allowed at all right now, independent of what any individual caller claims.

### 3.2 Guard chain composition

```python
def build_guard_chain(tools: list[AyonTool]) -> GuardChain:
    classifications = classify_side_effects(tools)  # 3.3

    if read_only_enabled():
        mode = ExecutionMode.READ_ONLY
    elif guards_mode() is GuardsMode.STRICT:
        mode = ExecutionMode.WRITE_ALLOWED        # destructive blocked
    else:
        mode = ExecutionMode.DESTRUCTIVE_ALLOWED

    side_effect = SideEffectGuard(SideEffectConfig(
        mode=mode,
        explicit_classifications=classifications,
    ))

    network = NetworkPolicyGuard(NetworkPolicyConfig(
        allowed_domains={urlparse(ayon_server_url()).hostname},
        require_https=ayon_server_url().startswith("https://"),
        block_private_ips=False,  # local/dev AYON servers are routinely private-IP
    ))

    return GuardChain([
        ("schema", SchemaStrictnessGuard()),
        ("side_effect", side_effect),
        ("network", network),
        ("sensitive_output", SensitiveDataGuard()),
        ("output_size", OutputSizeGuard(OutputSizeConfig(max_bytes=..., mode=TruncationMode.TRUNCATE))),
        ("concurrency", ConcurrencyGuard()),
        ("per_tool", PerToolGuard()),
    ])
```

Ordering rationale: cheap structural checks (`schema`) before policy
(`side_effect`, `network`) before anything that needs to run alongside
execution (`concurrency`). `sensitive_output`/`output_size` are the two that
also register for `check_output_all` (post-execution).

`strict` profile (`AYON_MCP_GUARDS=strict`) differs from `default` in two
ways:

- `SideEffectGuard` runs in `WRITE_ALLOWED` mode, so `destructive` tools are
  blocked regardless of `confirm_mutation`.
- `RetrySafetyGuard` is appended. The provider records a failed attempt per
  `(tool, arguments)` signature and clears it on success; after 3 identical
  failures the call is blocked (gap #4, thrash control).

Guards *not* included, and why:

- `BudgetGuard`, `TimeoutBudgetGuard` — they count calls/time per *turn*
  and need the caller to reset them (`reset()`, `start_turn()`) at turn
  boundaries. An MCP server never sees the agent's turns, so process-wide
  counters would eventually lock the server for good. Not usable here
  without a turn signal from the client.
- `PlanShapeGuard`, `RunawayGuard` — scoped to a specific agent loop shape
  rather than ayon-mcp's baseline.
- `PreconditionGuard`, `SaturationGuard`, `AssumptionTraceGuard` — built for
  math/stats tool chains, no AYON equivalent.
- `ProvenanceGuard`, `ContractGuard` — would need `ToolContract` metadata
  attached to every `AyonTool`; out of scope until there's a concrete need
  for output lineage tracking.

### 3.3 Side-effect classification source of truth

`MUTATING_TOOL_NAMES` (`tool_discovery.py:16-23`) becomes the
`explicit_classifications` input, extended with a `destructive` tier so
`delete_entity` is distinguished from `create_entity`/`update_entity`:

```python
DESTRUCTIVE_TOOL_NAMES = frozenset({"delete_entity"})

def classify_side_effects(tools: list[AyonTool]) -> dict[str, SideEffectClass]:
    classifications = {}
    for tool in tools:
        if tool.name in DESTRUCTIVE_TOOL_NAMES:
            classifications[tool.name] = SideEffectClass.DESTRUCTIVE
        elif tool.requires_confirmation:  # existing MUTATING_TOOL_NAMES / HTTP-method check
            classifications[tool.name] = SideEffectClass.WRITE
        else:
            classifications[tool.name] = SideEffectClass.READ_ONLY
    return classifications
```

This reuses `_requires_confirmation` (`tool_discovery.py:52-59`) as-is, so
the existing docstring-based `Endpoint: POST/PUT/PATCH/DELETE` heuristic for
generated OpenAPI tools keeps working without duplicating logic.

### 3.4 Covering the generated OpenAPI tools in direct-exposure mode

When `AYON_MCP_TOOL_EXPOSURE=direct` (`server.py:186-187`), tools are
registered straight onto `FastMCP` and never pass through
`AyonDynamicToolProvider`. Two options, in preference order:

1. **Preferred:** treat `direct` mode as explicitly out of guard coverage
   and say so loudly — it already exists only "for compatibility"
   (README.md:132-133). Document that guardrails require discovery mode.
2. **If direct mode needs guarding too:** wrap each function from
   `tools_module.ALL_TOOLS` with a thin decorator that runs the same
   `GuardChain` before calling through, applied in `register_tools`
   (`server.py:107`). This duplicates the check outside the provider, so
   only do it if there's an actual user of direct mode who needs it.

Recommendation: ship option 1 first, revisit only if someone actually runs
direct mode against a guarded deployment.

### 3.5 Configuration surface

New env vars, following the existing `AYON_MCP_*` naming convention
(`README.md:20-21`, `169-171`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `AYON_MCP_READ_ONLY` | `false` | Enforced independently of the guard chain (fixes gap #1), see 3.7. |
| `AYON_MCP_GUARDS` | `default` | `default` (chain in 3.2) / `strict` (blocks `destructive` tools, adds `RetrySafetyGuard`) / `off` (skip guard chain entirely, for local debugging). Plain truthy/falsey values map to `default`/`off`; unknown values fall back to `default` with a warning. |

There is deliberately no deployment-environment variable (an earlier draft
had `AYON_MCP_ENVIRONMENT=dev|staging|prod`). AYON's `production`/`staging`
are *settings variants* of bundles, not deployment tiers of the server, so
reusing those names for guard policy would suggest a connection that
doesn't exist. Blocking destructive tools is a policy choice, expressed by
`AYON_MCP_GUARDS=strict`.

### 3.6 Failure shape

Guard failures return through the exact same `{"success": False, "error":
str}` contract the provider already uses for the existing
`confirm_mutation` and not-found cases (`tool_discovery.py:172-186`). No
change needed on the MCP client/model side — a blocked call just looks like
any other tool error, with a message identifying which guard fired
(`GuardChainResult.final_reason`, `stopped_at`).

### 3.7 Read-only mode

`AYON_MCP_READ_ONLY` is enforced in three layers, none of which depends on
`AYON_MCP_GUARDS`:

1. `create_mcp_server` drops every mutating tool (`drop_mutating_tools`,
   same `_requires_confirmation` heuristic as 3.3) before registering tools,
   in both `discovery` and `direct` exposure mode — so direct mode, which
   bypasses the provider (3.4), is covered too, and the model never sees
   write tools in discovery results.
2. `AyonDynamicToolProvider.execute_tool` refuses any mutating tool before
   the `confirm_mutation` check, even with the guard chain off.
3. When the guard chain is on, `SideEffectGuard` runs in `READ_ONLY` mode
   as a third layer.

The server instructions also get a short note that the server is read-only,
so the model explains a refused change instead of hunting for a workaround.

### 3.8 Per-tool access policy (proposed, not implemented)

Goal: let the operator, and optionally the MCP client, set each tool's
access level to `off` / `read` / `write` instead of the single global
`AYON_MCP_READ_ONLY` switch.

#### Why the client's own permissions aren't enough

MCP has no standard channel for a client to hand a server per-tool policy.
A client can only allow/deny on its side, or pass configuration to the
server. Client-side permissions (e.g. Claude Code `permissions.deny`) work
per MCP tool name, and in `discovery` mode every call goes through
`call_ayon_tool` — the client can only allow or deny all of it at once,
`delete_entity` and `get_project` look the same. That only works in
`direct` mode (`permissions.deny: ["mcp__ayon__delete_entity"]`). So the
policy has to be enforced server-side, with the client supplying it.

#### Policy shape

```yaml
# ayon-mcp-policy.yaml
default: read            # off | read | write
namespaces:
  ayon.settings: read    # get_addon_settings yes, set_addon_settings no
  ayon.write: write
  ayon.openapi.*: off    # whole generated layer hidden
tools:
  delete_entity: off
  add_comment: write
```

Levels, as seen by a single tool:

- `off` — never registered and never returned by list/search; the model
  doesn't know the tool exists.
- `read` — allowed only if the tool is classified `read_only` (3.3). A
  write/destructive tool at `read` behaves like `off`.
- `write` — allowed; write tools still require confirmation.

For a single tool, `read` and `off` collapse into the same thing — each
tool is inherently either a read or a write. The three-level model pays
off at the namespace level; per-tool entries are exceptions on top.
Resolution order: `tools` entry, then the most specific matching
`namespaces` entry, then `default`.

#### How the client supplies it

- **Local (stdio):** an env var in the client's MCP server config, e.g.
  `"env": {"AYON_MCP_POLICY": "D:/ayon/policy.yaml"}` in `.mcp.json`.
  Resolved once in `create_mcp_server`, the same place
  `drop_mutating_tools` runs today — no per-request work.
- **Remote (streamable-http):** the operator sets the ceiling server-side,
  via env var or, preferably, the AYON addon settings (ayon-mcp already
  ships as an addon). A client may narrow its own view with a request
  header, e.g. `x-ayon-mcp-policy`, handled next to `x-api-key` in
  `RemoteApiKeyMiddleware`. **The header can only restrict — the effective
  policy is the intersection of ceiling and header, never more than the
  ceiling.** Otherwise any client could grant itself write access.

  Because the policy can differ per request, filtering can't happen at
  registration time: it moves into `on_list_tools`/`on_call_tool`
  middleware (for `direct` mode) and into the provider's
  list/search/execute (for `discovery` mode), with the effective policy
  carried in a contextvar.

#### Enforcement points in the existing code

- Classification: reuse `classify_side_effects` (3.3) unchanged.
- Filtering: generalize `drop_mutating_tools(tools)` into
  `apply_policy(tools, policy)`.
- Per-call check: extend `AyonDynamicToolProvider._policy_error`, so a
  hidden tool can't be called by guessing its name.
- `SideEffectGuard` stays as a backstop.
- `AYON_MCP_READ_ONLY=true` becomes shorthand for `default: read` with no
  `write` overrides, and wins over any policy file.

#### Prerequisites

- **Namespace collision.** `create_curated_tools` derives the namespace
  from the last module-path segment only, so generated tools land in
  `ayon.settings`, `ayon.events`, `ayon.projects` — the same namespaces as
  the curated modules. They need `ayon.openapi.<module>` before a policy
  can target one layer without the other.
- **Confirmation.** `confirm_mutation` is set by the model, so it
  effectively means "the model says the user agreed". MCP elicitation
  (fastmcp `ctx.elicit`) lets the server ask the human directly through
  the client, which is a stronger basis for the `write` level. Keep
  `confirm_mutation` as a fallback for clients without elicitation
  support.

#### What stays the real authority

The AYON API key's access groups. This policy shapes what the model sees
and attempts; it is not an authorization system. For a hard read-only
deployment, use a service user with a read-only access group — then even
a bug in the policy code can't write.

#### Suggested order

1. Namespace fix (`ayon.openapi.<module>`).
2. Stdio policy file via `AYON_MCP_POLICY` — smallest change, static
   filtering at startup.
3. Remote ceiling in addon settings + restrict-only header, with
   per-request filtering.
4. Elicitation-based confirmation for `write`.

## 4. Rollout plan

1. Add `chuk_tool_processor.guards` usage behind `AYON_MCP_GUARDS=off`
   default initially (opt-in), verify against `tests/test_tool_discovery.py`
   and the existing `llm`-marked agent tests
   (`tests/test_llm_agent.py`, `tests/test_llm_agent_claude.py`) which
   already exercise real tool-calling behavior end to end.
2. Flip default to `AYON_MCP_GUARDS=default` once the guard chain has run
   against a real AYON server for both `list_*`/`get_*` (read) and
   `create_entity`/`update_entity`/`delete_entity` (write/destructive)
   without false positives.
3. Update `README.md`'s tool table and the production-use warning to
   describe the guard layer and the new env vars.
4. Fix the actual doc/code mismatch on `AYON_MCP_READ_ONLY` as part of step
   1 regardless of the rest of this plan — it's a pre-existing gap, not
   guard-dependent.

## 5. Open questions

- `OutputSizeGuard` truncation vs. the existing pagination
  (`count`/`truncated`/`next_offset`) on list tools — need to make sure the
  guard's truncation doesn't fight with tools that already paginate
  correctly; likely scope the guard to unpaginated paths (REST gateway,
  `query_graphql`) rather than applying it universally.
- Worth exposing guard chain state (e.g. guard profile, read-only) through
  `get_server_info` so the calling model/user can see what's enforced
  without reading env vars?
- Per-tool policy (3.8): should the remote ceiling live in AYON addon
  settings (per project? per access group?) or stay a server env/file?
  Addon settings give operators a UI and per-project scoping, at the cost
  of one settings fetch per session.
- Per-tool policy (3.8): header format for the restrict-only client
  override — inline JSON, or a named profile defined in the ceiling?
