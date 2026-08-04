"""Client for interacting with the AYON server API."""
from contextvars import ContextVar

import ayon_api

# ContextVar ensures thread/async-task safety
_CLIENT_CV: ContextVar[ayon_api.ServerAPI | None] = ContextVar(
    "api_client", default=None)


def get_ayon_api(server_url: str, api_key: str) -> ayon_api.ServerAPI:
    """Return an instance of the API client.

    Args:
        server_url: AYON server URL (e.g. http://localhost:5000)
        api_key: AYON API key

    Returns:
        An instance of the AYON API client.

    Raises:
        RuntimeError: If the API key is invalid or the server is unreachable.

    """
    server = ayon_api.ServerAPI(server_url, token=api_key)

    if not server.has_valid_token:
        msg = (
            f"Could not authenticate against AYON server at {server_url!r}. "
            "Check that AYON_API_KEY is a valid, non-expired API key "
            "and that the server is reachable."
        )
        raise RuntimeError(
            msg
        )

    return server


def set_global_ayon_client(client: ayon_api.ServerAPI) -> None:
    """Set the global AYON API client for the current context.

    This function should be called once during application startup, after
    creating the client with `get_ayon_api`. It ensures that all tool modules
    use the same client instance.

    Args:
        client: An instance of the AYON API client.

    """
    _CLIENT_CV.set(client)


def get_global_ayon_client() -> ayon_api.ServerAPI:
    """Get the global AYON API client for the current context.

    All tool modules should use this function to access the AYON API client,
    ensuring that they all share the same instance set during application
    startup.

    Returns:
        An instance of the AYON API client.

    Raises:
        RuntimeError: If the client has not been initialized yet.

    """
    client = _CLIENT_CV.get()
    if client is None:
        msg = (
            "AYON API client has not been initialized yet. "
            "Call set_global_ayon_client() during application startup."
        )
        raise RuntimeError(msg)
    return client
