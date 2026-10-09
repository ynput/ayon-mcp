"""Addon tool execution provider."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from .addon_discovery import allowed_endpoint_prefix, is_within_prefix
from .rest_client import get_global_rest_client

if TYPE_CHECKING:
    from .addon_discovery import AddonTool
    from .rest_client import RestApiClient

logger = logging.getLogger(__name__)
_PLACEHOLDER = re.compile(r"\{([^{}/]+)\}")


class AddonToolProvider:
    """Provider for executing addon MCP tools."""

    def __init__(
        self,
        tools: list[AddonTool],
        client: RestApiClient | None = None,
    ) -> None:
        """Initialize the provider.

        Args:
            tools: List of discovered addon tools.
            client: Optional REST client. When omitted, the process-global
                client is resolved per call so the request-scoped API key
                set by the auth middleware is used.

        """
        self._tools = {t.full_name: t for t in tools}
        self._client = client

    def _resolve_client(self) -> RestApiClient:
        return self._client or get_global_rest_client()

    def has_tool(self, tool_name: str) -> bool:
        """Check if a tool exists in this provider."""
        return tool_name in self._tools

    def get_tool(self, tool_name: str) -> AddonTool | None:
        """Get a tool by name."""
        return self._tools.get(tool_name)

    def all_tools(self) -> list[AddonTool]:
        """Return all registered addon tools."""
        return list(self._tools.values())

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute an addon tool.

        Args:
            tool_name: Full namespaced tool name.
            arguments: Tool arguments from MCP call.

        Returns:
            Structured result with success flag and result/error.

        """
        tool = self._tools.get(tool_name)
        if tool is None:
            return {
                "success": False,
                "error": f"Addon tool '{tool_name}' not found.",
            }

        method = tool.endpoint.method.upper()
        path = tool.endpoint.path
        arguments = dict(arguments)

        # Substitute path parameters, e.g. /metrics/{project_name}
        for key, value in list(arguments.items()):
            placeholder = f"{{{key}}}"
            if placeholder in path:
                path = path.replace(placeholder, quote(str(value), safe=""))
                del arguments[key]

        unresolved = _PLACEHOLDER.findall(path)
        if unresolved:
            return {
                "success": False,
                "error": (
                    f"Missing required path argument(s): "
                    f"{', '.join(unresolved)}"
                ),
            }

        prefix = allowed_endpoint_prefix(tool.addon_name, tool.addon_version)
        if not is_within_prefix(path, prefix):
            return {
                "success": False,
                "error": (
                    f"Resolved path {path!r} is outside the addon "
                    f"prefix {prefix!r}."
                ),
            }

        client = self._resolve_client()
        try:
            if method == "GET":
                response = await client.request(
                    method=method,
                    path=path,
                    params=arguments or None,
                )
            else:
                response = await client.request(
                    method=method,
                    path=path,
                    json=arguments or None,
                )
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            logger.warning(
                "Addon tool %s returned %s: %s",
                tool_name,
                exc.response.status_code,
                detail,
            )
            return {
                "success": False,
                "error": (
                    f"Addon endpoint returned {exc.response.status_code}: "
                    f"{detail}"
                ),
            }
        except httpx.HTTPError as exc:
            logger.warning("Addon tool %s request failed: %s", tool_name, exc)
            return {
                "success": False,
                "error": f"Addon tool request failed: {exc}",
            }

        return {"success": True, "result": response}
