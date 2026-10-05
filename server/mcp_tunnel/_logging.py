"""Logger of the tunnel: the AYON server's, or stdlib logging outside it."""

from __future__ import annotations

import logging
from typing import Any


def _get_logger() -> Any:  # ruff: ignore[any-type]
    """Return loguru's logger in the AYON server, a stdlib one elsewhere.

    The tunnel only uses methods both have, with preformatted messages.

    Returns:
        The logger.

    """
    try:
        from ayon_server.logging import logger
    except ImportError:  # outside the AYON server, e.g. in ayon-mcp tests
        return logging.getLogger("mcp_tunnel")
    return logger


logger = _get_logger()

__all__ = ["logger"]
