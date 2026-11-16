"""Tavily provider: Extract (fetch) + Search.

Docs: https://docs.tavily.com/documentation/api-reference/endpoint/extract
      https://docs.tavily.com/documentation/api-reference/endpoint/search
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import ProviderError
from .base import (
    FetchResult,
    SearchResult,
    SearchResultItem,
    classify_http_error,
)

TAVILY_BASE = "https://api.tavily.com"
EXTRACT_MAX_TIMEOUT = 60  # Tavily Extract accepts 1-60s
SEARCH_MAX_RESULTS = 20


class TavilyProvider:
    """Wraps the Tavily Extract and Search endpoints."""

    name = "tavily"

    def __init__(self, api_key: str, base_url: str = TAVILY_BASE):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    @staticmethod
    def from_env(env: dict[str, str]) -> TavilyProvider | None:
        key = env.get("TAVILY_API_KEY", "").strip()
        if not key:
            return None
        base = env.get("TAVILY_BASE_URL", "").strip().rstrip("/")
        return TavilyProvider(key, base_url=base or TAVILY_BASE)

    # ------------------------------------------------------------------ fetch

    async def fetch(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        query: str | None = None,
        format: str = "markdown",
        extract_depth: str = "basic",
        timeout: int = 30,
    ) -> FetchResult:
        payload: dict[str, Any] = {
            "urls": url,
            # Tavily Extract's own timeout field is capped at 60s.
            "timeout": min(max(timeout, 1), EXTRACT_MAX_TIMEOUT),
        }
        if format in ("markdown", "text"):
            payload["format"] = format
        if extract_depth in ("basic", "advanced"):
            payload["extract_depth"] = extract_depth
        if query:
            payload["query"] = query

        data = await self._post(client, "/extract", payload)

        results = data.get("results") or []
        for item in results:
            content = (item.get("raw_content") or "").strip()
            if content:
                return FetchResult(
                    url=item.get("url", url),
                    content=content,
                    format=format,
                    provider=self.name,
                    metadata={"extract_depth": extract_depth},
                )

        failed = data.get("failed_results") or []
        if failed:
            detail = failed[0]
            raise ProviderError(
                self.name,
                f"Tavily could not extract content: {detail.get('reason', 'unknown reason')}",
                retriable=True,
            )
        raise ProviderError(self.name, "Tavily returned no content", retriable=True)

    # ----------------------------------------------------------------- search

    async def search(
        self,
        client: httpx.AsyncClient,
        query: str,
        *,
        max_results: int = 5,
        search_depth: str = "basic",
        topic: str = "general",
        include_answer: bool = False,
    ) -> SearchResult:
        payload: dict[str, Any] = {
            "query": query,
            "max_results": max(1, min(int(max_results), SEARCH_MAX_RESULTS)),
            "search_depth": search_depth if search_depth in ("basic", "advanced") else "basic",
            "topic": topic if topic in ("general", "news") else "general",
            "include_answer": bool(include_answer),
        }

        data = await self._post(client, "/search", payload)

        items: list[SearchResultItem] = []
        for hit in data.get("results") or []:
            items.append(
                SearchResultItem(
                    title=hit.get("title", ""),
                    url=hit.get("url", ""),
                    content=hit.get("content", ""),
                    score=hit.get("score"),
                )
            )

        answer = None
        if include_answer:
            # Tavily returns either a string or {"text": "..."} for include_answer.
            raw_answer = data.get("answer")
            if isinstance(raw_answer, dict):
                answer = raw_answer.get("text")
            elif isinstance(raw_answer, str):
                answer = raw_answer

        return SearchResult(query=query, answer=answer, results=items)

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
    # Keep error context short to avoid bloating the surfaced message.
    return (text[:200] + "…") if len(text) > 200 else text
