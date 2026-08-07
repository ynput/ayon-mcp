## Plan: Ollama REST Bridge for AYON MCP

Build a new REST bridge service in the ayon-mcp repository that accepts frontend-oriented AYON requests, calls an Ollama model, and lets the model use AYON MCP tools by connecting to the existing MCP streamable HTTP endpoint. Deploy with a two-container compose topology (bridge + Ollama), reuse AYON API key auth semantics, and support optional NVIDIA GPU via compose profiles/device reservations.

**Steps**
1. Phase 1 - Service boundary and API contract
2. Define bridge endpoint contract and request/response models in a new module under services/mcp (for example chat, health, models, and tool-capabilities endpoints), explicitly mapping AYON-focused fields (project, task, context hints) to internal prompt/tool orchestration. This contract is the baseline for frontend integration and blocks implementation of runtime logic.
3. Decide and document how AYON API key is propagated: accept x-api-key from REST callers, pass it unchanged when creating MCP client transport headers, and reject requests without key (aligned with existing RemoteApiKeyMiddleware behavior). This blocks endpoint handlers.
4. Phase 2 - REST bridge runtime implementation
5. Add a new bridge application package under services/mcp (separate from current ayon_mcp server package) with an ASGI REST app and structured settings loader for OLLAMA_URL, AYON_MCP_URL, default model, timeout, and request limits.
6. Implement MCP client adapter using fastmcp client transport over streamable-http to the configured AYON MCP endpoint, including tool discovery caching and call-tool execution methods. Depends on step 3.
7. Implement Ollama adapter against /api/tags and /api/chat (or /api/generate) with retry/timeout behavior and optional streaming toggle; include model existence check and clear error mapping for unavailable models.
8. Implement orchestration flow in chat endpoint: build system/user messages with AYON context, allow iterative tool-call loop (model requests tool, bridge calls MCP tool, bridge feeds tool result back to model), and return final assistant message plus trace metadata. Depends on steps 6 and 7.
9. Add health/readiness endpoints: bridge readiness should validate both upstreams (AYON MCP endpoint reachable and Ollama endpoint reachable) and return structured status used by compose healthchecks.
10. Phase 3 - Containerization and local deployment
11. Create a dedicated Dockerfile for the bridge service (python slim + uv, mirroring existing build conventions) and expose bridge REST port.
12. Extend services/mcp/docker-compose.yml with two services: ollama and bridge. Configure internal networking by service name, persistent Ollama model volume, bridge env vars, dependency ordering with health conditions, and optional NVIDIA GPU profile (non-default). This can run in parallel with step 11 once runtime env variables are finalized.
13. Keep existing ayon-mcp service either as a third optional service in the same compose or document that bridge expects an external MCP URL; recommended default for local integration is including ayon-mcp in compose for an end-to-end stack. Depends on step 12.
14. Phase 4 - Tests and docs
15. Add unit tests for adapters (MCP adapter, Ollama adapter) with mocked HTTP/MCP responses, plus endpoint tests for auth validation, model-not-found errors, and tool-loop behavior.
16. Add an integration test path behind marker/environment flags that runs against real local compose services and validates one AYON-contextual prompt invoking at least one MCP tool.
17. Update README with setup and run instructions, env var table, GPU usage notes, and curl examples for frontend-style calls; include troubleshooting for missing x-api-key, unreachable MCP, and missing Ollama model.
18. Add a short architecture note describing request flow (frontend -> bridge -> Ollama <-> bridge -> MCP -> AYON) and boundaries between bridge and existing MCP server.

**Relevant files**
- c:/dev/ayon/repos/ayon-mcp/services/mcp/ayon_mcp/server.py - Existing MCP remote transport and x-api-key middleware behavior to mirror in bridge auth handling.
- c:/dev/ayon/repos/ayon-mcp/services/mcp/ayon_mcp/rest_client.py - Existing async HTTP client patterns and error handling style to reuse for Ollama/bridge upstream calls.
- c:/dev/ayon/repos/ayon-mcp/services/mcp/ayon_mcp/tools/__init__.py - Tool registry source for expected MCP tool naming and capability exposure.
- c:/dev/ayon/repos/ayon-mcp/services/mcp/docker-compose.yml - Primary compose file to extend with ollama and bridge services.
- c:/dev/ayon/repos/ayon-mcp/services/mcp/Dockerfile - Build conventions to mirror for new bridge Dockerfile.
- c:/dev/ayon/repos/ayon-mcp/pyproject.toml - Dependency and script registration updates for bridge runtime/tests.
- c:/dev/ayon/repos/ayon-mcp/tests/conftest.py - Fixtures and environment setup patterns to extend for bridge tests.
- c:/dev/ayon/repos/ayon-mcp/tests/test_server.py - Existing unit test style and organization to mirror for new bridge test modules.
- c:/dev/ayon/repos/ayon-mcp/README.md - Primary documentation location for deployment and API usage.

**Verification**
1. Unit tests: run targeted bridge tests and existing server tests to ensure no regressions in current MCP behavior.
2. Compose smoke test: start services/mcp compose stack, verify health checks for ollama and bridge, and confirm bridge can reach configured AYON MCP endpoint.
3. REST contract test: call bridge health/models/chat endpoints with and without x-api-key and validate expected status codes and payload schema.
4. Tool invocation test: send a chat request requiring AYON data retrieval, verify bridge executes at least one MCP tool call and returns merged final response.
5. GPU optional path: run compose with GPU profile on NVIDIA host and validate Ollama container starts with device access; confirm CPU-only default still works unchanged.

**Decisions**
- Included scope: planning for ayon-mcp repository only, with two-container topology in one compose (bridge + ollama).
- Included scope: custom AYON-focused REST endpoints (not OpenAI compatibility).
- Included scope: auth via reused AYON API key propagated as x-api-key.
- Included scope: optional NVIDIA GPU support, CPU-default deployment.
- Excluded scope: frontend implementation in addon UI, ayon-docker repo wiring, and production-grade multi-tenant auth beyond x-api-key propagation.

**Further Considerations**
1. Endpoint granularity recommendation: start with one chat endpoint plus health/models/tool-capabilities, then add specialized endpoints only after frontend usage data.
2. Context budget recommendation: define max prompt/tool-output token/size caps early to avoid oversized MCP payloads degrading Ollama responsiveness.
3. Observability recommendation: include structured request IDs and per-step timing in bridge logs from day one to simplify debugging model/tool orchestration.
