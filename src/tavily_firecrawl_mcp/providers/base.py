"""Shared data structures and a small HTTP helper used by all providers."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    """Normalized output of a single-provider fetch.

    Attributes:
        url: The URL that was fetched.
        content: The extracted page content (markdown or plain text).
        format: The content format returned ("markdown" or "text").
        provider: Which provider produced this result ("tavily" / "firecrawl").
        metadata: Optional provider-specific metadata (title, description, ...).
    """

    url: str
    content: str
    format: str
    provider: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResultItem:
    """A single search hit."""

    title: str
    url: str
    content: str
    score: float | None = None


@dataclass
class SearchResult:
    """Normalized output of a search."""

    query: str
    answer: str | None
    results: list[SearchResultItem]


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def make_client(timeout: float, proxy: str | None = None) -> httpx.AsyncClient:
    """Create a shared AsyncClient with sensible defaults.

    follow_redirects is on so short-link / moved URLs resolve transparently.

    Proxy precedence:
      1. Explicit ``proxy`` argument (typically from the WEB_FETCH_PROXY env var).
      2. httpx defaults via trust_env=True, i.e. HTTP_PROXY / HTTPS_PROXY /
         ALL_PROXY environment variables.

    Passing proxy=None keeps trust_env on so standard env vars still work; when
    an explicit proxy is given, env-var fallback is disabled to keep behavior
    predictable.
    """
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout, connect=10.0),
        "follow_redirects": True,
        "headers": {"User-Agent": "tavily-firecrawl-mcp/0.1 (+https://modelcontextprotocol.io)"},
    }
    if proxy:
        kwargs["proxy"] = proxy
        kwargs["trust_env"] = False
    try:
        return httpx.AsyncClient(**kwargs)
    except httpx.InvalidURL as exc:
        # Some HTTP_PROXY/NO_PROXY values that other tools accept (e.g. the
        # IPv6 literal "[::1]" in no_proxy) are not valid httpx URL patterns
        # and raise here during client construction. A broken env proxy config
        # must not take down the whole server: fall back to direct connection.
        logger.warning(
            "Ignoring invalid proxy environment configuration (%s); "
            "falling back to direct connection.",
            exc,
        )
        kwargs["trust_env"] = False
        return httpx.AsyncClient(**kwargs)


def classify_http_error(provider: str, status_code: int) -> tuple[str, bool]:
    """Map an HTTP status to (human message, retriable flag).

    `retriable=True` means a fallback provider is worth trying. Auth/key errors
    (401/403) are retriable because another provider may have a valid key;
    rate limiting (429) and transient 5xx are also retriable.
    """
    if status_code in (401, 403):
        return f"authentication failed (HTTP {status_code}): check the API key", True
    if status_code == 404:
        return f"endpoint not found (HTTP {status_code})", False
    if status_code == 429:
        return f"rate limited (HTTP {status_code})", True
    if 500 <= status_code < 600:
        return f"upstream server error (HTTP {status_code})", True
    return f"unexpected HTTP status {status_code}", True
