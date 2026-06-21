"""Error types for web-fetch providers and orchestration."""

from __future__ import annotations


class WebFetchError(Exception):
    """Base error for all web-fetch failures."""


class ProviderDisabledError(WebFetchError):
    """Raised when a provider is unavailable because its API key is missing."""

    def __init__(self, provider: str, env_var: str):
        self.provider = provider
        self.env_var = env_var
        super().__init__(
            f"Provider '{provider}' is disabled: environment variable "
            f"{env_var} is not set."
        )


class ProviderError(WebFetchError):
    """A provider call failed (HTTP error, network error, empty content, ...)."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        retriable: bool = False,
    ):
        self.provider = provider
        self.status_code = status_code
        # Whether trying the next provider is worthwhile.
        self.retriable = retriable
        super().__init__(f"[{provider}] {message}")


class FetchError(WebFetchError):
    """All providers failed / unavailable. Surfaced to the MCP tool layer."""

    def __init__(self, url: str, message: str, *, failures: list[dict] | None = None):
        self.url = url
        self.failures = failures or []
        super().__init__(message)
