"""Firecrawl provider: Scrape (fetch), used as the fallback.

Docs: https://docs.firecrawl.dev/api-reference/endpoint/scrape
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import ProviderError
from .base import FetchResult, classify_http_error

FIRECRAWL_BASE = "https://api.firecrawl.dev"
SCRAPE_MAX_TIMEOUT = 300  # Firecrawl accepts 1-300s


class FirecrawlProvider:
    """Wraps the Firecrawl Scrape endpoint."""

    name = "firecrawl"

    def __init__(self, api_key: str):
        self._api_key = api_key

    @staticmethod
    def from_env(env: dict[str, str]) -> "FirecrawlProvider | None":
        key = env.get("FIRECRAWL_API_KEY", "").strip()
        return FirecrawlProvider(key) if key else None

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
        # We always request markdown; if the caller wants text, we still request
        # markdown and let the orchestrator convert, since Firecrawl's "text"
        # format is rarely what callers actually want.
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
            raise ProviderError(
                self.name, "Firecrawl returned no content", retriable=False
            )

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

    # ----------------------------------------------------------------- helper

    async def _post(
        self, client: httpx.AsyncClient, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            resp = await client.post(
                f"{FIRECRAWL_BASE}{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name, f"network error: {exc!s}", retriable=True
            ) from exc

        if resp.status_code >= 400:
            msg, retriable = classify_http_error(self.name, resp.status_code)
            snippet = _safe_text(resp)
            if snippet:
                msg = f"{msg} — {snippet}"
            raise ProviderError(
                self.name, msg, status_code=resp.status_code, retriable=retriable
            )

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
