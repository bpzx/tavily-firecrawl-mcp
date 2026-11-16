"""Smoke tests — verify the package imports and core pieces wire up.

These don't hit the network; they guard against packaging / import regressions
so CI always has something meaningful to run.
"""

from __future__ import annotations

import tavily_firecrawl_mcp
from tavily_firecrawl_mcp import __version__
from tavily_firecrawl_mcp.errors import FetchError, ProviderError
from tavily_firecrawl_mcp.fetcher import Fetcher
from tavily_firecrawl_mcp.providers import FirecrawlProvider, TavilyProvider


def test_version_is_a_string():
    assert isinstance(__version__, str)
    assert __version__.strip() != ""


def test_package_namespace():
    assert hasattr(tavily_firecrawl_mcp, "__version__")


def test_fetcher_without_providers():
    # No providers configured -> fetcher should be constructible with None.
    f = Fetcher(tavily=None, firecrawl=None)
    assert f.tavily is None
    assert f.firecrawl is None


def test_providers_skip_when_no_env(monkeypatch):
    # Strip every key the providers look for; both should come back as None.
    for key in (
        "TAVILY_API_KEY",
        "FIRECRAWL_API_KEY",
        "TAVILY_BASE_URL",
        "FIRECRAWL_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    assert TavilyProvider.from_env({}) is None
    assert FirecrawlProvider.from_env({}) is None


def test_error_types_subclass_exception():
    assert issubclass(FetchError, Exception)
    assert issubclass(ProviderError, Exception)


async def test_server_module_imports_and_registers_tools():
    # Regression guard for dependency breakages (e.g. mcp 2.x renaming
    # FastMCP) that used to crash at import time and kill the stdio server.
    from tavily_firecrawl_mcp.server import mcp

    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert {"web_fetch", "web_search"} <= names
