# MCP Apps POC: interactive AYON cards in Claude

Proof of concept, not production code. It wraps the regular `create_mcp_server`
and adds three tools that render interactive HTML cards inside the chat
(the [MCP Apps](https://modelcontextprotocol.io/extensions/apps) extension,
`io.modelcontextprotocol/ui`). Nothing in `services/` changes.

| Tool | For | What it does |
|---|---|---|
| `show_project_insights` | model, read-only | Insights card: KPIs, status breakdown, task type x status, open tasks per assignee |
| `propose_task_status_change` | model, read-only | Confirmation card: task, current to target status, Approve / Cancel. Issues a single-use approval token in the result `_meta` |
| `apply_task_status_change` | card only (`visibility: ["app"]`) | Called by Approve with the token; refuses an unknown, used or expired (30 min) token, or a task whose status changed since the card was shown |

How the write is guarded:

- `visibility: ["app"]` hides the apply tool only in hosts that honour it;
  fastmcp still lists it in `tools/list`. A host without MCP Apps (Claude Code,
  Cursor, MCP Inspector) shows it to the model. The token is what stops the
  model: the token is only in `_meta`, which goes to the card, not the
  model, and each token is bound to one proposed change and works once.
  Tokens live in process memory, so a restart invalidates open cards.
- The base server still registers its own mutating tools (`update_entity`
  behind `confirm_mutation`). For a demo where the card is the only write
  path, run with `AYON_MCP_READ_ONLY=1`. In read-only mode the POC does not
  register `apply_task_status_change` at all and the card says it cannot
  apply changes.
- Writes go as the configured service user (no `x-as-user`), so the activity
  feed shows the service user, not the person who clicked Approve.
- Status and task type colours and icon names come from project anatomy as
  free strings; the server passes only hex colours and `[a-z0-9_]` icon names
  to the cards. Only statuses whose scope includes `task` can be proposed.

## Files

- `server.py` - the runner: tools, `ui://` resources, asset inlining
- `insights.html`, `confirm.html` - the cards
- `bridge.js` - minimal MCP Apps host bridge (postMessage JSON-RPC, no SDK, no CDN)
- `ayon.css` - AYON design tokens (dark and light) copied from
  `ayon-react-components` `src/theme/tokens.css`

At start the server inlines Nunito Sans, the AYON logo and a Material Symbols
subset (only the icons the projects' statuses and task types use, about 11 KB,
fetched once from Google Fonts) into each card. The iframe cannot load the
AYON frontend's own assets. Collecting the icons reads every project's
anatomy one by one and sends the icon names to Google Fonts: fine on a dev
server, slow on one with many projects.

## Run

```sh
AYON_SERVER_URL=http://localhost:5000 \
AYON_API_KEY=<service user key> \
POC_MCP_PATH=/mcp-<random> \
POC_ARC_FONTS=../ayon-react-components/dist/fonts \
POC_AYON_LOGO=../ayon-frontend/public/AYON.svg \
.venv/bin/python scripts/poc_mcp_apps/server.py
```

It listens on `127.0.0.1:8088`. Expose it with a tunnel and add
`https://<host><POC_MCP_PATH>` as a custom connector in claude.ai. claude.ai
cannot send an API key header, so the random path is the only protection: anyone
with the URL acts as the configured service user. Use a dev server only.

Without `POC_ARC_FONTS` / `POC_AYON_LOGO` the cards fall back to system fonts
and a text logo.

Try:

- "How is demo_Big_Episodic doing? Show me project insights."
- "Move task <task id> in demo_Big_Episodic to Approved."

## Findings

- **It works in claude.ai** with a custom connector: both cards render, the
  Approve button's `tools/call` to an app-only tool goes through, and the card
  follows the Claude theme (`hostContext.theme`, not the OS setting).
- **claude.ai caches the card HTML per connector URL.** It calls
  `resources/read` only when the connector is first connected, never per tool
  call. A new `ui://` URI (the runner puts a content hash in it), or removing and
  re-adding the connector with the same URL, still showed the old card. Only a
  new connector URL fetched the new HTML. So a card change we deploy may not
  reach users who are already connected. This needs an answer before production:
  a thin shell card that loads its UI at render time (if the host CSP allows
  it), the version in the tool name, or guidance from Anthropic on cache
  invalidation.
- The model's text reply is written before the user clicks, so it can say
  "nothing changed yet" next to an "Applied" card. The card sends
  `ui/update-model-context` after Approve or Cancel so the next turn knows.
- Cancel is local to the card. If the host renders the card again (reload),
  Approve shows again and works while the token is unused and not expired.
- Insights reads all tasks of the project (about 0.7 s for 7,181 tasks). Fine
  for a POC; production should use a server-side aggregate.
