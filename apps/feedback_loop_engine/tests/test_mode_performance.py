"""FB-5: feedback populates mode_performance; the scorer ranks modes off it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from feedback_loop_engine.mode_performance import (
    ModePerformanceIntegrityError,
    ModePerformanceWriter,
)
from lib_application.db.models import (
    Base,
    Broker,
    CanonicalSignal,
    ExecutionMetric,
    Instrument,
    LinkedBrokerAccount,
    ModePerformance,
    SignalPerformance,
    Strategy,
    User,
)

TS = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)


def _sp(
    *,
    perf_id: int,
    instr_id: int,
    run_id: str,
    pnl: float,
    horizon: str = "1d",
    signal_id: int | None = None,
) -> SignalPerformance:
    return SignalPerformance(
        perf_id=perf_id,
        signal_id=signal_id if signal_id is not None else perf_id,
        strategy_id="s1",
        instr_id=instr_id,
        predicted_direction="long",
        confidence=1.0,
        signal_ts=TS,
        entry_price=100.0,
        evaluation_horizon=horizon,
        evaluation_ts=TS,
        exit_price=100.0 * (1 + pnl),
        actual_direction="long",
        price_change_pct=pnl,
        is_correct=pnl > 0,
        pnl_pct=pnl,
        consecutive_wrong_count=0,
        needs_optimization=False,
        run_id=run_id,
        did_execute=True,
        evaluated_at=TS,
    )


def _metric(
    *,
    run_id: str,
    mode: str,
    turnover: float,
    trade_pnl: float,
    last_trade_turnover: float = 100,
    orders_filled: int = 1,
    metric_id: str | None = None,
    signal_id: str | None = None,
    user_id: str = "u1",
    account_id: int = 1,
    symbol: str = "BTCUSD",
    created_at: datetime | None = None,
    entry_signal_id: int | None = None,
) -> ExecutionMetric:
    runtime_signal_id = signal_id or f"signal-{run_id}"
    numeric_suffix = run_id.removeprefix("r")
    default_offset = int(numeric_suffix) if numeric_suffix.isdigit() else 0
    attributed_signal_id = entry_signal_id or max(default_offset, 1)
    return ExecutionMetric(
        metric_id=metric_id or f"metric-{run_id}",
        user_id=user_id,
        account_id=account_id,
        strategy_id="s1",
        symbol=symbol,
        execution_mode=mode,
        broker="paper",
        signal_id=runtime_signal_id,
        run_id=run_id,
        orders_submitted=1,
        orders_filled=orders_filled,
        total_commission=0,
        metadata_json={
            "position_state": {
                "turnover": turnover,
                "last_trade_turnover": last_trade_turnover,
                "last_trade_pnl": trade_pnl,
            },
            "realized_pnl_contributions": [
                {
                    "entry_exec_id": attributed_signal_id * 10 + 1,
                    "exit_exec_id": attributed_signal_id * 10 + 2,
                    "entry_canonical_signal_id": attributed_signal_id,
                    "exit_canonical_signal_id": attributed_signal_id + 10_000,
                    "quantity": "1",
                    "realized_pnl": str(trade_pnl),
                    "deployed_capital": str(last_trade_turnover),
                    "account_currency": "USD",
                    "exit_time": (created_at or TS).isoformat(),
                }
            ],
        },
        created_at=created_at or TS + timedelta(seconds=default_offset),
    )


def _seed(session: Session) -> None:
    session.add_all(
        [
            User(user_id="u1", email="u1@example.com", base_ccy="USD"),
            Broker(broker_id=1, code="paper", name="Paper"),
            Strategy(strategy_id="s1", strategy_name="S1", asset_class="crypto"),
            LinkedBrokerAccount(
                account_id=1,
                user_id="u1",
                broker_id=1,
                environment="paper",
                display_name="U1 paper",
                base_ccy="USD",
                paper_initial_equity=100_000,
                paper_initial_cash=100_000,
                status="connected",
            ),
        ]
    )
    # spot: +1%, -0.5%; futures: +5%, +3%. Returns come from
    # actual realised P&L / incremental turnover, not signal pnl_pct.
    session.add_all(
        [
            _sp(perf_id=1, instr_id=1, run_id="r1", pnl=0.01),
            _sp(perf_id=2, instr_id=1, run_id="r2", pnl=-0.005),
            _sp(perf_id=3, instr_id=1, run_id="r3", pnl=0.05),
            _sp(perf_id=4, instr_id=1, run_id="r4", pnl=0.03),
            _metric(run_id="r1", mode="spot", turnover=100, trade_pnl=1),
            _metric(run_id="r2", mode="spot", turnover=200, trade_pnl=-0.5),
            _metric(run_id="r3", mode="futures", turnover=100, trade_pnl=5),
            _metric(run_id="r4", mode="futures", turnover=200, trade_pnl=3),
        ]
    )
    session.commit()


def test_writer_aggregates_by_instrument_mode_horizon() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _seed(s)

    written = ModePerformanceWriter(engine).update(now=TS)
    assert written == 2  # (1, spot, intraday) + (1, futures, intraday)

    with Session(engine) as s:
        rows = {r.execution_mode: r for r in s.query(ModePerformance).all()}
        assert set(rows) == {"spot", "futures"}
        assert all(r.horizon == "intraday" for r in rows.values())  # "1d" -> intraday
        assert rows["spot"].sample_size == 2
        assert rows["futures"].sample_size == 2
        # futures (two wins 5% + 3%) outperforms spot (1% win, 0.5% loss).
        assert float(rows["futures"].total_return) > float(rows["spot"].total_return)
        assert float(rows["futures"].win_rate) == 1.0
        assert float(rows["spot"].win_rate) == 0.5


def test_only_executed_signals_are_aggregated() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        # A signal with no filled execution is excluded.
        s.add_all(
            [
                User(user_id="u1", email="u1@example.com", base_ccy="USD"),
                Broker(broker_id=1, code="paper", name="Paper"),
                Strategy(strategy_id="s1", strategy_name="S1", asset_class="crypto"),
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="u1",
                    broker_id=1,
                    environment="paper",
                    display_name="U1 paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
            ]
        )
        s.add_all(
            [
                _sp(perf_id=1, instr_id=1, run_id="r1", pnl=0.02),
                _metric(
                    run_id="r1",
                    mode="spot",
                    turnover=100,
                    trade_pnl=2,
                    orders_filled=0,
                ),
            ]
        )
        s.commit()
    assert ModePerformanceWriter(engine).update(now=TS) == 0


def test_writer_does_not_relabel_legacy_snapshot_pnl_as_entry_performance() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                User(user_id="u1", email="u1@example.com", base_ccy="USD"),
                Broker(broker_id=1, code="paper", name="Paper"),
                Strategy(strategy_id="s1", strategy_name="S1", asset_class="crypto"),
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="u1",
                    broker_id=1,
                    environment="paper",
                    display_name="U1 paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                _sp(perf_id=1, instr_id=1, run_id="entry-run", pnl=0.25),
            ]
        )
        metric = _metric(run_id="close-run", mode="spot", turnover=100, trade_pnl=5)
        metric.metadata_json.pop("realized_pnl_contributions")
        session.add(metric)
        session.commit()

    assert ModePerformanceWriter(engine).update(now=TS) == 0


def test_writer_fails_closed_on_malformed_realized_lineage() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                User(user_id="u1", email="u1@example.com", base_ccy="USD"),
                Broker(broker_id=1, code="paper", name="Paper"),
                Strategy(strategy_id="s1", strategy_name="S1", asset_class="crypto"),
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="u1",
                    broker_id=1,
                    environment="paper",
                    display_name="U1 paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                _sp(perf_id=1, instr_id=1, run_id="entry-run", pnl=0.25),
            ]
        )
        metric = _metric(run_id="close-run", mode="spot", turnover=100, trade_pnl=5)
        metric.metadata_json["realized_pnl_contributions"][0]["deployed_capital"] = "0"
        session.add(metric)
        session.commit()

    with pytest.raises(
        ModePerformanceIntegrityError,
        match="non-positive deployed_capital",
    ):
        ModePerformanceWriter(engine).update(now=TS)


def test_writer_uses_incremental_turnover_at_lookback_boundary() -> None:
    """The first in-window snapshot must not deploy all pre-window turnover."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                User(user_id="u1", email="u1@example.com", base_ccy="USD"),
                Broker(broker_id=1, code="paper", name="Paper"),
                Strategy(strategy_id="s1", strategy_name="S1", asset_class="crypto"),
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="u1",
                    broker_id=1,
                    environment="paper",
                    display_name="U1 paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                _sp(perf_id=10, instr_id=1, run_id="r10", pnl=0.99),
                _metric(
                    run_id="r10",
                    mode="spot",
                    turnover=1_100,
                    last_trade_turnover=100,
                    trade_pnl=5,
                ),
            ]
        )
        session.commit()

    assert ModePerformanceWriter(engine).update(lookback_days=1, now=TS) == 1
    with Session(engine) as session:
        row = session.query(ModePerformance).one()
        assert float(row.total_return) == 0.05


def test_writer_counts_one_trade_once_per_normalized_horizon_bucket() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                User(user_id="u1", email="u1@example.com", base_ccy="USD"),
                Broker(broker_id=1, code="paper", name="Paper"),
                Strategy(strategy_id="s1", strategy_name="S1", asset_class="crypto"),
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="u1",
                    broker_id=1,
                    environment="paper",
                    display_name="U1 paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                Instrument(
                    instr_id=1,
                    asset_class="crypto",
                    canonical="BTCUSD",
                    settlement_currency="USD",
                ),
                CanonicalSignal(
                    signal_id=101,
                    strategy_id="s1",
                    instr_id=1,
                    action="long",
                    run_id="shared-horizon-run",
                    external_signal_id="external-horizon-signal",
                    ts=TS,
                ),
                _sp(
                    perf_id=1011,
                    signal_id=101,
                    instr_id=1,
                    run_id="shared-horizon-run",
                    pnl=0.01,
                    horizon="15min",
                ),
                _sp(
                    perf_id=1012,
                    signal_id=101,
                    instr_id=1,
                    run_id="shared-horizon-run",
                    pnl=0.02,
                    horizon="1h",
                ),
                _sp(
                    perf_id=1013,
                    signal_id=101,
                    instr_id=1,
                    run_id="shared-horizon-run",
                    pnl=0.03,
                    horizon="2w",
                ),
                _metric(
                    metric_id="metric-shared-horizon",
                    signal_id="runtime-horizon-signal",
                    run_id="shared-horizon-run",
                    mode="spot",
                    turnover=100,
                    trade_pnl=2,
                    entry_signal_id=101,
                ),
            ]
        )
        session.commit()

    assert ModePerformanceWriter(engine).update(now=TS) == 2
    with Session(engine) as session:
        rows = {row.horizon: row for row in session.query(ModePerformance).all()}
        assert set(rows) == {"intraday", "long_term"}
        assert rows["intraday"].sample_size == 1
        assert rows["long_term"].sample_size == 1
        assert float(rows["intraday"].total_return) == 0.02
        assert float(rows["long_term"].total_return) == 0.02


def test_writer_uses_canonical_signal_and_account_for_shared_run() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                User(user_id="u1", email="u1@example.com", base_ccy="USD"),
                Broker(broker_id=1, code="paper", name="Paper"),
                Strategy(strategy_id="s1", strategy_name="S1", asset_class="crypto"),
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="u1",
                    broker_id=1,
                    environment="paper",
                    display_name="U1 first paper account",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=2,
                    user_id="u1",
                    broker_id=1,
                    environment="paper",
                    display_name="U1 second paper account",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                Instrument(
                    instr_id=1,
                    asset_class="crypto",
                    canonical="BTCUSD",
                    settlement_currency="USD",
                ),
                Instrument(
                    instr_id=2,
                    asset_class="crypto",
                    canonical="ETHUSD",
                    settlement_currency="USD",
                ),
                CanonicalSignal(
                    signal_id=201,
                    strategy_id="s1",
                    instr_id=1,
                    action="long",
                    run_id="shared-run",
                    external_signal_id="external-btc-signal",
                    ts=TS,
                ),
                CanonicalSignal(
                    signal_id=202,
                    strategy_id="s1",
                    instr_id=2,
                    action="long",
                    run_id="shared-run",
                    external_signal_id="external-eth-signal",
                    ts=TS,
                ),
                _sp(
                    perf_id=2011,
                    signal_id=201,
                    instr_id=1,
                    run_id="shared-run",
                    pnl=0.01,
                ),
                _sp(
                    perf_id=2021,
                    signal_id=202,
                    instr_id=2,
                    run_id="shared-run",
                    pnl=0.05,
                ),
                _metric(
                    metric_id="metric-btc-account-1",
                    signal_id="runtime-btc-signal",
                    run_id="shared-run",
                    mode="spot",
                    turnover=100,
                    trade_pnl=1,
                    account_id=1,
                    symbol="BTCUSD",
                    entry_signal_id=201,
                ),
                _metric(
                    metric_id="metric-eth-account-2",
                    signal_id="runtime-eth-signal",
                    run_id="shared-run",
                    mode="spot",
                    turnover=100,
                    trade_pnl=5,
                    account_id=2,
                    symbol="ETHUSD",
                    entry_signal_id=202,
                ),
            ]
        )
        session.commit()

    assert ModePerformanceWriter(engine).update(now=TS) == 2
    with Session(engine) as session:
        rows = {
            (int(row.account_id), int(row.instr_id)): row
            for row in session.query(ModePerformance).all()
        }
        assert set(rows) == {(1, 1), (2, 2)}
        assert float(rows[(1, 1)].total_return) == 0.01
        assert float(rows[(2, 2)].total_return) == 0.05
        assert all(row.sample_size == 1 for row in rows.values())


def test_writer_feeds_scorer_best_mode(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # End-to-end: feedback writes mode_performance, the scorer reads it back and
    # ranks the best mode — the read/write horizon buckets must agree.
    from scoring_engine.engine import ScoreEngine
    from scoring_engine.storage import AppScoreStore

    db_url = f"sqlite:///{tmp_path}/fb5.db"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(
            Instrument(
                instr_id=1,
                asset_class="crypto",
                canonical="BTCUSD",
                settlement_currency="USD",
            )
        )
        _seed(s)

    assert ModePerformanceWriter(engine).update(now=TS) == 2
    # Flush + release the writer's connections so the scorer's separate engine
    # reads a fully-committed file db.
    engine.dispose()

    scorer = ScoreEngine(AppScoreStore(db_url))
    best = scorer.select_best_mode(
        "BTCUSD",
        allowed_modes=["spot", "futures"],
        policy="best_return",
        account_id=1,
        strategy_id="s1",
    )
    assert best == "futures"


def test_degenerate_dispersion_yields_null_ratios() -> None:
    """Two nearly identical fills one bar apart produced sd≈5e-13 and a
    Sharpe of ~1.1e10, overflowing Numeric(10,4) in production. A ratio with
    degenerate dispersion is statistically undefined and must persist NULL,
    never numerical noise."""
    from feedback_loop_engine.mode_performance import _bounded_ratio

    assert _bounded_ratio(0.0056, 0.0) is None
    assert _bounded_ratio(0.0056, 5e-13) is None
    assert _bounded_ratio(0.01, 0.02) == pytest.approx(0.5)
