from typing import Any


class BusinessLeadFinderError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


class ProviderError(BusinessLeadFinderError):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        provider: str = "unknown",
        details: dict[str, Any] | None = None,
    ):
        self.provider = provider
        super().__init__(message, status_code, details)


class ProviderNotFoundError(ProviderError):
    def __init__(self, provider_type: str):
        super().__init__(
            message=f"Provider '{provider_type}' not found",
            status_code=501,
            provider=provider_type,
        )


class ProviderConfigurationError(ProviderError):
    def __init__(self, provider: str, message: str):
        super().__init__(
            message=f"Provider '{provider}' configuration error: {message}",
            status_code=500,
            provider=provider,
        )


class ValidationError(BusinessLeadFinderError):
    def __init__(self, message: str, field: str | None = None):
        details = {"field": field} if field else {}
        super().__init__(message, status_code=400, details=details)


class RateLimitError(ProviderError):
    def __init__(self, provider: str, retry_after: int | None = None):
        details = {"retry_after": retry_after} if retry_after else {}
        super().__init__(
            message=f"Rate limit exceeded for provider '{provider}'",
            status_code=429,
            provider=provider,
            details=details,
        )


class ProviderTimeoutError(ProviderError):
    def __init__(self, provider: str, timeout: int):
        super().__init__(
            message=f"Request to '{provider}' timed out after {timeout}s",
            status_code=504,
            provider=provider,
        )


class NoResultsError(ProviderError):
    def __init__(self, provider: str, query: str):
        super().__init__(
            message=f"No results found for '{query}' from provider '{provider}'",
            status_code=404,
            provider=provider,
        )
