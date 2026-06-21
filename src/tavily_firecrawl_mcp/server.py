"""FastMCP server: exposes `web_fetch` and `web_search` over stdio.

Providers are loaded once at startup from environment variables; a single
httpx client is shared for the server's lifetime.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .context import AppContext
from .errors import FetchError, ProviderError
from .fetcher import Fetcher
from .providers import FirecrawlProvider, TavilyProvider
from .providers.base import make_client

# Default per-request timeout in seconds.
DEFAULT_TIMEOUT = 30


def build_fetcher(env: dict[str, str]) -> Fetcher:
    """Construct a Fetcher from the given environment mapping."""
    tavily = TavilyProvider.from_env(env)
    firecrawl = FirecrawlProvider.from_env(env)
    return Fetcher(tavily=tavily, firecrawl=firecrawl)


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[AppContext]:
    """Create shared resources once; clean them up on shutdown."""
    env = dict(os.environ)
    fetcher = build_fetcher(env)
    # Optional outbound proxy (e.g. "http://127.0.0.1:7890"). When unset,
    # httpx still honors HTTP_PROXY/HTTPS_PROXY via trust_env.
    proxy = env.get("WEB_FETCH_PROXY", "").strip() or None
    client = make_client(timeout=DEFAULT_TIMEOUT, proxy=proxy)
    try:
        yield AppContext(client=client, fetcher=fetcher)
    finally:
        await client.aclose()


mcp = FastMCP(
    name="web-fetch",
    instructions=(
        "Web fetching (Tavily primary, Firecrawl automatic fallback) and "
        "web search (Tavily). Use web_fetch to retrieve a page's content; "
        "use web_search to find pages."
    ),
    lifespan=lifespan,
)


def _ctx(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context  # type: ignore[no-any-return]


def _error_payload(exc: FetchError | ProviderError, url: str) -> dict[str, Any]:
    """Build a structured error response for the LLM."""
    return {
        "ok": False,
        "url": url,
        "error": str(exc),
        "failures": getattr(exc, "failures", []),
    }


@mcp.tool(
    name="web_fetch",
    description=(
        "Fetch and extract the content of a web page as markdown (or plain text). "
        "Tries Tavily first; on failure or empty result, automatically falls back "
        "to Firecrawl. Requires TAVILY_API_KEY and/or FIRECRAWL_API_KEY in the "
        "server's environment."
    ),
)
async def web_fetch(
    url: str,
    ctx: Context,
    *,
    query: str | None = None,
    format: str = "markdown",
    extract_depth: str = "basic",
    only_main_content: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Fetch a single URL.

    Args:
        url: The page URL to fetch.
        query: Optional intent; Tavily reorders the extracted content around it.
        format: "markdown" (default) or "text".
        extract_depth: Tavily depth: "basic" (default) or "advanced".
        only_main_content: Firecrawl only — drop nav/ads/footer (default True).
        timeout: Per-request timeout in seconds (default 30).
    """
    app = _ctx(ctx)
    try:
        outcome = await app.fetcher.fetch(
            app.client,
            url,
            query=query,
            format=format,
            extract_depth=extract_depth,
            only_main_content=only_main_content,
            timeout=timeout,
        )
    except FetchError as exc:
        await ctx.warning(f"web_fetch failed for {url}: {exc}")
        return _error_payload(exc, url)

    if outcome.used_fallback and outcome.failed_providers:
        await ctx.info(
            f"web_fetch used fallback ({outcome.provider}) for {url}; "
            f"primary failed: {outcome.failed_providers}"
        )
    return {"ok": True, **outcome.to_dict()}


@mcp.tool(
    name="web_search",
    description=(
        "Search the web with Tavily and return titles, URLs, and snippets "
        "(plus an optional synthesized answer). No fallback — requires "
        "TAVILY_API_KEY in the server's environment."
    ),
)
async def web_search(
    query: str,
    ctx: Context,
    *,
    max_results: int = 5,
    search_depth: str = "basic",
    topic: str = "general",
    include_answer: bool = False,
) -> dict[str, Any]:
    """Run a web search.

    Args:
        query: The search query.
        max_results: Number of results (1-20, default 5).
        search_depth: "basic" (default, faster) or "advanced".
        topic: "general" (default) or "news".
        include_answer: If True, also return a synthesized answer.
    """
    app = _ctx(ctx)
    try:
        result = await app.fetcher.search(
            app.client,
            query,
            max_results=max_results,
            search_depth=search_depth,
            topic=topic,
            include_answer=include_answer,
        )
    except FetchError as exc:
        await ctx.warning(f"web_search failed for {query!r}: {exc}")
        return {"ok": False, "query": query, "error": str(exc), "results": []}

    return {"ok": True, **result}


def main() -> None:
    """Console-script entry point."""
    mcp.run()


if __name__ == "__main__":
    main()
