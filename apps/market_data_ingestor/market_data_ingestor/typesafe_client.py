"""HTTP client for the pinned TypeSafe System One endpoint.

The client sends exactly the bytes it serializes and returns the exact request and
response bytes next to their parsed JSON, so callers can persist the real exchange.
The API key is held privately and never appears in errors, reprs, or logs.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import httpx

from lib_application.services.adverse_news_observations import TypeSafeExchange
from lib_common.http_exceptions import is_retryable_status
from lib_common.retries import retry_sync
from lib_strategy.adverse_news_questions import TYPESAFE_ENDPOINT

_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_RETRIES = 2
_DEFAULT_BASE_DELAY_SECONDS = 1.0
_DEFAULT_MAX_DELAY_SECONDS = 10.0


class TypeSafeRequestError(RuntimeError):
    """A TypeSafe request failed or returned an unusable response."""


class _RetryableTypeSafeError(TypeSafeRequestError):
    """Internal marker for transport failures and retryable HTTP statuses."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TypeSafeClient:
    """Send one pinned request to TypeSafe and return the exact exchange."""

    def __init__(
        self,
        *,
        api_key: str,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] = _utc_now,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        retries: int = _DEFAULT_RETRIES,
        retry_base_delay_seconds: float = _DEFAULT_BASE_DELAY_SECONDS,
        retry_max_delay_seconds: float = _DEFAULT_MAX_DELAY_SECONDS,
    ) -> None:
        if not api_key.strip():
            msg = "TypeSafe API key must not be blank"
            raise ValueError(msg)
        self._api_key = api_key
        self._owns_http = http_client is None
        self._http = http_client if http_client is not None else httpx.Client()
        self._clock = clock
        self._timeout = timeout_seconds
        self._retries = retries
        self._base_delay = retry_base_delay_seconds
        self._max_delay = retry_max_delay_seconds

    def __repr__(self) -> str:
        return f"{type(self).__name__}(endpoint={TYPESAFE_ENDPOINT!r})"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP client this instance created; a supplied one stays open."""
        if self._owns_http:
            self._http.close()

    def ask(self, request_json: Mapping[str, object]) -> TypeSafeExchange:
        """POST the request and return the exact exchange; raise on any failure."""
        try:
            request_bytes = json.dumps(
                request_json, separators=(",", ":"), ensure_ascii=False
            ).encode()
        except (TypeError, ValueError) as exc:
            msg = "TypeSafe request is not JSON serializable"
            raise TypeSafeRequestError(msg) from exc

        response_bytes = retry_sync(
            lambda: self._post(request_bytes),
            retries=self._retries,
            base_delay=self._base_delay,
            max_delay=self._max_delay,
            jitter=0.0,
            retry_on=(_RetryableTypeSafeError,),
            operation_name="typesafe_request",
        )
        try:
            response_json = json.loads(response_bytes)
        except ValueError as exc:
            msg = "TypeSafe response is not valid JSON"
            raise TypeSafeRequestError(msg) from exc
        if not isinstance(response_json, dict):
            msg = "TypeSafe response is not a JSON object"
            raise TypeSafeRequestError(msg)
        return TypeSafeExchange(
            request_json=json.loads(request_bytes),
            response_json=response_json,
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            received_at=self._clock(),
        )

    def _post(self, request_bytes: bytes) -> bytes:
        try:
            response = self._http.post(
                TYPESAFE_ENDPOINT,
                content=request_bytes,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            msg = "TypeSafe request timed out"
            raise _RetryableTypeSafeError(msg) from None
        except httpx.TransportError as exc:
            msg = f"TypeSafe transport failure: {type(exc).__name__}"
            raise _RetryableTypeSafeError(msg) from None
        status = response.status_code
        if not httpx.codes.is_success(status):
            msg = f"TypeSafe returned HTTP {status}"
            if is_retryable_status(status):
                raise _RetryableTypeSafeError(msg)
            raise TypeSafeRequestError(msg)
        return bytes(response.content)


__all__ = ["TypeSafeClient", "TypeSafeRequestError"]
