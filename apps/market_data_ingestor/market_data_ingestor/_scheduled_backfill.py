"""Scheduled-market daily backfill policy pieces (private to ``backfill``).

Used only by :class:`market_data_ingestor.backfill.HistoricalBackfiller`; the
one-shot ``backfill`` command remains the single backfill surface for both
market kinds.

Why scheduled markets get a distinct policy from the continuous (crypto)
paging path:

- **Coverage is calendar-aware.** Weekends and exchange holidays are not
  gaps, so the gate compares upserted rows against the expected trading
  sessions from ``lib_data.sessions.sessions_between``, not wall-clock
  bucket counts.
- **One request per symbol.** A multi-year daily history is only ~hundreds
  to ~2k rows and fits a single provider response; request-sized window
  paging (and its empty-window budget) does not apply at 1d granularity.
- **Corporate actions** (splits/dividends) exist only for scheduled markets
  and are loaded through :class:`ICorporateActionSource` when the configured
  provider exposes it — the source is derived structurally from the
  configured provider, never hard-wired to a vendor.

Both policies share the fail-closed core in ``backfill.py``: exact instrument
identity, per-row source/timeframe provenance validation, and loud coverage
gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Protocol, cast

from sqlalchemy import select

from lib_application.db.models import CorporateAction
from lib_data.sessions import sessions_between

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any

    from lib_data.market_data import CandleRow
    from lib_infrastructure.market_data.eodhd_client import DividendRecord, SplitRecord
    from lib_infrastructure.market_data.ports import IMarketDataProvider

# Coverage is judged against the exchange calendar (expected trading
# sessions), not wall-clock buckets, so the floor is strict.
SCHEDULED_DAILY_MIN_COVERAGE = 0.97
_ACTION_SPLIT = "split"
_ACTION_DIVIDEND = "dividend"


class ICorporateActionSource(Protocol):
    """The corporate-action slice of a market-data provider (duck-typed)."""

    source: str

    def fetch_splits(self, *, product_id: str, start: date, end: date) -> Sequence[SplitRecord]:
        """Split events with ex-dates in ``[start, end]``."""
        ...

    def fetch_dividends(
        self, *, product_id: str, start: date, end: date
    ) -> Sequence[DividendRecord]:
        """Cash dividends with ex-dates in ``[start, end]``."""
        ...


def derive_corporate_action_source(
    provider: IMarketDataProvider,
) -> ICorporateActionSource | None:
    """Return ``provider`` when it also serves corporate actions, else ``None``."""
    if callable(getattr(provider, "fetch_splits", None)) and callable(
        getattr(provider, "fetch_dividends", None)
    ):
        return cast("ICorporateActionSource", provider)
    return None


@dataclass(frozen=True)
class ScheduledDailySummary:
    """Per-symbol outcome of a scheduled-market daily backfill."""

    symbol: str
    instr_id: int
    expected_sessions: int
    rows_upserted: int
    first_session: date
    last_session: date
    narrowed: bool

    @property
    def meets_coverage(self) -> bool:
        """Whether upserted rows satisfy the calendar-session coverage floor."""
        if self.expected_sessions == 0:
            return False
        return self.rows_upserted / self.expected_sessions >= SCHEDULED_DAILY_MIN_COVERAGE


def expected_scheduled_sessions(calendar_mic: str, first: date, last: date) -> int:
    """Expected trading-session count on ``calendar_mic`` in ``[first, last]``."""
    return len(sessions_between(calendar_mic, first, last))


def compute_scheduled_daily_summary(
    *,
    symbol: str,
    instr_id: int,
    rows: Sequence[CandleRow],
    rows_upserted: int,
    requested_first: date,
    requested_last: date,
    calendar_mic: str,
) -> ScheduledDailySummary:
    """Summarize one fetched+upserted daily span against the exchange calendar.

    ``rows`` must be non-empty and timestamp-sorted (the backfiller's validated
    output). A span starting after ``requested_first`` or ending before
    ``requested_last`` (listing/delisting) is flagged NARROWED, and expected
    coverage is judged on the span the vendor actually served.
    """
    first_session = rows[0].ts.date()
    last_session = rows[-1].ts.date()
    return ScheduledDailySummary(
        symbol=symbol,
        instr_id=instr_id,
        expected_sessions=expected_scheduled_sessions(calendar_mic, first_session, last_session),
        rows_upserted=rows_upserted,
        first_session=first_session,
        last_session=last_session,
        narrowed=first_session > requested_first or last_session < requested_last,
    )


def upsert_corporate_actions(
    *,
    session_factory: Callable[[], Any],
    source: str,
    instr_id: int,
    splits: Sequence[SplitRecord],
    dividends: Sequence[DividendRecord],
) -> int:
    """Insert new splits/dividends for one instrument; returns the insert count.

    Idempotent on the ``(instr_id, action_type, ex_date, source)`` unique key
    via a portable select-then-insert (volumes here are tiny). Rows keep the
    corporate-action source's own provenance tag — never relabelled.
    """
    with session_factory() as session:
        existing = set(
            session.execute(
                select(CorporateAction.action_type, CorporateAction.ex_date).where(
                    CorporateAction.instr_id == instr_id,
                    CorporateAction.source == source,
                )
            ).all()
        )
        inserted = 0
        for split in splits:
            if (_ACTION_SPLIT, split.ex_date) in existing:
                continue
            session.add(
                CorporateAction(
                    instr_id=instr_id,
                    action_type=_ACTION_SPLIT,
                    ex_date=split.ex_date,
                    ratio=split.ratio,
                    source=source,
                )
            )
            inserted += 1
        for dividend in dividends:
            if (_ACTION_DIVIDEND, dividend.ex_date) in existing:
                continue
            session.add(
                CorporateAction(
                    instr_id=instr_id,
                    action_type=_ACTION_DIVIDEND,
                    ex_date=dividend.ex_date,
                    amount=dividend.amount,
                    currency=dividend.currency,
                    source=source,
                )
            )
            inserted += 1
        session.commit()
    return inserted
