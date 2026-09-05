"""Authoritative market-session admission and persistence tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from execution_engine.execution_gates import ExecutionGatekeeper
from execution_engine.market_sessions import (
    MarketSessionDecision,
    SqlMarketSessionProvider,
)
from lib_application.db.models import (
    Base,
    EquityObservation,
    EquitySourceLineage,
    Instrument,
    MarketCalendar,
    MarketSession,
)
from lib_application.services.market_calendars import (
    MarketSessionWindow,
    replace_market_calendar,
)
from lib_application.services.strategy_panel_sessions import market_session_content_sha256
from lib_strategy.signals.signal import Signal, SignalAction

# Friday 24 July 2026 was an ordinary Nasdaq session. The regular-session
# interval below is the exchange's published 09:30-16:00 America/New_York
# schedule represented in UTC (EDT).
_OBSERVED_AT = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)
_OPEN_AT = datetime(2026, 7, 24, 13, 30, tzinfo=UTC)
_CLOSE_AT = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
_COVERAGE_START = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
_COVERAGE_END = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)
_NASDAQ_SOURCE = "https://www.nasdaq.com/market-activity/stock-market-holiday-schedule"


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _instrument(
    session_factory,
    *,
    asset_class: str,
    canonical: str,
    is_tradable: bool | None = None,
) -> int:
    with session_factory() as session:
        values: dict[str, Any] = {
            "asset_class": asset_class,
            "canonical": canonical,
            "exchange": "NASDAQ" if asset_class in {"equity", "etf"} else "global",
            "settlement_currency": "USD",
        }
        if is_tradable is not None:
            values["is_tradable"] = is_tradable
        row = Instrument(
            **values,
        )
        session.add(row)
        session.commit()
        return int(row.instr_id)


def _signal(
    instrument_id: int,
    *,
    action: SignalAction = SignalAction.LONG,
    asset_class: str = "equity",
    symbol: str = "AAPL",
) -> Signal:
    return Signal(
        strategy_id="market_session_contract",
        strategy_type="indicator",
        symbol=symbol,
        asset_class=asset_class,
        instrument_id=instrument_id,
        action=action,
        confidence=0.8,
        timestamp=_OPEN_AT,
        entry_price=225.01,
        external_signal_id=f"market-session-{instrument_id}-{action.value}",
    )


def _sync_nasdaq_calendar(session_factory, instrument_id: int) -> None:
    with session_factory() as session:
        replace_market_calendar(
            session,
            code="XNAS:REGULAR",
            source_kind="exchange",
            provider="nasdaq",
            source_reference=_NASDAQ_SOURCE,
            observed_at=_OBSERVED_AT,
            coverage_start=_COVERAGE_START,
            coverage_end=_COVERAGE_END,
            windows=[MarketSessionWindow(opens_at=_OPEN_AT, closes_at=_CLOSE_AT)],
            instrument_ids=[instrument_id],
            now=_OBSERVED_AT,
        )
        session.commit()


def _attach_calendar_lineage(
    session_factory,
    *,
    lineage_id: str = "a" * 64,
    observation_id: str = "c" * 64,
    observed_at: datetime = _OBSERVED_AT,
    dataset_version: str = "2026-07-24",
    source_revision: str = "1",
    revision: int = 1,
    lineage_content_sha256: str = "b" * 64,
    observation_content_sha256: str = "d" * 64,
    coverage_end: datetime | None = None,
) -> str:
    with session_factory() as session:
        lineage = EquitySourceLineage(
            lineage_id=lineage_id,
            provider="nasdaq",
            product="regular-session-calendar",
            endpoint=_NASDAQ_SOURCE,
            dataset_version=dataset_version,
            tool_version="calendar-ingestor-v1",
            source_identity="XNAS:REGULAR",
            source_revision=source_revision,
            retrieved_at=observed_at,
            timestamp_semantics={"opens_at": "UTC", "closes_at": "UTC"},
            adjustment_policy="not-applicable",
            entitlement_scope="public",
            missing_data_policy="fail-closed",
            content_sha256=lineage_content_sha256,
        )
        observation = EquityObservation(
            observation_id=observation_id,
            lineage_id=lineage.lineage_id,
            instr_id=None,
            observation_kind="calendar",
            source_record_identity=_NASDAQ_SOURCE,
            event_at=observed_at,
            available_at=observed_at,
            revision=revision,
            disposition="observed",
            content_sha256=observation_content_sha256,
        )
        session.add_all((lineage, observation))
        calendar = session.execute(
            select(MarketCalendar).where(MarketCalendar.code == "XNAS:REGULAR")
        ).scalar_one()
        calendar.observation_id = observation.observation_id
        calendar.observed_at = observed_at
        if coverage_end is not None:
            calendar.coverage_end = coverage_end
        session.commit()
        window = session.execute(
            select(MarketSession).where(MarketSession.calendar_id == calendar.calendar_id)
        ).scalar_one()
        return market_session_content_sha256(calendar, window, lineage)


def test_crypto_is_persisted_as_an_explicit_continuous_market(session_factory) -> None:
    instrument_id = _instrument(
        session_factory,
        asset_class="crypto",
        canonical="BTC/USD",
    )
    provider = SqlMarketSessionProvider(
        session_factory,
        max_observation_age=timedelta(hours=1),
        clock=lambda: _OPEN_AT,
    )

    decision = provider.evaluate(
        _signal(
            instrument_id,
            asset_class="crypto",
            symbol="BTC/USD",
        )
    )

    assert decision == MarketSessionDecision(
        is_open=True,
        reason="continuous_market",
        message="BTC/USD is explicitly classified as a continuous crypto market",
    )
    with session_factory() as session:
        instrument = session.get(Instrument, instrument_id)
        assert instrument is not None
        assert instrument.market_session_policy == "continuous"
        assert instrument.market_calendar_id is None


def test_scheduled_entry_requires_an_assigned_authoritative_calendar(session_factory) -> None:
    instrument_id = _instrument(
        session_factory,
        asset_class="equity",
        canonical="AAPL",
    )
    provider = SqlMarketSessionProvider(
        session_factory,
        max_observation_age=timedelta(hours=1),
        clock=lambda: _OPEN_AT,
    )

    decision = provider.evaluate(_signal(instrument_id))

    assert decision.is_open is False
    assert decision.reason == "market_session_unavailable"
    assert "no authoritative market calendar" in decision.message


def test_cash_index_is_persisted_as_reference_only_and_never_admitted(
    session_factory,
) -> None:
    instrument_id = _instrument(
        session_factory,
        asset_class="index",
        canonical="NIFTY50",
    )
    provider = SqlMarketSessionProvider(
        session_factory,
        max_observation_age=timedelta(hours=1),
        clock=lambda: _OPEN_AT,
    )
    signal = _signal(
        instrument_id,
        asset_class="index",
        symbol="NIFTY50",
    )

    decision = provider.evaluate_tradability(signal)

    assert decision.is_open is False
    assert decision.reason == "instrument_not_tradable"
    assert "reference-only" in decision.message
    with session_factory() as session:
        instrument = session.get(Instrument, instrument_id)
        assert instrument is not None
        assert instrument.is_tradable is False
        assert instrument.market_session_policy == "scheduled"


def test_database_constraint_rejects_tradable_cash_index(session_factory) -> None:
    with session_factory() as session:
        session.add(
            Instrument(
                asset_class="index",
                canonical="BANKNIFTY",
                exchange="NSE",
                settlement_currency="INR",
                market_session_policy="scheduled",
                is_tradable=True,
            )
        )
        with pytest.raises(IntegrityError, match="ck_index_reference_only"):
            session.commit()


def test_fresh_authoritative_regular_session_admits_entry(session_factory) -> None:
    instrument_id = _instrument(
        session_factory,
        asset_class="equity",
        canonical="AAPL",
    )
    _sync_nasdaq_calendar(session_factory, instrument_id)
    provider = SqlMarketSessionProvider(
        session_factory,
        max_observation_age=timedelta(hours=3),
        clock=lambda: datetime(2026, 7, 24, 15, 0, tzinfo=UTC),
    )

    decision = provider.evaluate(_signal(instrument_id))

    assert decision.is_open is True
    assert decision.reason == "market_open"
    assert decision.calendar_code == "XNAS:REGULAR"
    assert decision.provider == "nasdaq"


def test_rebalance_requires_exact_open_and_pinned_semantic_digest(session_factory) -> None:
    instrument_id = _instrument(
        session_factory,
        asset_class="equity",
        canonical="AAPL",
    )
    _sync_nasdaq_calendar(session_factory, instrument_id)
    digest = _attach_calendar_lineage(session_factory)
    provider = SqlMarketSessionProvider(
        session_factory,
        max_observation_age=timedelta(hours=3),
        clock=lambda: datetime(2026, 7, 24, 15, 0, tzinfo=UTC),
    )
    signal = _signal(instrument_id)

    admitted = provider.evaluate_pinned_rebalance_session(
        signal,
        expected_open_at=_OPEN_AT,
        expected_content_sha256=digest,
    )
    changed = provider.evaluate_pinned_rebalance_session(
        signal,
        expected_open_at=_OPEN_AT,
        expected_content_sha256="f" * 64,
    )
    wrong_open = provider.evaluate_pinned_rebalance_session(
        signal,
        expected_open_at=_OPEN_AT + timedelta(days=1),
        expected_content_sha256=digest,
    )

    assert admitted.is_open is True
    assert admitted.reason == "rebalance_execution_session_pinned"
    assert changed.reason == "rebalance_execution_session_digest_mismatch"
    assert wrong_open.reason == "rebalance_execution_session_mismatch"


def test_rebalance_session_allows_coverage_refresh_but_rejects_window_correction(
    session_factory,
) -> None:
    instrument_id = _instrument(
        session_factory,
        asset_class="equity",
        canonical="AAPL",
    )
    _sync_nasdaq_calendar(session_factory, instrument_id)
    pinned_digest = _attach_calendar_lineage(session_factory)
    refreshed_digest = _attach_calendar_lineage(
        session_factory,
        lineage_id="e" * 64,
        observation_id="f" * 64,
        observed_at=_OBSERVED_AT + timedelta(hours=1),
        dataset_version="2026-07-31",
        source_revision="2",
        revision=2,
        lineage_content_sha256="1" * 64,
        observation_content_sha256="2" * 64,
        coverage_end=_COVERAGE_END + timedelta(days=7),
    )
    provider = SqlMarketSessionProvider(
        session_factory,
        max_observation_age=timedelta(hours=3),
        clock=lambda: datetime(2026, 7, 24, 15, 0, tzinfo=UTC),
    )
    signal = _signal(instrument_id)

    refreshed = provider.evaluate_pinned_rebalance_session(
        signal,
        expected_open_at=_OPEN_AT,
        expected_content_sha256=pinned_digest,
    )
    with session_factory() as session:
        window = session.execute(
            select(MarketSession).where(MarketSession.opens_at == _OPEN_AT)
        ).scalar_one()
        window.closes_at = _CLOSE_AT - timedelta(minutes=5)
        session.commit()
    corrected = provider.evaluate_pinned_rebalance_session(
        signal,
        expected_open_at=_OPEN_AT,
        expected_content_sha256=pinned_digest,
    )

    assert refreshed_digest == pinned_digest
    assert refreshed.is_open is True
    assert refreshed.reason == "rebalance_execution_session_pinned"
    assert corrected.is_open is False
    assert corrected.reason == "rebalance_execution_session_digest_mismatch"


def test_complete_coverage_without_an_open_window_is_closed(session_factory) -> None:
    instrument_id = _instrument(
        session_factory,
        asset_class="equity",
        canonical="AAPL",
    )
    _sync_nasdaq_calendar(session_factory, instrument_id)
    provider = SqlMarketSessionProvider(
        session_factory,
        max_observation_age=timedelta(hours=12),
        clock=lambda: datetime(2026, 7, 24, 21, 0, tzinfo=UTC),
    )

    decision = provider.evaluate(_signal(instrument_id))

    assert decision.is_open is False
    assert decision.reason == "market_closed"
    assert "reports no open regular session" in decision.message


def test_stale_or_out_of_coverage_calendar_fails_closed(session_factory) -> None:
    instrument_id = _instrument(
        session_factory,
        asset_class="equity",
        canonical="AAPL",
    )
    _sync_nasdaq_calendar(session_factory, instrument_id)
    stale_provider = SqlMarketSessionProvider(
        session_factory,
        max_observation_age=timedelta(minutes=30),
        clock=lambda: datetime(2026, 7, 24, 15, 0, tzinfo=UTC),
    )
    uncovered_provider = SqlMarketSessionProvider(
        session_factory,
        max_observation_age=timedelta(days=2),
        clock=lambda: datetime(2026, 7, 25, 1, 0, tzinfo=UTC),
    )

    stale = stale_provider.evaluate(_signal(instrument_id))
    uncovered = uncovered_provider.evaluate(_signal(instrument_id))

    assert stale.reason == "market_session_unavailable"
    assert "stale" in stale.message
    assert uncovered.reason == "market_session_unavailable"
    assert "does not cover" in uncovered.message


def test_calendar_replacement_is_complete_and_atomic(session_factory) -> None:
    instrument_id = _instrument(
        session_factory,
        asset_class="equity",
        canonical="AAPL",
    )
    omitted_instrument_id = _instrument(
        session_factory,
        asset_class="equity",
        canonical="MSFT",
    )
    with session_factory() as session:
        replace_market_calendar(
            session,
            code="XNAS:REGULAR",
            source_kind="exchange",
            provider="nasdaq",
            source_reference=_NASDAQ_SOURCE,
            observed_at=_OBSERVED_AT,
            coverage_start=_COVERAGE_START,
            coverage_end=_COVERAGE_END,
            windows=[MarketSessionWindow(opens_at=_OPEN_AT, closes_at=_CLOSE_AT)],
            instrument_ids=[instrument_id, omitted_instrument_id],
            now=_OBSERVED_AT,
        )
        session.commit()
    with session_factory() as session:
        replace_market_calendar(
            session,
            code="XNAS:REGULAR",
            source_kind="exchange",
            provider="nasdaq",
            source_reference=_NASDAQ_SOURCE,
            observed_at=datetime(2026, 7, 24, 13, 5, tzinfo=UTC),
            coverage_start=_COVERAGE_START,
            coverage_end=_COVERAGE_END,
            windows=[],
            instrument_ids=[instrument_id],
            now=datetime(2026, 7, 24, 13, 5, tzinfo=UTC),
        )
        session.commit()

    with session_factory() as session:
        calendar = session.execute(
            select(MarketCalendar).where(MarketCalendar.code == "XNAS:REGULAR")
        ).scalar_one()
        assert (
            session.execute(
                select(MarketSession).where(MarketSession.calendar_id == calendar.calendar_id)
            )
            .scalars()
            .all()
            == []
        )
        instrument = session.get(Instrument, instrument_id)
        assert instrument is not None
        assert instrument.market_calendar_id == calendar.calendar_id
        omitted_instrument = session.get(Instrument, omitted_instrument_id)
        assert omitted_instrument is not None
        assert omitted_instrument.market_session_policy == "scheduled"
        assert omitted_instrument.market_calendar_id is None


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (
            {"source_reference": "http://untrusted.invalid/schedule"},
            "HTTPS official source",
        ),
        (
            {
                "windows": [
                    MarketSessionWindow(
                        opens_at=_OPEN_AT,
                        closes_at=_CLOSE_AT,
                    ),
                    MarketSessionWindow(
                        opens_at=_CLOSE_AT - timedelta(minutes=1),
                        closes_at=_CLOSE_AT + timedelta(minutes=1),
                    ),
                ]
            },
            "cannot overlap",
        ),
    ],
)
def test_calendar_sync_rejects_unverifiable_or_overlapping_input(
    session_factory,
    kwargs: dict[str, Any],
    match: str,
) -> None:
    instrument_id = _instrument(
        session_factory,
        asset_class="equity",
        canonical="AAPL",
    )
    arguments = {
        "code": "XNAS:REGULAR",
        "source_kind": "exchange",
        "provider": "nasdaq",
        "source_reference": _NASDAQ_SOURCE,
        "observed_at": _OBSERVED_AT,
        "coverage_start": _COVERAGE_START,
        "coverage_end": _COVERAGE_END,
        "windows": [MarketSessionWindow(opens_at=_OPEN_AT, closes_at=_CLOSE_AT)],
        "instrument_ids": [instrument_id],
        "now": _OBSERVED_AT,
        **kwargs,
    }

    with session_factory() as session, pytest.raises(ValueError, match=match):
        replace_market_calendar(session, **arguments)


def test_calendar_sync_rejects_crypto_assignment(session_factory) -> None:
    instrument_id = _instrument(
        session_factory,
        asset_class="crypto",
        canonical="BTC/USD",
    )

    with session_factory() as session, pytest.raises(ValueError, match="explicitly continuous"):
        replace_market_calendar(
            session,
            code="COINBASE:REGULAR",
            source_kind="exchange",
            provider="coinbase",
            source_reference="https://www.coinbase.com/",
            observed_at=_OBSERVED_AT,
            coverage_start=_COVERAGE_START,
            coverage_end=_COVERAGE_END,
            windows=[MarketSessionWindow(opens_at=_COVERAGE_START, closes_at=_COVERAGE_END)],
            instrument_ids=[instrument_id],
            now=_OBSERVED_AT,
        )


class _UnexpectedProvider:
    def evaluate(self, signal: Signal) -> MarketSessionDecision:
        msg = f"provider must not be called for {signal.action}"
        raise AssertionError(msg)


def _gate(provider=None) -> ExecutionGatekeeper:
    gate = object.__new__(ExecutionGatekeeper)
    gate._market_session_provider = provider
    return gate


def test_reduce_only_close_remains_eligible_outside_market_session() -> None:
    gate = _gate(_UnexpectedProvider())

    error = gate.check_market_session(
        signal=_signal(42, action=SignalAction.CLOSE),
        environment="live",
        trace_ctx={},
        allow_historical_replay=False,
    )

    assert error is None


@pytest.mark.parametrize(
    ("action", "allow_historical_replay"),
    [
        (SignalAction.LONG, False),
        (SignalAction.CLOSE, False),
        (SignalAction.LONG, True),
    ],
)
def test_reference_only_index_is_blocked_before_session_exemptions(
    session_factory,
    action: SignalAction,
    allow_historical_replay: bool,
) -> None:
    instrument_id = _instrument(
        session_factory,
        asset_class="index",
        canonical="NIFTY50",
    )
    provider = SqlMarketSessionProvider(
        session_factory,
        max_observation_age=timedelta(hours=1),
        clock=lambda: _OPEN_AT,
    )
    gate = _gate(provider)
    signal = _signal(
        instrument_id,
        action=action,
        asset_class="index",
        symbol="NIFTY50",
    )

    tradability_error = gate.check_instrument_tradability(
        signal=signal,
        environment="paper",
        trace_ctx={},
    )

    assert tradability_error is not None
    assert tradability_error[0] == "instrument_not_tradable"
    # The historical and reduce-only exemptions apply only to session timing;
    # they cannot turn a reference instrument into an executable contract.
    if action == SignalAction.CLOSE or allow_historical_replay:
        assert (
            gate.check_market_session(
                signal=signal,
                environment="paper",
                trace_ctx={},
                allow_historical_replay=allow_historical_replay,
            )
            is None
        )


def test_live_historical_replay_is_forbidden_before_session_lookup() -> None:
    gate = _gate(_UnexpectedProvider())

    error = gate.check_market_session(
        signal=_signal(42),
        environment="live",
        trace_ctx={},
        allow_historical_replay=True,
    )

    assert error is not None
    assert error[0] == "live_historical_replay_forbidden"
