"""Firecrawl provider: Scrape (fetch fallback) + Search (search fallback).

Docs: https://docs.firecrawl.dev/api-reference/endpoint/scrape
      https://docs.firecrawl.dev/api-reference/endpoint/search
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import ProviderError
from .base import FetchResult, SearchResult, SearchResultItem, classify_http_error

FIRECRAWL_BASE = "https://api.firecrawl.dev"
SCRAPE_MAX_TIMEOUT = 300  # Firecrawl accepts 1-300s
SEARCH_MAX_RESULTS = 20


class FirecrawlProvider:
    """Wraps the Firecrawl Scrape and Search endpoints."""

    name = "firecrawl"

    def __init__(self, api_key: str, base_url: str = FIRECRAWL_BASE):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    @staticmethod
    def from_env(env: dict[str, str]) -> FirecrawlProvider | None:
        key = env.get("FIRECRAWL_API_KEY", "").strip()
        if not key:
            return None
        base = env.get("FIRECRAWL_BASE_URL", "").strip().rstrip("/")
        return FirecrawlProvider(key, base_url=base or FIRECRAWL_BASE)

    async def fetch(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        format: str = "markdown",
        only_main_content: bool = True,
        timeout: int = 30,
        **_unused: Any,
    ) -> FetchResult:
        # Firecrawl's scrape API has no plain-"text" format, so we always
        # request markdown; when the caller asked for text the orchestrator
        # (fetcher) strips the markup best-effort before returning.
        payload: dict[str, Any] = {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": bool(only_main_content),
            "timeout": min(max(timeout, 1), SCRAPE_MAX_TIMEOUT) * 1000,  # ms
        }

        data = await self._post(client, "/v2/scrape", payload)

        page = data.get("data") or {}
        content = (page.get("markdown") or "").strip()
        if not content:
            # Fall back to raw html-derived text if markdown is missing.
            content = (page.get("html") or "").strip()

        if not content:
            raise ProviderError(self.name, "Firecrawl returned no content", retriable=False)

        meta = page.get("metadata") or {}
        return FetchResult(
            url=meta.get("sourceURL", url),
            content=content,
            # Report the actual stored format; text conversion happens upstream.
            format="markdown",
            provider=self.name,
            metadata={
                "title": meta.get("title"),
                "description": meta.get("description"),
            },
        )

    # ----------------------------------------------------------------- search

    async def search(
        self,
        client: httpx.AsyncClient,
        query: str,
        *,
        max_results: int = 5,
        **_unused: Any,
    ) -> SearchResult:
        """Firecrawl web search — fallback provider (no answer synthesis)."""
        payload: dict[str, Any] = {
            "query": query,
            "limit": max(1, min(int(max_results), SEARCH_MAX_RESULTS)),
        }

        data = await self._post(client, "/v2/search", payload)

        # v2 wraps hits in data.web; older/self-hosted builds may use
        # data.results — accept both shapes defensively.
        page = data.get("data") or {}
        hits = page.get("web") or page.get("results") or []

        items: list[SearchResultItem] = []
        for hit in hits:
            items.append(
                SearchResultItem(
                    title=hit.get("title", ""),
                    url=hit.get("url", ""),
                    content=hit.get("description") or hit.get("markdown") or "",
                    score=hit.get("score"),
                )
            )

        return SearchResult(query=query, answer=None, results=items)

    # ----------------------------------------------------------------- helper

    async def _post(
        self, client: httpx.AsyncClient, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            resp = await client.post(
                f"{self._base_url}{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"network error: {exc!s}", retriable=True) from exc

        if resp.status_code >= 400:
            msg, retriable = classify_http_error(self.name, resp.status_code)
            snippet = _safe_text(resp)
            if snippet:
                msg = f"{msg} — {snippet}"
            raise ProviderError(self.name, msg, status_code=resp.status_code, retriable=retriable)

        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(
                self.name, f"invalid JSON response: {exc!s}", retriable=True
            ) from exc


def _safe_text(resp: httpx.Response) -> str:
    try:
        text = resp.text.strip()
    except Exception:
        return ""
    return (text[:200] + "…") if len(text) > 200 else text
