"""Fetch orchestration: Tavily first, Firecrawl as automatic fallback.

This module is transport-agnostic (no MCP imports) so it can be unit-tested
and reused outside the server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import FetchError, ProviderError
from .providers import FirecrawlProvider, TavilyProvider
from .providers.base import FetchResult, SearchResultItem


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
    """Holds the configured providers and runs fetch/search-with-fallback flows.

    Fetch chain: Tavily → Firecrawl. Search chain: Tavily → Firecrawl.
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
        return self.tavily is not None or self.firecrawl is not None

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
                "No fetch provider configured. Set at least one of: " + ", ".join(missing),
                failures=[{"provider": None, "reason": "not configured"}],
            )

        failures: list[dict[str, Any]] = []

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
                return _to_outcome(
                    result,
                    used_fallback=False,
                    failures=failures,
                    requested_format=format,
                )
            except ProviderError as exc:
                failures.append(_failure_record(exc))
                # Only fall back if the error is worth retrying.
                if not exc.retriable:
                    raise FetchError(url, str(exc), failures=failures) from exc

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
                return _to_outcome(
                    result,
                    used_fallback=True,
                    failures=failures,
                    requested_format=format,
                )
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
        if not self.has_search_provider:
            missing = []
            if self.tavily is None:
                missing.append("TAVILY_API_KEY")
            if self.firecrawl is None:
                missing.append("FIRECRAWL_API_KEY")
            raise FetchError(
                query,
                "No search provider configured. Set at least one of: " + ", ".join(missing),
                failures=[{"provider": None, "reason": "not configured"}],
            )

        failures: list[dict[str, Any]] = []

        # --- Primary: Tavily -------------------------------------------------
        if self.tavily is not None:
            try:
                result = await self.tavily.search(
                    client,
                    query,
                    max_results=max_results,
                    search_depth=search_depth,
                    topic=topic,
                    include_answer=include_answer,
                )
                return {
                    "query": result.query,
                    "answer": result.answer,
                    "results": [_search_item_dict(item) for item in result.results],
                    "provider": "tavily",
                    "used_fallback": False,
                    "failed_providers": failures,
                }
            except ProviderError as exc:
                failures.append(_failure_record(exc))
                # Only fall back if the error is worth retrying.
                if not exc.retriable:
                    raise FetchError(query, str(exc), failures=failures) from exc

        # --- Fallback: Firecrawl --------------------------------------------
        if self.firecrawl is not None:
            try:
                result = await self.firecrawl.search(
                    client,
                    query,
                    max_results=max_results,
                )
                return {
                    "query": result.query,
                    "answer": None,
                    "results": [_search_item_dict(item) for item in result.results],
                    "provider": "firecrawl",
                    "used_fallback": True,
                    "failed_providers": failures,
                }
            except ProviderError as exc:
                failures.append(_failure_record(exc))
                detail = "; ".join(f"[{f['provider']}] {f['reason']}" for f in failures)
                raise FetchError(
                    query, f"All search providers failed: {detail}", failures=failures
                ) from exc

        # Reached only if Tavily failed non-retriable but Firecrawl is None.
        detail = "; ".join(f"[{f['provider']}] {f['reason']}" for f in failures)
        raise FetchError(
            query,
            f"Primary search provider failed and no fallback is configured: {detail}",
            failures=failures,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_outcome(
    result: FetchResult,
    *,
    used_fallback: bool,
    failures: list[dict[str, Any]],
    requested_format: str = "markdown",
) -> FetchOutcome:
    format = result.format
    content = result.content
    # Firecrawl can only produce markdown; honor a "text" request with a
    # best-effort strip so the reported format never lies about the payload.
    if requested_format == "text" and format == "markdown":
        content = _markdown_to_text(content)
        format = "text"
    return FetchOutcome(
        url=result.url,
        provider=result.provider,
        content=content,
        format=format,
        used_fallback=used_fallback,
        failed_providers=failures,
        metadata=result.metadata,
    )


# Precompiled markdown-stripping patterns (best-effort, ordered).
_MD_FENCE = re.compile(r"```.*?\n(.*?)(```|\Z)", re.DOTALL)
_MD_IMAGES = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINKS = re.compile(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)")
_MD_HEADINGS = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BOLD = re.compile(r"(\*\*\*|\*\*|__|~~)")
_MD_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])")
_MD_BLOCKQUOTE = re.compile(r"^>\s?", re.MULTILINE)
_MD_RULE = re.compile(r"^ {0,3}([-_*]\s?){3,}$", re.MULTILINE)
_MD_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.MULTILINE)
_MD_TABLE_PIPES = re.compile(r"\s*\|\s*")
_MD_INLINE_CODE = re.compile(r"`([^`]*)`")
_HTML_TAGS = re.compile(r"<[^>]+>")
_MULTI_BLANK = re.compile(r"\n{3,}")


def _markdown_to_text(markdown: str) -> str:
    """Strip markdown markup best-effort, keeping the readable content."""
    text = markdown

    # Code fences: keep the code, drop the fence markers.
    text = _MD_FENCE.sub(lambda m: m.group(1).rstrip(), text)
    # Images: keep alt text; Links: keep label (drop URL); anchors dropped.
    text = _MD_IMAGES.sub(r"\1", text)
    text = _MD_LINKS.sub(r"\1", text)
    text = _MD_TABLE_SEP.sub("", text)
    text = _MD_TABLE_PIPES.sub(" | ", text)
    text = _MD_HEADINGS.sub("", text)
    text = _MD_BLOCKQUOTE.sub("", text)
    text = _MD_RULE.sub("", text)
    text = _MD_BOLD.sub("", text)
    text = _MD_ITALIC.sub(r"\1", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    text = _HTML_TAGS.sub("", text)
    # Collapse runs of blank lines left behind by removed blocks.
    return _MULTI_BLANK.sub("\n\n", text).strip()


def _failure_record(exc: ProviderError) -> dict[str, Any]:
    return {
        "provider": exc.provider,
        "reason": str(exc).split("] ", 1)[-1],  # drop "[provider] " prefix
        "retriable": exc.retriable,
        "status_code": exc.status_code,
    }


def _search_item_dict(item: SearchResultItem) -> dict[str, Any]:
    return {
        "title": item.title,
        "url": item.url,
        "content": item.content,
        "score": item.score,
    }
