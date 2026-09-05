"""RG-1: resolve_risk_baseline sources day-start and peak equity while keeping
exact persisted-metric provenance distinct from the ordinary bootstrap.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import SQLAlchemyError

from execution_engine.risk_baseline import (
    AccountEquityObservation,
    AccountEquityObservationSource,
    RiskBaseline,
    RiskBaselineUnavailableError,
    resolve_risk_baseline,
)
from lib_application.db.models import (
    Base,
    Broker,
    DailyNav,
    ExecutionMetric,
    LinkedBrokerAccount,
    Strategy,
    User,
)
from lib_application.db.session import create_engine_for_env, get_session_factory


def _session_factory():
    engine = create_engine_for_env(env="test")
    Base.metadata.create_all(engine)
    session_factory = get_session_factory(engine=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add_all(
            [
                User(user_id="user-1", email="user-1@example.com", base_ccy="EUR"),
                Broker(broker_id=1, code="paper", name="Paper", capabilities={}),
                Strategy(strategy_id="s1", strategy_name="Strategy 1"),
            ]
        )
        session.flush()
        session.add(
            LinkedBrokerAccount(
                account_id=1,
                user_id="user-1",
                broker_id=1,
                environment="paper",
                display_name="EUR paper",
                base_ccy="EUR",
                status="connected",
                paper_initial_equity=Decimal("10000"),
                paper_initial_cash=Decimal("10000"),
            )
        )
        session.commit()
    return session_factory


def _metric(
    metric_id: str,
    *,
    peak: Decimal,
    created: datetime,
    account_id: int = 1,
    user_id: str = "user-1",
) -> ExecutionMetric:
    return ExecutionMetric(
        metric_id=metric_id,
        user_id=user_id,
        account_id=account_id,
        strategy_id="s1",
        symbol="BTCUSD",
        execution_mode="spot",
        broker="paper",
        peak_equity=peak,
        created_at=created,
    )


def _observation(
    equity: float,
    *,
    user_id: str = "user-1",
    account_id: int = 1,
    currency: str = "EUR",
    source: AccountEquityObservationSource = "broker",
    observed_at: datetime | None = None,
    broker_reported_account_id: str | None = None,
) -> AccountEquityObservation:
    return AccountEquityObservation(
        user_id=user_id,
        account_id=account_id,
        broker_reported_account_id=(broker_reported_account_id or str(account_id)),
        currency=currency,
        equity=equity,
        observed_at=observed_at or datetime.now(tz=UTC) - timedelta(seconds=1),
        source=source,
    )


def test_none_session_factory_bootstraps_to_current_equity() -> None:
    assert resolve_risk_baseline(
        None,
        user_id="user-1",
        account_id=1,
        current_equity=5_000.0,
    ) == RiskBaseline(
        day_start_equity=5_000.0,
        peak_equity=5_000.0,
    )


def test_bootstraps_to_current_equity_without_history() -> None:
    sf = _session_factory()
    assert resolve_risk_baseline(
        sf,
        user_id="user-1",
        account_id=1,
        current_equity=12_000.0,
    ) == RiskBaseline(
        day_start_equity=12_000.0,
        peak_equity=12_000.0,
    )


@pytest.mark.parametrize("current_equity", [8_000.0, 12_000.0])
def test_fresh_paper_account_uses_exact_configured_peak(
    current_equity: float,
) -> None:
    sf = _session_factory()

    baseline = resolve_risk_baseline(
        sf,
        user_id="user-1",
        account_id=1,
        current_equity=current_equity,
        equity_observation=_observation(current_equity),
    )

    assert baseline.day_start_equity == current_equity
    assert baseline.peak_equity == 10_000.0
    assert baseline.has_persisted_account_peak is True
    assert baseline.peak_provenance is not None
    assert baseline.peak_provenance.source == "paper_account_initial_equity"
    assert baseline.peak_provenance.source_id == "linked-broker-account:1"
    assert baseline.peak_provenance.user_id == "user-1"
    assert baseline.peak_provenance.account_id == 1
    assert baseline.peak_provenance.currency == "EUR"
    assert baseline.current_equity_observation is not None
    assert baseline.current_equity_observation == _observation(
        current_equity,
        observed_at=baseline.current_equity_observation.observed_at,
    )


def test_profile_cache_cannot_authorize_paper_initial_peak() -> None:
    sf = _session_factory()
    baseline = resolve_risk_baseline(
        sf,
        user_id="user-1",
        account_id=1,
        current_equity=9_000.0,
        equity_observation=_observation(9_000.0, source="profile_cache"),
    )

    assert baseline.peak_equity == 9_000.0
    assert baseline.has_persisted_account_peak is False
    assert baseline.peak_provenance is None


def test_configured_external_account_identity_is_accepted_exactly() -> None:
    sf = _session_factory()
    with sf() as session:
        account = session.get(LinkedBrokerAccount, 1)
        assert account is not None
        account.external_ref = "paper-external-1"
        session.commit()

    baseline = resolve_risk_baseline(
        sf,
        user_id="user-1",
        account_id=1,
        current_equity=10_000.0,
        equity_observation=_observation(
            10_000.0,
            broker_reported_account_id="paper-external-1",
        ),
    )

    assert baseline.has_persisted_account_peak is True
    assert baseline.peak_provenance is not None
    assert baseline.peak_provenance.source == "paper_account_initial_equity"

    with pytest.raises(
        RiskBaselineUnavailableError,
        match="broker-reported account identity",
    ):
        resolve_risk_baseline(
            sf,
            user_id="user-1",
            account_id=1,
            current_equity=10_000.0,
            equity_observation=_observation(10_000.0),
        )


@pytest.mark.parametrize(
    ("observation", "message"),
    [
        (_observation(9_000.0, user_id="user-2"), "owned account"),
        (_observation(9_000.0, account_id=2), "owned account"),
        (_observation(9_000.0, currency="USD"), "does not match"),
        (
            _observation(9_000.0, broker_reported_account_id="wrong-account"),
            "broker-reported account identity",
        ),
        (_observation(8_000.0), "changed between"),
    ],
)
def test_current_equity_observation_mismatch_fails_closed(
    observation: AccountEquityObservation,
    message: str,
) -> None:
    sf = _session_factory()
    with pytest.raises(RiskBaselineUnavailableError, match=message):
        resolve_risk_baseline(
            sf,
            user_id="user-1",
            account_id=1,
            current_equity=9_000.0,
            equity_observation=observation,
        )


def test_current_equity_observation_future_dated_fails_closed() -> None:
    sf = _session_factory()
    evaluated_at = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)

    with pytest.raises(RiskBaselineUnavailableError, match="future-dated"):
        resolve_risk_baseline(
            sf,
            user_id="user-1",
            account_id=1,
            current_equity=9_000.0,
            equity_observation=_observation(
                9_000.0,
                observed_at=evaluated_at + timedelta(seconds=1),
            ),
            now=evaluated_at,
        )


def test_live_fresh_account_has_no_durable_bootstrap() -> None:
    sf = _session_factory()
    with sf() as session:
        session.add(
            LinkedBrokerAccount(
                account_id=2,
                user_id="user-1",
                broker_id=1,
                environment="live",
                display_name="Live EUR",
                base_ccy="EUR",
                status="connected",
                paper_initial_equity=None,
                paper_initial_cash=None,
            )
        )
        session.commit()

    baseline = resolve_risk_baseline(
        sf,
        user_id="user-1",
        account_id=2,
        current_equity=20_000.0,
        equity_observation=_observation(20_000.0, account_id=2),
    )

    assert baseline.peak_equity == 20_000.0
    assert baseline.has_persisted_account_peak is False


def test_day_start_uses_prior_day_close_not_today() -> None:
    sf = _session_factory()
    today = datetime.now(tz=UTC).date()
    with sf() as s:
        s.add(
            DailyNav(
                user_id="user-1",
                account_id=1,
                date=today - timedelta(days=1),
                nav_ccy="EUR",
                nav_value=Decimal("9000.00"),
            )
        )
        # Today's NAV must be ignored — we want the prior-day baseline.
        s.add(
            DailyNav(
                user_id="user-1",
                account_id=1,
                date=today,
                nav_ccy="EUR",
                nav_value=Decimal("9500.00"),
            )
        )
        s.commit()

    baseline = resolve_risk_baseline(
        sf,
        user_id="user-1",
        account_id=1,
        current_equity=8_800.0,
    )
    assert baseline.day_start_equity == 9000.0


def test_day_boundary_rebaselines_to_prior_day_nav() -> None:
    # H10: crossing UTC midnight, day_start_equity must flip to the prior day's
    # persisted NAV. `now` is injected so the boundary is deterministic.
    sf = _session_factory()
    with sf() as s:
        s.add(
            DailyNav(
                user_id="user-1",
                account_id=1,
                date=datetime(2026, 6, 27, tzinfo=UTC).date(),
                nav_ccy="EUR",
                nav_value=Decimal("9000.00"),
            )
        )
        s.commit()
    just_after_midnight = datetime(2026, 6, 28, 0, 5, tzinfo=UTC)
    baseline = resolve_risk_baseline(
        sf,
        user_id="user-1",
        account_id=1,
        current_equity=8_800.0,
        now=just_after_midnight,
    )
    assert baseline.day_start_equity == 9000.0  # prior close, not current equity


def test_day_boundary_is_utc_regardless_of_host_tz() -> None:
    # M10 (tz convention): the day boundary must be UTC-based, not host-local, so a
    # non-UTC deploy doesn't shift the daily-loss reset. With the same explicit UTC
    # `now`, the resolved baseline is identical across host time zones.
    sf = _session_factory()
    with sf() as s:
        s.add(
            DailyNav(
                user_id="user-1",
                account_id=1,
                date=datetime(2026, 6, 27, tzinfo=UTC).date(),
                nav_ccy="EUR",
                nav_value=Decimal("9000.00"),
            )
        )
        s.commit()
    now = datetime(2026, 6, 28, 0, 5, tzinfo=UTC)  # just past UTC midnight
    original_tz = os.environ.get("TZ")
    results = []
    try:
        for tz in ("UTC", "America/New_York", "Asia/Kolkata"):
            os.environ["TZ"] = tz
            time.tzset()
            baseline = resolve_risk_baseline(
                sf,
                user_id="user-1",
                account_id=1,
                current_equity=8_800.0,
                now=now,
            )
            results.append(baseline.day_start_equity)
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()
    assert results == [9000.0, 9000.0, 9000.0]  # host TZ never shifts the UTC boundary


def test_no_prior_day_nav_leaves_cap_inert() -> None:
    # H10 gap: a no-fill day writes no DailyNav, so the next day has no prior-day
    # baseline and day_start_equity falls back to current equity (daily-loss cap
    # inert). Lock that documented fail-open so a future change can't silently
    # make it fail-closed (which would block all trading after an idle day).
    sf = _session_factory()
    with sf() as s:
        # NAV exists only for the SAME day as `now` — nothing strictly before it.
        s.add(
            DailyNav(
                user_id="user-1",
                account_id=1,
                date=datetime(2026, 6, 28, tzinfo=UTC).date(),
                nav_ccy="EUR",
                nav_value=Decimal("9500.00"),
            )
        )
        s.commit()
    baseline = resolve_risk_baseline(
        sf,
        user_id="user-1",
        account_id=1,
        current_equity=8_800.0,
        now=datetime(2026, 6, 28, 9, 0, tzinfo=UTC),
    )
    assert baseline.day_start_equity == 8_800.0  # bootstrap, no prior-day NAV


def test_peak_uses_highest_account_metric_without_resetting_to_current() -> None:
    sf = _session_factory()
    with sf() as s:
        s.add(_metric("m1", peak=Decimal("11000"), created=datetime(2026, 6, 1, tzinfo=UTC)))
        s.add(_metric("m2", peak=Decimal("13000"), created=datetime(2026, 6, 2, tzinfo=UTC)))
        s.add(_metric("m3", peak=Decimal("12000"), created=datetime(2026, 6, 3, tzinfo=UTC)))
        s.commit()

    # Current below the recorded peak -> use the latest recorded peak.
    baseline = resolve_risk_baseline(
        sf,
        user_id="user-1",
        account_id=1,
        current_equity=10_000.0,
    )
    assert baseline.peak_equity == 13000.0
    assert baseline.has_persisted_account_peak is True
    assert baseline.peak_provenance is not None
    assert baseline.peak_provenance.source == "execution_metric"
    assert baseline.peak_provenance.source_id == "m2"

    # Current above the durable peak has zero drawdown, but it cannot silently
    # rewrite the persisted peak before the post-fill metric is committed.
    fresh_high = resolve_risk_baseline(
        sf,
        user_id="user-1",
        account_id=1,
        current_equity=14_000.0,
    )
    assert fresh_high.peak_equity == 13000.0
    assert fresh_high.has_persisted_account_peak is True


def test_baseline_does_not_cross_broker_account_boundary() -> None:
    sf = _session_factory()
    prior_day = datetime.now(tz=UTC).date() - timedelta(days=1)
    with sf() as session:
        session.add(
            LinkedBrokerAccount(
                account_id=2,
                user_id="user-1",
                broker_id=1,
                environment="paper",
                display_name="Second EUR paper",
                base_ccy="EUR",
                status="connected",
                paper_initial_equity=Decimal("50000"),
                paper_initial_cash=Decimal("50000"),
            )
        )
        session.add_all(
            [
                DailyNav(
                    user_id="user-1",
                    account_id=1,
                    date=prior_day,
                    nav_ccy="EUR",
                    nav_value=Decimal("9000.00"),
                ),
                DailyNav(
                    user_id="user-1",
                    account_id=2,
                    date=prior_day,
                    nav_ccy="EUR",
                    nav_value=Decimal("50000.00"),
                ),
                _metric(
                    "account-1",
                    peak=Decimal("11000"),
                    created=datetime(2026, 6, 1, tzinfo=UTC),
                ),
                _metric(
                    "account-2",
                    peak=Decimal("80000"),
                    created=datetime(2026, 6, 2, tzinfo=UTC),
                    account_id=2,
                ),
            ]
        )
        session.commit()

    baseline = resolve_risk_baseline(
        sf,
        user_id="user-1",
        account_id=1,
        current_equity=10_000.0,
    )

    assert baseline.day_start_equity == 9000.0
    assert baseline.peak_equity == 11000.0
    assert baseline.has_persisted_account_peak is True
    assert baseline.peak_provenance is not None
    assert baseline.peak_provenance.source_id == "account-1"


def test_foreign_user_and_account_metrics_do_not_supply_peak_provenance() -> None:
    sf = _session_factory()
    with sf() as session:
        session.add(User(user_id="user-2", email="user-2@example.com", base_ccy="EUR"))
        session.flush()
        session.add_all(
            [
                LinkedBrokerAccount(
                    account_id=2,
                    user_id="user-1",
                    broker_id=1,
                    environment="paper",
                    display_name="Other account",
                    base_ccy="EUR",
                    status="connected",
                    paper_initial_equity=Decimal("20000"),
                    paper_initial_cash=Decimal("20000"),
                ),
                LinkedBrokerAccount(
                    account_id=3,
                    user_id="user-2",
                    broker_id=1,
                    environment="paper",
                    display_name="Other user account",
                    base_ccy="EUR",
                    status="connected",
                    paper_initial_equity=Decimal("30000"),
                    paper_initial_cash=Decimal("30000"),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                _metric(
                    "foreign-account",
                    peak=Decimal("50000"),
                    created=datetime(2026, 6, 1, tzinfo=UTC),
                    account_id=2,
                ),
                _metric(
                    "foreign-user-account",
                    peak=Decimal("90000"),
                    created=datetime(2026, 6, 2, tzinfo=UTC),
                    account_id=3,
                    user_id="user-2",
                ),
            ]
        )
        session.commit()

    baseline = resolve_risk_baseline(
        sf,
        user_id="user-1",
        account_id=1,
        current_equity=10_000.0,
    )

    assert baseline.peak_equity == 10_000.0
    assert baseline.has_persisted_account_peak is False


def test_database_failure_propagates_for_execution_to_fail_closed() -> None:
    def _failing_session_factory():
        raise SQLAlchemyError("risk baseline database unavailable")

    with pytest.raises(SQLAlchemyError, match="risk baseline database unavailable"):
        resolve_risk_baseline(
            _failing_session_factory,
            user_id="user-1",
            account_id=1,
            current_equity=10_000.0,
        )
