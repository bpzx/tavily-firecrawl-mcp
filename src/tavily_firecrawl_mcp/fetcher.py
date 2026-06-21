"""Fetch orchestration: Tavily first, Firecrawl as automatic fallback.

This module is transport-agnostic (no MCP imports) so it can be unit-tested
and reused outside the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .errors import FetchError, ProviderError
from .providers import FirecrawlProvider, TavilyProvider
from .providers.base import FetchResult


@dataclass
class FetchOutcome:
    """What the orchestrator hands back to the MCP tool layer."""

    url: str
    provider: str
    content: str
    format: str
    used_fallback: bool
    failed_providers: list[dict[str, Any]]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "provider": self.provider,
            "content": self.content,
            "format": self.format,
            "used_fallback": self.used_fallback,
            "failed_providers": self.failed_providers,
            "metadata": self.metadata,
        }


class Fetcher:
    """Holds the configured providers and runs the fetch-with-fallback flow.

    Providers that are None (no API key configured) are simply absent from the
    chain — calling code never constructs a request for them.
    """

    def __init__(
        self,
        tavily: TavilyProvider | None,
        firecrawl: FirecrawlProvider | None,
    ):
        self.tavily = tavily
        self.firecrawl = firecrawl

    @property
    def has_fetch_provider(self) -> bool:
        return self.tavily is not None or self.firecrawl is not None

    @property
    def has_search_provider(self) -> bool:
        return self.tavily is not None

    async def fetch(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        query: str | None = None,
        format: str = "markdown",
        extract_depth: str = "basic",
        only_main_content: bool = True,
        timeout: int = 30,
    ) -> FetchOutcome:
        if not self.has_fetch_provider:
            missing = []
            if self.tavily is None:
                missing.append("TAVILY_API_KEY")
            if self.firecrawl is None:
                missing.append("FIRECRAWL_API_KEY")
            raise FetchError(
                url,
                "No fetch provider configured. Set at least one of: "
                + ", ".join(missing),
                failures=[{"provider": None, "reason": "not configured"}],
            )

        failures: list[dict[str, Any]] = []
        used_fallback = False

        # --- Primary: Tavily -------------------------------------------------
        if self.tavily is not None:
            try:
                result = await self.tavily.fetch(
                    client,
                    url,
                    query=query,
                    format=format,
                    extract_depth=extract_depth,
                    timeout=timeout,
                )
                return _to_outcome(result, used_fallback=False, failures=failures)
            except ProviderError as exc:
                failures.append(_failure_record(exc))
                # Only fall back if the error is worth retrying.
                if not exc.retriable:
                    raise FetchError(url, str(exc), failures=failures) from exc
                used_fallback = True

        # --- Fallback: Firecrawl --------------------------------------------
        if self.firecrawl is not None:
            try:
                result = await self.firecrawl.fetch(
                    client,
                    url,
                    format=format,
                    only_main_content=only_main_content,
                    timeout=timeout,
                )
                return _to_outcome(result, used_fallback=True, failures=failures)
            except ProviderError as exc:
                failures.append(_failure_record(exc))
                # Last provider: surface combined error.
                detail = "; ".join(f"[{f['provider']}] {f['reason']}" for f in failures)
                raise FetchError(
                    url, f"All fetch providers failed: {detail}", failures=failures
                ) from exc

        # Reached only if Tavily failed non-retriable but Firecrawl is None.
        detail = "; ".join(f"[{f['provider']}] {f['reason']}" for f in failures)
        raise FetchError(
            url,
            f"Primary provider failed and no fallback is configured: {detail}",
            failures=failures,
        )

    async def search(
        self,
        client: httpx.AsyncClient,
        query: str,
        *,
        max_results: int = 5,
        search_depth: str = "basic",
        topic: str = "general",
        include_answer: bool = False,
    ) -> dict[str, Any]:
        if self.tavily is None:
            raise FetchError(
                query,
                "Search requires TAVILY_API_KEY (no fallback search provider).",
                failures=[{"provider": "tavily", "reason": "not configured"}],
            )
        try:
            result = await self.tavily.search(
                client,
                query,
                max_results=max_results,
                search_depth=search_depth,
                topic=topic,
                include_answer=include_answer,
            )
        except ProviderError as exc:
            raise FetchError(query, str(exc), failures=[_failure_record(exc)]) from exc

        return {
            "query": result.query,
            "answer": result.answer,
            "results": [
                {
                    "title": item.title,
                    "url": item.url,
                    "content": item.content,
                    "score": item.score,
                }
                for item in result.results
            ],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_outcome(
    result: FetchResult,
    *,
    used_fallback: bool,
    failures: list[dict[str, Any]],
) -> FetchOutcome:
    return FetchOutcome(
        url=result.url,
        provider=result.provider,
        content=result.content,
        format=result.format,
        used_fallback=used_fallback,
        failed_providers=failures,
        metadata=result.metadata,
    )


def _failure_record(exc: ProviderError) -> dict[str, Any]:
    return {
        "provider": exc.provider,
        "reason": str(exc).split("] ", 1)[-1],  # drop "[provider] " prefix
        "retriable": exc.retriable,
        "status_code": exc.status_code,
    }
