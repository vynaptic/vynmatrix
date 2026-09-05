"""Supervised official market-session synchronization."""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.exc import SQLAlchemyError

from lib_application.services.market_calendars import (
    MarketCalendarTarget,
    load_market_calendar_targets,
)
from lib_common.logging import get_logger
from lib_infrastructure.market_data.market_calendars import (
    IBKROfficialMarketCalendarProvider,
    NSEOfficialMarketCalendarProvider,
    OfficialMarketCalendar,
    OfficialMarketCalendarError,
    OfficialMarketCalendarProvider,
    SaxoOfficialMarketCalendarProvider,
)

logger = get_logger(__name__)

_PROVIDER_BROKER_CODES = {
    "ibkr": "ibkr",
    "nse": "zerodha",
    "saxo_live": "saxo",
}
_CALENDAR_CODE_COMPONENT = re.compile(r"[^A-Z0-9_.-]+")
_MIN_POLL_INTERVAL_SECONDS = 15
_MAX_POLL_INTERVAL_SECONDS = 1800
_MAX_CALENDAR_CODE_LENGTH = 100


class MarketCalendarSyncError(RuntimeError):
    """A calendar batch could not be published through the control plane."""


@dataclass(frozen=True, slots=True)
class MarketCalendarSyncSummary:
    """One complete configured-target synchronization."""

    target_count: int
    calendar_count: int
    session_count: int
    completed_at: datetime


class BackendMarketCalendarClient:
    """Authenticated client for the backend's atomic calendar PUT boundary."""

    def __init__(
        self,
        *,
        base_url: str,
        admin_api_key: str | None,
        client: httpx.Client | None = None,
    ) -> None:
        normalized_url = str(base_url or "").strip().rstrip("/")
        parsed = urlparse(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            msg = "MARKET_CALENDAR_API_URL must be an absolute HTTP(S) URL"
            raise ValueError(msg)
        normalized_key = str(admin_api_key or "").strip()
        if not normalized_key:
            msg = "MARKET_CALENDAR_ADMIN_API_KEY is required"
            raise ValueError(msg)
        self._headers = {"X-Admin-Key": normalized_key}
        self._client = client or httpx.Client(
            base_url=normalized_url,
            timeout=20.0,
            headers=self._headers,
        )

    def replace(
        self,
        *,
        code: str,
        observation: OfficialMarketCalendar,
        instrument_ids: Sequence[int],
    ) -> None:
        payload = {
            "source_kind": observation.source_kind,
            "provider": observation.provider,
            "source_reference": observation.source_reference,
            "observed_at": observation.observed_at.isoformat(),
            "coverage_start": observation.coverage_start.isoformat(),
            "coverage_end": observation.coverage_end.isoformat(),
            "sessions": [
                {
                    "opens_at": session.opens_at.isoformat(),
                    "closes_at": session.closes_at.isoformat(),
                }
                for session in observation.sessions
            ],
            "instrument_ids": sorted({int(instrument_id) for instrument_id in instrument_ids}),
        }
        try:
            response = self._client.put(
                f"/market-calendars/{code}",
                json=payload,
                headers=self._headers,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            status_code = (
                exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            )
            suffix = f" (HTTP {status_code})" if status_code is not None else ""
            msg = f"Backend rejected market calendar {code!r}{suffix}"
            raise MarketCalendarSyncError(msg) from exc

    def close(self) -> None:
        self._client.close()


class MarketCalendarSyncWorker:
    """Fetch official schedules and publish them without inventing fallbacks."""

    def __init__(
        self,
        *,
        session_factory: Any,
        provider: OfficialMarketCalendarProvider,
        symbols: Sequence[str],
        poll_interval_sec: int,
        api_client: BackendMarketCalendarClient,
    ) -> None:
        normalized_symbols = tuple(
            str(symbol or "").strip() for symbol in symbols if str(symbol or "").strip()
        )
        if not normalized_symbols:
            msg = "MARKET_CALENDAR_SYMBOLS must select at least one canonical instrument"
            raise ValueError(msg)
        if not _MIN_POLL_INTERVAL_SECONDS <= poll_interval_sec <= _MAX_POLL_INTERVAL_SECONDS:
            msg = "MARKET_CALENDAR_POLL_INTERVAL_SEC must be between 15 and 1800"
            raise ValueError(msg)
        try:
            broker_code = _PROVIDER_BROKER_CODES[provider.provider]
        except KeyError as exc:
            supported = ", ".join(sorted(_PROVIDER_BROKER_CODES))
            msg = (
                f"Unsupported official market-calendar provider {provider.provider!r}; "
                f"expected one of: {supported}"
            )
            raise ValueError(msg) from exc

        self._session_factory = session_factory
        self._provider = provider
        self._broker_code = broker_code
        self._symbols = normalized_symbols
        self._poll_interval_sec = poll_interval_sec
        self._api_client = api_client
        self._stop_event = threading.Event()
        self._last_success_at: datetime | None = None
        self._closed = False

    def _load_targets(self) -> tuple[MarketCalendarTarget, ...]:
        with self._session_factory() as session:
            return load_market_calendar_targets(
                session,
                broker_code=self._broker_code,
                symbols=self._symbols,
                require_instrument_type=self._provider.provider.startswith("saxo_"),
            )

    def run_once(self) -> MarketCalendarSyncSummary:
        """Synchronize all selected instruments once.

        Every provider response is fetched and validated before the first write.
        A source-shape failure therefore refreshes none of the configured
        calendars; the prior observations simply become stale and fail closed.
        """
        targets = self._load_targets()
        if self._provider.provider == "nse":
            observations = [
                (
                    self._calendar_code(targets[0], group_targets=targets),
                    self._provider.fetch(
                        product_id=targets[0].product_id,
                        broker_instrument_type=targets[0].broker_instrument_type,
                    ),
                    tuple(target.instrument_id for target in targets),
                )
            ]
        else:
            observations = [
                (
                    self._calendar_code(target),
                    self._provider.fetch(
                        product_id=target.product_id,
                        broker_instrument_type=target.broker_instrument_type,
                    ),
                    (target.instrument_id,),
                )
                for target in targets
            ]

        for code, observation, instrument_ids in observations:
            self._api_client.replace(
                code=code,
                observation=observation,
                instrument_ids=instrument_ids,
            )

        completed_at = datetime.now(tz=UTC)
        self._last_success_at = completed_at
        summary = MarketCalendarSyncSummary(
            target_count=len(targets),
            calendar_count=len(observations),
            session_count=sum(len(observation.sessions) for _, observation, _ in observations),
            completed_at=completed_at,
        )
        logger.info(
            "Official market calendars synchronized",
            provider=self._provider.provider,
            targets=summary.target_count,
            calendars=summary.calendar_count,
            sessions=summary.session_count,
        )
        return summary

    def _calendar_code(
        self,
        target: MarketCalendarTarget,
        *,
        group_targets: Sequence[MarketCalendarTarget] = (),
    ) -> str:
        if self._provider.provider == "nse":
            identity = "-".join(
                f"I{target_id}" for target_id in sorted(row.instrument_id for row in group_targets)
            )
        else:
            identity = f"I{target.instrument_id}"
        component = _CALENDAR_CODE_COMPONENT.sub("-", identity.upper()).strip("-")
        if not component:
            msg = "Cannot derive a stable market-calendar code"
            raise MarketCalendarSyncError(msg)
        provider = self._provider.provider.upper()
        code = f"{provider}:{component}"
        if len(code) > _MAX_CALENDAR_CODE_LENGTH:
            msg = "Configured market-calendar target set cannot form a unique 100-character code"
            raise MarketCalendarSyncError(msg)
        return code

    def run_forever(self) -> None:
        """Run immediately and retain prior observations across source failures."""
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except (
                MarketCalendarSyncError,
                OfficialMarketCalendarError,
                SQLAlchemyError,
                httpx.HTTPError,
                TypeError,
                ValueError,
            ):
                logger.exception(
                    "Official market-calendar sync failed; prior coverage will age out"
                )
            if self._stop_event.wait(self._poll_interval_sec):
                break

    def is_fresh(self) -> bool:
        """Whether every configured target completed within two poll periods."""
        if self._last_success_at is None:
            return False
        age = datetime.now(tz=UTC) - self._last_success_at
        return timedelta(0) <= age <= timedelta(seconds=2 * self._poll_interval_sec)

    def stop(self) -> None:
        self._stop_event.set()
        if self._closed:
            return
        self._closed = True
        self._provider.close()
        self._api_client.close()


def build_official_market_calendar_provider(
    *,
    provider: str,
    ibkr_gateway_url: str | None = None,
    saxo_access_token: str | None = None,
    saxo_access_token_expires_at: str | None = None,
    nse_market: str | None = None,
    nse_lease_seconds: int = 120,
) -> OfficialMarketCalendarProvider:
    """Build one explicitly configured official source."""
    normalized = str(provider or "").strip().lower()
    if normalized == "ibkr":
        gateway_url = str(ibkr_gateway_url or "").strip()
        if not gateway_url:
            msg = "IBKR market-calendar sync requires IBKR_GATEWAY_URL"
            raise ValueError(msg)
        return IBKROfficialMarketCalendarProvider(gateway_url=gateway_url)
    if normalized == "saxo_live":
        return SaxoOfficialMarketCalendarProvider(
            access_token=saxo_access_token,
            access_token_expires_at=saxo_access_token_expires_at,
        )
    if normalized == "nse":
        return NSEOfficialMarketCalendarProvider(
            market=str(nse_market or ""),
            lease_seconds=nse_lease_seconds,
        )
    supported = ", ".join(sorted(_PROVIDER_BROKER_CODES))
    msg = f"Unknown MARKET_CALENDAR_PROVIDER {provider!r}; expected one of: {supported}"
    raise ValueError(msg)


__all__ = [
    "BackendMarketCalendarClient",
    "MarketCalendarSyncError",
    "MarketCalendarSyncSummary",
    "MarketCalendarSyncWorker",
    "build_official_market_calendar_provider",
]
