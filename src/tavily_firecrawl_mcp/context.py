"""Application context held across the FastMCP server lifetime.

Stored in the lifespan context and injected into each tool via the MCP
`Context` object, so a single httpx client + provider set is shared by all
requests instead of being recreated per call.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .fetcher import Fetcher


@dataclass
class AppContext:
    """Shared resources available to every tool handler."""

    client: httpx.AsyncClient
    fetcher: Fetcher
