"""SEC EDGAR earnings-date loader.

Reference: strategies/indicator/USQualityCompounder/README.md, "Data and Failure Rules".

Populates ``earnings_events`` from 8-K filings carrying Item 2.02 (Results of
Operations and Financial Condition): the filing date is the announcement date
and the EDGAR acceptance timestamp is a true point-in-time record of when the
event became public. The submissions field's trailing ``Z`` is not interpreted
as UTC: SEC exposes the authoritative EDGAR Eastern wall clock, which the
shared parser converts to UTC. ``announce_time`` (bmo/amc/dmh) is derived from
that instant in New York — filings typically follow the press release within
hours, and the residual skew is covered by the §4.5 disclosed assumption (the
blackout spans [T-3, T+1] regardless).
"""

from __future__ import annotations

import time
from datetime import date, datetime
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from lib_application.db.models import EarningsEvent
from lib_common.logging import get_logger

from .sec_edgar import parse_submissions_acceptance_time

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = get_logger(__name__)

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
_EARNINGS_ITEM = "2.02"
_EARNINGS_FORMS = frozenset({"8-K", "8-K/A"})
_SOURCE = "edgar_8k"
_NEW_YORK = ZoneInfo("America/New_York")
_MARKET_OPEN_ET = dt_time(9, 30)
_MARKET_CLOSE_ET = dt_time(16, 0)
_MIN_REQUEST_INTERVAL_SEC = 0.13  # SEC fair-access guidance: stay under ~10 req/s
_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_SEC = 0.5
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_MIN_ERROR_STATUS = 400
_CIK_WIDTH = 10


class EdgarEarningsError(RuntimeError):
    """Raised on EDGAR transport failures or unresolvable tickers (fail loud)."""


def classify_announce_time(acceptance_utc: datetime) -> str:
    """Map an EDGAR acceptance timestamp to the bmo/amc/dmh reaction slot."""
    eastern = acceptance_utc.astimezone(_NEW_YORK).time()
    if eastern < _MARKET_OPEN_ET:
        return "bmo"
    if eastern >= _MARKET_CLOSE_ET:
        return "amc"
    return "dmh"


def select_earnings_filings(
    columns: Mapping[str, list[Any]], *, since: date
) -> dict[date, datetime]:
    """Extract {filing_date: earliest acceptance} for Item-2.02 8-K rows.

    ``columns`` is the EDGAR column-oriented filing block (``form``,
    ``items``, ``filingDate``, ``acceptanceDateTime`` arrays of equal length).
    """
    events: dict[date, datetime] = {}
    forms = columns.get("form", [])
    items_column = columns.get("items", [])
    filing_dates = columns.get("filingDate", [])
    acceptances = columns.get("acceptanceDateTime", [])
    for form, items, filing_date_raw, acceptance_raw in zip(
        forms, items_column, filing_dates, acceptances, strict=True
    ):
        if form not in _EARNINGS_FORMS:
            continue
        if _EARNINGS_ITEM not in str(items or "").split(","):
            continue
        filing_date = date.fromisoformat(str(filing_date_raw))
        if filing_date < since:
            continue
        accepted = parse_submissions_acceptance_time(acceptance_raw)
        existing = events.get(filing_date)
        if existing is None or accepted < existing:
            events[filing_date] = accepted
    return events


class EdgarEarningsLoader:
    """Fetches Item-2.02 8-K dates per CIK and upserts ``earnings_events`` rows.

    A descriptive ``user_agent`` is mandatory (SEC fair-access policy); the
    CLI passes the real operator contact — never a fabricated identity.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        user_agent: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not user_agent.strip():
            msg = "EDGAR requires a descriptive User-Agent (operator contact)"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._client = client or httpx.Client(timeout=30.0)
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._last_request_monotonic = 0.0

    def close(self) -> None:
        self._client.close()

    def fetch_earnings_dates(
        self,
        tickers: list[str],
        *,
        since: date,
        cik_overrides: Mapping[str, str] | None = None,
    ) -> dict[str, dict[date, datetime]]:
        """Fetch {ticker: {report_date: acceptance}} with no DB involvement.

        ``cik_overrides`` covers deregistered issuers (e.g. acquired names)
        that no longer appear in company_tickers.json. Tickers sharing a CIK
        (GOOGL/GOOG) are fetched once.
        """
        cik_by_ticker = self._resolve_ciks(tickers, overrides=cik_overrides or {})
        events_by_cik: dict[str, dict[date, datetime]] = {}
        for cik in sorted(set(cik_by_ticker.values())):
            events_by_cik[cik] = self._fetch_earnings_dates(cik, since=since)
        return {ticker: events_by_cik[cik_by_ticker[ticker]] for ticker in tickers}

    def load_for_instruments(
        self,
        instruments: Mapping[str, int],
        *,
        since: date,
        cik_overrides: Mapping[str, str] | None = None,
    ) -> dict[str, int]:
        """Load earnings events for ``{ticker: instr_id}``; returns inserted counts."""
        events_by_ticker = self.fetch_earnings_dates(
            list(instruments), since=since, cik_overrides=cik_overrides
        )
        inserted_by_ticker: dict[str, int] = {}
        for ticker, instr_id in instruments.items():
            events = events_by_ticker[ticker]
            inserted_by_ticker[ticker] = self._upsert_events(instr_id, events)
            logger.info(
                "EDGAR earnings for %s: %d events, %d inserted",
                ticker,
                len(events),
                inserted_by_ticker[ticker],
            )
        return inserted_by_ticker

    def _resolve_ciks(self, tickers: list[str], *, overrides: Mapping[str, str]) -> dict[str, str]:
        payload = self._get_json(_TICKERS_URL, label="company_tickers")
        by_ticker = {
            str(entry["ticker"]).upper(): f"{int(entry['cik_str']):0{_CIK_WIDTH}d}"
            for entry in payload.values()
        }
        resolved: dict[str, str] = {}
        missing: list[str] = []
        for ticker in tickers:
            override = overrides.get(ticker.upper())
            if override is not None:
                resolved[ticker] = f"{int(override):0{_CIK_WIDTH}d}"
                continue
            cik = by_ticker.get(ticker.upper())
            if cik is None:
                missing.append(ticker)
            else:
                resolved[ticker] = cik
        if missing:
            msg = f"No EDGAR CIK for: {', '.join(sorted(missing))} (add a cik_overrides entry)"
            raise EdgarEarningsError(msg)
        return resolved

    def _fetch_earnings_dates(self, cik: str, *, since: date) -> dict[date, datetime]:
        submissions = self._get_json(f"{_SUBMISSIONS_BASE}/CIK{cik}.json", label=f"CIK{cik}")
        filings = submissions.get("filings", {})
        events = select_earnings_filings(filings.get("recent", {}), since=since)
        for page in filings.get("files", []):
            filing_to = page.get("filingTo")
            if filing_to and date.fromisoformat(str(filing_to)) < since:
                continue
            older = self._get_json(f"{_SUBMISSIONS_BASE}/{page['name']}", label=str(page["name"]))
            for filing_date, accepted in select_earnings_filings(older, since=since).items():
                existing = events.get(filing_date)
                if existing is None or accepted < existing:
                    events[filing_date] = accepted
        return events

    def _upsert_events(self, instr_id: int, events: dict[date, datetime]) -> int:
        with self._session_factory() as session:
            existing = {
                row[0]
                for row in session.execute(
                    select(EarningsEvent.report_date).where(
                        EarningsEvent.instr_id == instr_id,
                        EarningsEvent.source == _SOURCE,
                    )
                ).all()
            }
            inserted = 0
            for report_date in sorted(events):
                if report_date in existing:
                    continue
                accepted = events[report_date]
                session.add(
                    EarningsEvent(
                        instr_id=instr_id,
                        report_date=report_date,
                        announce_time=classify_announce_time(accepted),
                        status="actual",
                        first_seen_at=accepted,
                        source=_SOURCE,
                    )
                )
                inserted += 1
            session.commit()
        return inserted

    def _get_json(self, url: str, *, label: str) -> Any:
        for attempt in range(_MAX_ATTEMPTS):
            if attempt:
                time.sleep(_RETRY_BASE_DELAY_SEC * 2 ** (attempt - 1))
            self._throttle()
            try:
                response = self._client.get(url, headers=self._headers)
            except httpx.RequestError as exc:
                logger.warning(
                    "EDGAR request error for %s (attempt %d/%d): %s",
                    label,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    type(exc).__name__,
                )
                continue
            if response.status_code in _RETRYABLE_STATUSES:
                logger.warning(
                    "EDGAR HTTP %d for %s (attempt %d/%d)",
                    response.status_code,
                    label,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                )
                continue
            if response.status_code >= _MIN_ERROR_STATUS:
                msg = f"EDGAR request for {label} failed with HTTP {response.status_code}"
                raise EdgarEarningsError(msg)
            try:
                return response.json()
            except ValueError as exc:
                msg = f"EDGAR returned a non-JSON body for {label}"
                raise EdgarEarningsError(msg) from exc
        msg = f"EDGAR request for {label} failed after {_MAX_ATTEMPTS} attempts"
        raise EdgarEarningsError(msg)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_monotonic
        if elapsed < _MIN_REQUEST_INTERVAL_SEC:
            time.sleep(_MIN_REQUEST_INTERVAL_SEC - elapsed)
        self._last_request_monotonic = time.monotonic()
