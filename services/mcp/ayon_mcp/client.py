"""Client for interacting with the AYON server API."""
from contextvars import ContextVar

import ayon_api

# ContextVar ensures thread/async-task safety
_CLIENT_CV: ContextVar[ayon_api.ServerAPI | None] = ContextVar(
    "api_client", default=None)
_API_KEY_CV: ContextVar[str | None] = ContextVar("api_key", default=None)
_AS_USER_CV: ContextVar[str | None] = ContextVar("as_user", default=None)
_DELEGATED_CV: ContextVar[str | None] = ContextVar("delegated", default=None)

# Sent with ``x-as-user`` when the service acts for an application of the
# user (an MCP client signed in with OAuth), naming its client id. AYON then
# gives the request the limits of an application: no credentials, accounts
# or secret user data. Servers without support ignore it.
AS_APPLICATION_HEADER = "x-as-application"


class _ApplicationServerAPI(ayon_api.ServerAPI):
    """AYON client of a service acting as a user for an application."""

    def __init__(self, base_url: str, token: str, application: str) -> None:
        # ``ServerAPI.__init__`` may already build the headers.
        self._application = application
        super().__init__(base_url, token=token)

    def get_headers(self, content_type: str | None = None) -> dict[str, str]:
        """Return the request headers, naming the application.

        Returns:
            The headers of ``ServerAPI`` with ``AS_APPLICATION_HEADER``.

        """
        headers = super().get_headers(content_type)
        headers[AS_APPLICATION_HEADER] = self._application
        return headers


def get_ayon_api(
    server_url: str, api_key: str, application: str | None = None
) -> ayon_api.ServerAPI:
    """Return an instance of the API client.

    Args:
        server_url: AYON server URL (e.g. http://localhost:5000)
        api_key: AYON API key
        application: Client id of the application a service acts as a
            user for, sent as ``AS_APPLICATION_HEADER``.

    Returns:
        An instance of the AYON API client.

    Raises:
        RuntimeError: If the API key is invalid or the server is unreachable.

    """
    if application is None:
        server = ayon_api.ServerAPI(server_url, token=api_key)
    else:
        server = _ApplicationServerAPI(
            server_url, token=api_key, application=application
        )

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


def set_global_ayon_api_key(api_key: str) -> None:
    """Set the AYON API key for the current execution context.

    Args:
        api_key: AYON API key used for the current request.

    """
    _API_KEY_CV.set(api_key)


def set_global_ayon_as_user(user_name: str | None) -> None:
    """Set the AYON user a service API key acts as in this context.

    Args:
        user_name: User to act as (sent as ``x-as-user``), or ``None``
            to act as the API key's own user.

    """
    _AS_USER_CV.set(user_name)


def get_global_ayon_as_user() -> str | None:
    """Get the AYON user the service API key acts as in this context.

    Returns:
        The user name, or ``None`` if requests act as the key's own user.

    """
    return _AS_USER_CV.get()


def set_global_ayon_delegated_to(client_id: str | None) -> None:
    """Set the application acting as the user in this context, if any.

    Args:
        client_id: Client id of the application (a delegated session, such
            as an OAuth access token), or ``None`` when the user calls in
            person.

    """
    _DELEGATED_CV.set(client_id)


def get_global_ayon_delegated_to() -> str | None:
    """Get the application acting as the user in this context, if any.

    Requests acting as the user name it in ``AS_APPLICATION_HEADER``, so
    AYON applies its limits on applications. Older servers ignore it and
    give ``x-as-user`` the user's full access, so REST tools apply the
    limits too.

    Returns:
        The client id, or ``None`` when the user calls in person.

    """
    return _DELEGATED_CV.get()


def get_global_ayon_api_key() -> str:
    """Get the AYON API key for the current execution context.

    Returns:
        The AYON API key string for this request context.

    Raises:
        RuntimeError: If the API key has not been initialized in context.

    """
    api_key = _API_KEY_CV.get()
    if not api_key:
        msg = (
            "AYON API key has not been initialized yet. "
            "Call set_global_ayon_api_key() before using request-scoped auth."
        )
        raise RuntimeError(msg)
    return api_key


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
