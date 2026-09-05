"""Mode selection honors policy + the user's permitted modes (M-2, M-3)."""

from __future__ import annotations

import pytest

from lib_application.db.models import ModePerformance as ModePerformanceRow
from scoring_engine.engine import ScoreEngine
from scoring_engine.models import ModePerformance, ScoreRecord, ScoringUserBinding
from scoring_engine.storage import AppScoreStore
from scoring_engine.storage_memory import InMemoryScoreStore

ASSET = "BTCUSD"
ACCOUNT_ID = 1
STRATEGY_ID = "s1"


def _engine() -> ScoreEngine:
    store = InMemoryScoreStore()
    # spot: safest (low drawdown); futures: best return + Sharpe; options: middle.
    for mode, sharpe, ret, dd in [
        ("spot", 1.0, 0.05, 0.02),
        ("futures", 2.0, 0.20, 0.30),
        ("options_single", 1.5, 0.10, 0.05),
    ]:
        store.upsert_mode_performance(
            ModePerformance(
                asset=ASSET,
                execution_mode=mode,
                horizon="1d",
                sharpe=sharpe,
                total_return=ret,
                max_drawdown=dd,
                account_id=ACCOUNT_ID,
                strategy_id=STRATEGY_ID,
            )
        )
    return ScoreEngine(store)


def test_highest_sharpe_default() -> None:
    assert (
        _engine().select_best_mode(
            ASSET,
            account_id=ACCOUNT_ID,
            strategy_id=STRATEGY_ID,
        )
        == "futures"
    )


def test_best_return_policy() -> None:
    assert (
        _engine().select_best_mode(
            ASSET,
            policy="best_return",
            account_id=ACCOUNT_ID,
            strategy_id=STRATEGY_ID,
        )
        == "futures"
    )


def test_lowest_risk_policy_picks_min_drawdown() -> None:
    assert (
        _engine().select_best_mode(
            ASSET,
            policy="lowest_risk",
            account_id=ACCOUNT_ID,
            strategy_id=STRATEGY_ID,
        )
        == "spot"
    )


def test_allowed_modes_filter_excludes_disallowed() -> None:
    # User permits only spot + options → the high-Sharpe futures mode is excluded.
    chosen = _engine().select_best_mode(
        ASSET,
        allowed_modes=["spot", "options_single"],
        policy="highest_sharpe",
        account_id=ACCOUNT_ID,
        strategy_id=STRATEGY_ID,
    )
    assert chosen == "options_single"


def test_spot_only_user_never_gets_derivatives() -> None:
    chosen = _engine().select_best_mode(
        ASSET,
        allowed_modes=["spot"],
        account_id=ACCOUNT_ID,
        strategy_id=STRATEGY_ID,
    )
    assert chosen == "spot"


def test_no_perf_for_permitted_modes_returns_none() -> None:
    # User permits a mode with no performance data → None (caller uses preferred_mode).
    assert (
        _engine().select_best_mode(
            ASSET,
            allowed_modes=["margin"],
            account_id=ACCOUNT_ID,
            strategy_id=STRATEGY_ID,
        )
        is None
    )


def test_binding_evaluation_selects_account_scoped_best_mode() -> None:
    engine = _engine()
    score = ScoreRecord(target=ASSET, scope="asset", score=0.8, components=[])
    binding = ScoringUserBinding(
        user_id="u1",
        binding_id=1,
        broker_account_id=ACCOUNT_ID,
        strategy_id=STRATEGY_ID,
        asset_score_threshold=0.6,
        execution_mode="best",
        execution_modes_allowed=["spot", "futures"],
        mode_selection_policy="highest_sharpe",
    )

    decision = engine.evaluate_bindings(
        score,
        bindings=[binding],
        signal_strategy_id=STRATEGY_ID,
    )[0]

    assert decision.execution_mode == "futures"
    assert decision.reason == "Score threshold met; selected best mode (futures); direction=long"


def test_binding_evaluation_falls_back_within_permitted_modes() -> None:
    engine = _engine()
    score = ScoreRecord(target=ASSET, scope="asset", score=0.8, components=[])
    binding = ScoringUserBinding(
        user_id="u1",
        binding_id=2,
        broker_account_id=ACCOUNT_ID,
        strategy_id=STRATEGY_ID,
        asset_score_threshold=0.6,
        execution_mode="auto",
        execution_modes_allowed=["margin"],
        preferred_mode="margin",
        mode_selection_policy="highest_sharpe",
    )

    decision = engine.evaluate_bindings(
        score,
        bindings=[binding],
        signal_strategy_id=STRATEGY_ID,
    )[0]

    assert decision.execution_mode == "margin"
    assert (
        decision.reason == "Score threshold met; no mode performance for permitted modes; "
        "using fallback mode=margin; direction=long"
    )


@pytest.mark.parametrize(
    ("instrument_id", "sector_id", "asset_class"),
    [
        (11, None, "crypto"),
        (None, 22, "equity"),
        (None, None, "crypto"),
    ],
)
def test_database_mode_performance_upsert_is_effectively_once(
    instrument_id: int | None,
    sector_id: int | None,
    asset_class: str | None,
) -> None:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    initial = ModePerformance(
        asset=ASSET,
        execution_mode="spot",
        horizon="1d",
        sharpe=1.0,
        total_return=0.1,
        max_drawdown=0.02,
        account_id=ACCOUNT_ID,
        strategy_id=STRATEGY_ID,
        instrument_id=instrument_id,
        sector_id=sector_id,
        asset_class=asset_class,
        sample_size=1,
    )
    updated = ModePerformance(
        **{
            **initial.__dict__,
            "sharpe": 2.0,
            "sample_size": 2,
        }
    )

    store.upsert_mode_performance(initial)
    store.upsert_mode_performance(updated)

    with store.get_session() as session:
        rows = session.query(ModePerformanceRow).all()
        assert len(rows) == 1
        assert float(rows[0].sharpe_ratio) == 2.0
        assert rows[0].sample_size == 2
