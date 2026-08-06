"""Shared REST client for OpenAPI-backed MCP tools."""

from __future__ import annotations

from typing import Any

import httpx

from .client import get_global_ayon_api_key

_REST_CLIENT: RestApiClient | None = None


class RestApiClient:
    """Wrapped shared HTTP client for direct server endpoint interaction."""

    def __init__(self, base_url: str):
        """Initialize the RestApiClient with base URL."""
        self.http_client = httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self.http_client.aclose()

    async def request(  # ruff: ignore[too-many-arguments]
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send a request and return parsed JSON when possible.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: URL path for the request.
            params: Query parameters for the request.
            json: JSON body for the request.
            data: Raw body for the request.
            headers: Additional headers for the request.

        Returns:
            Parsed JSON response if content type is application/json,
            otherwise raw text or None.

        """
        request_headers = dict(headers or {})
        if "x-api-key" not in request_headers:
            request_headers["x-api-key"] = get_global_ayon_api_key()

        response = await self.http_client.request(
            method=method.upper(),
            url=path,
            params=params,
            json=json,
            data=data,
            headers=request_headers,
        )
        response.raise_for_status()

        content_type = (response.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            return response.json()
        if response.text:
            return response.text
        return None


def set_global_rest_client(client: RestApiClient | None) -> None:
    """Set process-global RestApiClient used by generated OpenAPI tools."""
    global _REST_CLIENT
    _REST_CLIENT = client


def get_global_rest_client() -> RestApiClient:
    """Get the configured RestApiClient for generated OpenAPI tools.

    Returns:
        RestApiClient: The global RestApiClient instance.

    Raises:
        RuntimeError: If the RestApiClient has not been initialized yet.

    """
    if _REST_CLIENT is None:
        msg = (
            "REST API client has not been initialized yet. "
            "Call set_global_rest_client() during application startup."
        )
        raise RuntimeError(msg)
    return _REST_CLIENT
