"""HTTP/API exception classes for consistent error handling.

This module provides a hierarchy of HTTP-related exceptions for use across
broker adapters, signal clients, and other API interactions.

Consolidated from patterns in:
- libs/python/lib_infrastructure/lib_infrastructure/brokers/adapters/*.py
"""

from __future__ import annotations

from typing import Any

from lib_common.exceptions import VMStrategyError

# HTTP status code ranges
HTTP_CLIENT_ERROR_MIN = 400
HTTP_CLIENT_ERROR_MAX = 499
HTTP_SERVER_ERROR_MIN = 500
HTTP_SERVER_ERROR_MAX = 599

# Common retryable status codes
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class APIError(VMStrategyError):
    """Base exception for API/HTTP errors.

    Attributes:
        status_code: HTTP status code (if available).
        response_body: Raw response body (if available).
        request_url: URL that was requested.
        is_retryable: Whether the error is considered retryable.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: Any | None = None,
        request_url: str | None = None,
        is_retryable: bool | None = None,
    ) -> None:
        """Initialize APIError.

        Args:
            message: Human-readable error message.
            status_code: HTTP status code.
            response_body: Response body from the API.
            request_url: The URL that was requested.
            is_retryable: Override for retryable determination.
        """
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.request_url = request_url

        # Determine retryability
        if is_retryable is not None:
            self.is_retryable = is_retryable
        elif status_code is not None:
            self.is_retryable = status_code in RETRYABLE_STATUS_CODES
        else:
            self.is_retryable = False

    def to_dict(self) -> dict[str, Any]:
        """Convert error to dictionary for logging/serialization."""
        return {
            "error_type": self.__class__.__name__,
            "message": str(self),
            "status_code": self.status_code,
            "request_url": self.request_url,
            "is_retryable": self.is_retryable,
        }


class ClientError(APIError):
    """HTTP 4xx client errors (bad request, unauthorized, etc.)."""


class BadRequestError(ClientError):
    """HTTP 400 Bad Request."""

    def __init__(
        self,
        message: str = "Bad request",
        **kwargs: Any,
    ) -> None:
        """Initialize BadRequestError."""
        super().__init__(message, status_code=400, **kwargs)


class UnauthorizedError(ClientError):
    """HTTP 401 Unauthorized."""

    def __init__(
        self,
        message: str = "Unauthorized",
        **kwargs: Any,
    ) -> None:
        """Initialize UnauthorizedError."""
        super().__init__(message, status_code=401, **kwargs)


class ForbiddenError(ClientError):
    """HTTP 403 Forbidden."""

    def __init__(
        self,
        message: str = "Forbidden",
        **kwargs: Any,
    ) -> None:
        """Initialize ForbiddenError."""
        super().__init__(message, status_code=403, **kwargs)


class NotFoundError(ClientError):
    """HTTP 404 Not Found."""

    def __init__(
        self,
        message: str = "Not found",
        **kwargs: Any,
    ) -> None:
        """Initialize NotFoundError."""
        super().__init__(message, status_code=404, **kwargs)


class RateLimitError(ClientError):
    """HTTP 429 Too Many Requests (rate limited).

    This error is considered retryable by default.
    """

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        *,
        retry_after: float | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize RateLimitError.

        Args:
            message: Error message.
            retry_after: Seconds to wait before retrying (from Retry-After header).
            **kwargs: Additional arguments for APIError.
        """
        super().__init__(message, status_code=429, is_retryable=True, **kwargs)
        self.retry_after = retry_after


class ServerError(APIError):
    """HTTP 5xx server errors (internal error, bad gateway, etc.).

    Server errors are generally considered retryable.
    """


class InternalServerError(ServerError):
    """HTTP 500 Internal Server Error."""

    def __init__(
        self,
        message: str = "Internal server error",
        **kwargs: Any,
    ) -> None:
        """Initialize InternalServerError."""
        super().__init__(message, status_code=500, is_retryable=True, **kwargs)


class BadGatewayError(ServerError):
    """HTTP 502 Bad Gateway."""

    def __init__(
        self,
        message: str = "Bad gateway",
        **kwargs: Any,
    ) -> None:
        """Initialize BadGatewayError."""
        super().__init__(message, status_code=502, is_retryable=True, **kwargs)


class ServiceUnavailableError(ServerError):
    """HTTP 503 Service Unavailable."""

    def __init__(
        self,
        message: str = "Service unavailable",
        **kwargs: Any,
    ) -> None:
        """Initialize ServiceUnavailableError."""
        super().__init__(message, status_code=503, is_retryable=True, **kwargs)


class GatewayTimeoutError(ServerError):
    """HTTP 504 Gateway Timeout."""

    def __init__(
        self,
        message: str = "Gateway timeout",
        **kwargs: Any,
    ) -> None:
        """Initialize GatewayTimeoutError."""
        super().__init__(message, status_code=504, is_retryable=True, **kwargs)


class ConnectionFailedError(APIError):
    """Connection failure (network error, DNS resolution, etc.).

    Connection failures are generally considered retryable.
    """

    def __init__(
        self,
        message: str = "Connection failed",
        **kwargs: Any,
    ) -> None:
        """Initialize ConnectionFailedError."""
        super().__init__(message, is_retryable=True, **kwargs)


class TimeoutError(APIError):
    """Request timeout.

    Timeouts are generally considered retryable.
    """

    def __init__(
        self,
        message: str = "Request timeout",
        **kwargs: Any,
    ) -> None:
        """Initialize TimeoutError."""
        super().__init__(message, is_retryable=True, **kwargs)


def raise_for_status(
    status_code: int,
    message: str | None = None,
    **kwargs: Any,
) -> None:
    """Raise appropriate exception for HTTP status code.

    Args:
        status_code: HTTP status code.
        message: Optional custom error message.
        **kwargs: Additional arguments passed to exception constructor.

    Raises:
        BadRequestError: For 400 status.
        UnauthorizedError: For 401 status.
        ForbiddenError: For 403 status.
        NotFoundError: For 404 status.
        RateLimitError: For 429 status.
        InternalServerError: For 500 status.
        BadGatewayError: For 502 status.
        ServiceUnavailableError: For 503 status.
        GatewayTimeoutError: For 504 status.
        ClientError: For other 4xx status codes.
        ServerError: For other 5xx status codes.
        APIError: For other non-2xx status codes.
    """
    # Success codes don't raise
    if 200 <= status_code < 300:  # noqa: PLR2004
        return

    # Map specific status codes to exceptions
    status_map = {
        400: BadRequestError,
        401: UnauthorizedError,
        403: ForbiddenError,
        404: NotFoundError,
        429: RateLimitError,
        500: InternalServerError,
        502: BadGatewayError,
        503: ServiceUnavailableError,
        504: GatewayTimeoutError,
    }

    exc_class = status_map.get(status_code)
    if exc_class:
        raise exc_class(message or exc_class.__doc__ or "HTTP error", **kwargs)

    # Fall back to generic classes based on range
    default_msg = message or f"HTTP error {status_code}"
    if HTTP_CLIENT_ERROR_MIN <= status_code <= HTTP_CLIENT_ERROR_MAX:
        raise ClientError(default_msg, status_code=status_code, **kwargs)
    if HTTP_SERVER_ERROR_MIN <= status_code <= HTTP_SERVER_ERROR_MAX:
        raise ServerError(default_msg, status_code=status_code, is_retryable=True, **kwargs)

    raise APIError(default_msg, status_code=status_code, **kwargs)


def is_retryable_status(status_code: int) -> bool:
    """Check if HTTP status code is retryable.

    Args:
        status_code: HTTP status code.

    Returns:
        True if the status code is considered retryable.
    """
    return status_code in RETRYABLE_STATUS_CODES
