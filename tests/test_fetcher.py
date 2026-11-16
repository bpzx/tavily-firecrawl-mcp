"""Unit tests for the fetch/search fallback chains — no network, MockTransport."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tavily_firecrawl_mcp.errors import FetchError
from tavily_firecrawl_mcp.fetcher import Fetcher, _markdown_to_text
from tavily_firecrawl_mcp.providers import FirecrawlProvider, TavilyProvider
from tavily_firecrawl_mcp.providers.base import make_client

URL = "https://example.com/page"

TAVILY_EXTRACT_OK: dict[str, Any] = {
    "results": [{"url": URL, "raw_content": "tavily content"}],
    "failed_results": [],
}
FIRECRAWL_SCRAPE_OK: dict[str, Any] = {
    "success": True,
    "data": {
        "markdown": "# Title\n\nfirecrawl **content**",
        "metadata": {"title": "Example", "sourceURL": URL},
    },
}
TAVILY_SEARCH_OK: dict[str, Any] = {
    "results": [{"title": "t", "url": URL, "content": "snippet", "score": 0.9}],
}
FIRECRAWL_SEARCH_OK: dict[str, Any] = {
    "success": True,
    "data": {"web": [{"title": "f", "url": URL, "description": "desc"}]},
}


def _json(status: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=body)


def _client(
    tavily: httpx.Response,
    firecrawl: httpx.Response,
    calls: list[str],
) -> httpx.AsyncClient:
    """MockTransport client that answers by provider host and records calls."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        calls.append(host)
        return tavily if "tavily" in host else firecrawl

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fetcher() -> Fetcher:
    return Fetcher(tavily=TavilyProvider("k"), firecrawl=FirecrawlProvider("k"))


# ---------------------------------------------------------------------- fetch


async def test_fetch_primary_success():
    calls: list[str] = []
    async with _client(
        _json(200, TAVILY_EXTRACT_OK), _json(200, FIRECRAWL_SCRAPE_OK), calls
    ) as client:
        outcome = await _fetcher().fetch(client, URL)
    assert outcome.provider == "tavily"
    assert outcome.used_fallback is False
    assert outcome.content == "tavily content"
    assert calls == [c for c in calls if "tavily" in c]  # firecrawl never called


async def test_fetch_falls_back_on_retriable_error():
    calls: list[str] = []
    async with _client(
        _json(500, {"error": "boom"}), _json(200, FIRECRAWL_SCRAPE_OK), calls
    ) as client:
        outcome = await _fetcher().fetch(client, URL)
    assert outcome.provider == "firecrawl"
    assert outcome.used_fallback is True
    assert outcome.failed_providers[0]["status_code"] == 500
    assert any("tavily" in c for c in calls) and any("firecrawl" in c for c in calls)


async def test_fetch_no_fallback_on_404():
    calls: list[str] = []
    with pytest.raises(FetchError) as exc_info:
        async with _client(
            _json(404, {"error": "nf"}), _json(200, FIRECRAWL_SCRAPE_OK), calls
        ) as client:
            await _fetcher().fetch(client, URL)
    # Non-retriable: fallback must never be attempted.
    assert all("tavily" in c for c in calls)
    assert len(exc_info.value.failures) == 1


async def test_fetch_all_providers_fail():
    calls: list[str] = []
    with pytest.raises(FetchError) as exc_info:
        async with _client(_json(500, {"e": 1}), _json(429, {"e": 2}), calls) as client:
            await _fetcher().fetch(client, URL)
    failures = exc_info.value.failures
    assert [f["provider"] for f in failures] == ["tavily", "firecrawl"]
    assert failures[0]["status_code"] == 500
    assert failures[1]["status_code"] == 429


async def test_fetch_text_via_fallback_converts_markdown():
    async with _client(_json(500, {"e": 1}), _json(200, FIRECRAWL_SCRAPE_OK), []) as client:
        outcome = await _fetcher().fetch(client, URL, format="text")
    assert outcome.format == "text"
    # Heading text is kept as content; inline markup stripped.
    assert outcome.content == "Title\n\nfirecrawl content"


async def test_fetch_no_providers_configured():
    with pytest.raises(FetchError) as exc_info:
        # trust_env=False: the machine's NO_PROXY may hold values httpx cannot
        # parse, and these tests never hit the network anyway.
        async with httpx.AsyncClient(trust_env=False) as client:
            await Fetcher(tavily=None, firecrawl=None).fetch(client, URL)
    assert "TAVILY_API_KEY" in str(exc_info.value)
    assert "FIRECRAWL_API_KEY" in str(exc_info.value)


# --------------------------------------------------------------------- search


async def test_search_primary_success():
    async with _client(_json(200, TAVILY_SEARCH_OK), _json(200, FIRECRAWL_SEARCH_OK), []) as client:
        result = await _fetcher().search(client, "hello")
    assert result["provider"] == "tavily"
    assert result["used_fallback"] is False
    assert result["results"][0]["url"] == URL


async def test_search_falls_back_on_retriable_error():
    async with _client(_json(500, {"e": 1}), _json(200, FIRECRAWL_SEARCH_OK), []) as client:
        result = await _fetcher().search(client, "hello")
    assert result["provider"] == "firecrawl"
    assert result["used_fallback"] is True
    assert result["failed_providers"][0]["provider"] == "tavily"


async def test_search_all_providers_fail():
    with pytest.raises(FetchError) as exc_info:
        async with _client(_json(500, {"e": 1}), _json(500, {"e": 2}), []) as client:
            await _fetcher().search(client, "hello")
    assert len(exc_info.value.failures) == 2


async def test_search_no_providers_configured():
    with pytest.raises(FetchError):
        async with httpx.AsyncClient(trust_env=False) as client:
            await Fetcher(tavily=None, firecrawl=None).search(client, "hello")


# ------------------------------------------------------------- markdown→text


def test_markdown_to_text_strips_markup():
    md = (
        "# Heading\n\n"
        "Some *emphasis* and **bold** text with [a link](https://x.test/a).\n\n"
        "![alt text](https://x.test/i.png)\n\n"
        "```python\nprint('kept')\n```\n\n"
        "> a quote\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    )
    text = _markdown_to_text(md)
    assert "#" not in text
    assert "**" not in text
    assert "https://x.test" not in text
    assert "a link" in text
    assert "alt text" in text
    assert "print('kept')" in text
    assert "Heading" in text


# ------------------------------------------------------------------ providers


def test_base_url_overrides():
    tavily = TavilyProvider.from_env(
        {"TAVILY_API_KEY": "k", "TAVILY_BASE_URL": " http://127.0.0.1:9001/ "}
    )
    assert tavily is not None and tavily._base_url == "http://127.0.0.1:9001"

    firecrawl = FirecrawlProvider.from_env(
        {"FIRECRAWL_API_KEY": "k", "FIRECRAWL_BASE_URL": "http://127.0.0.1:9002"}
    )
    assert firecrawl is not None and firecrawl._base_url == "http://127.0.0.1:9002"


def test_base_url_defaults():
    tavily = TavilyProvider.from_env({"TAVILY_API_KEY": "k"})
    assert tavily is not None and tavily._base_url == "https://api.tavily.com"

    firecrawl = FirecrawlProvider.from_env({"FIRECRAWL_API_KEY": "k"})
    assert firecrawl is not None and firecrawl._base_url == "https://api.firecrawl.dev"


# ---------------------------------------------------------------- make_client


def test_make_client_survives_invalid_no_proxy(monkeypatch):
    # The IPv6 literal "[::1]" is common in NO_PROXY but httpx cannot parse it;
    # make_client must degrade to a direct-connection client instead of raising.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1],*.local")
    client = make_client(timeout=10)  # used to raise httpx.InvalidURL
    assert client is not None
    assert client._trust_env is False  # degraded: env proxies ignored


def test_make_client_explicit_proxy_disables_env(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    client = make_client(timeout=10, proxy="http://127.0.0.1:11223")
    assert client._trust_env is False
