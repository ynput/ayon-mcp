/* Minimal MCP Apps bridge (postMessage JSON-RPC, no SDK, no CDN).
   Calls window.onToolResult(structuredContent, _meta) when the host
   delivers the tool result. */
const pending = new Map(); let nextId = 1;
const post = (m) => window.parent.postMessage(m, "*");
const request = (method, params) => new Promise((resolve, reject) => {
  const id = nextId++; pending.set(id, { resolve, reject });
  post({ jsonrpc: "2.0", id, method, params });
});
const notify = (method, params) => post({ jsonrpc: "2.0", method, params });
window.addEventListener("message", (e) => {
  if (e.source !== window.parent) return;
  const m = e.data; if (!m || m.jsonrpc !== "2.0") return;
  if (m.id !== undefined && !m.method) {
    const p = pending.get(m.id); if (!p) return; pending.delete(m.id);
    m.error ? p.reject(m.error) : p.resolve(m.result); return;
  }
  if (m.method === "ui/notifications/tool-result" && window.onToolResult) {
    window.onToolResult(m.params && m.params.structuredContent, m.params && m.params._meta);
  }
  if (m.method === "ui/notifications/host-context-changed") applyTheme(m.params);
  if (m.id !== undefined) post({ jsonrpc: "2.0", id: m.id, result: {} });
});
new ResizeObserver(() => notify("ui/notifications/size-changed", {
  width: document.documentElement.scrollWidth,
  height: document.documentElement.scrollHeight,
})).observe(document.body);
/* Theme follows the Claude app setting (hostContext.theme), not the OS.
   prefers-color-scheme is only the fallback when the host sends no theme. */
let hostTheme = null;
function applyTheme(ctx) {
  if (ctx && (ctx.theme === "light" || ctx.theme === "dark")) hostTheme = ctx.theme;
  const dark = hostTheme ? hostTheme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}
applyTheme(null);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(null));

request("ui/initialize", {
  protocolVersion: "2026-01-26",
  appInfo: { name: document.title, version: "0.0.3" },
  appCapabilities: {},
}).then((r) => applyTheme(r && r.hostContext))
  .finally(() => notify("ui/notifications/initialized", {}));

const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const icon = (name, cls = "", style = "") =>
  `<span class="icon ${cls}" style="${style}">${esc(name)}</span>`;
const statusEl = (s) =>
  `<span class="status" style="--c:${esc(s.color)}">${icon(s.icon, "sm")}${esc(s.name)}</span>`;
