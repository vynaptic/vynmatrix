"""Tests for user binding threshold evaluation."""

import logging

from sqlalchemy.orm import Session

from lib_application.db import models as app_models
from lib_application.db.models import Broker, LinkedBrokerAccount
from lib_common.hashing import canonical_json_hash
from lib_common.paper_promotion import (
    PaperPromotionModelContext,
    PaperPromotionScope,
    paper_promotion_instrument_set_sha256,
)
from lib_strategy.signals.signal import SignalAction
from scoring_engine.engine import ScoreEngine
from scoring_engine.models import ScoreRecord, ScoringUserBinding
from scoring_engine.storage import AppScoreStore, InMemoryScoreStore

# A gating score produced by the cross-strategy ensemble (SC-asset flag-on).
_CROSS = {"cross_strategy": {"batch_size": 2, "siblings": ["beta"]}}


def _paper_promotion_scope() -> PaperPromotionScope:
    return PaperPromotionScope(
        user_id="user-1",
        broker_account_id=101,
        strategy_binding_id=201,
        strategy_id="swing_high_low_pmo_v1",
        strategy_version="1.0.1",
        strategy_universe="BTCUSDC",
        model_scope="single_instrument",
        canonical_instrument="BTC-USDC",
        asset_class="crypto",
        broker_code="paper",
        data_use_scope=None,
        model_configuration_sha256=None,
        instrument_set_sha256=canonical_json_hash(
            {
                "schema": "paper-promotion-single-instrument-v1",
                "canonical_instrument": "BTC-USDC",
            }
        ),
        instruments=(),
        scoring_semantics="calibrated_forecast",
        order_evidence_profile="bracket_oco",
    )


def _portfolio_promotion_scope() -> PaperPromotionScope:
    return PaperPromotionScope(
        user_id="user-1",
        broker_account_id=101,
        strategy_binding_id=201,
        strategy_id="us_quality_compounder_v1",
        strategy_version="1.0.0",
        strategy_universe="SP500",
        model_scope="synchronized_portfolio",
        canonical_instrument=None,
        asset_class="equity",
        broker_code="ibkr",
        data_use_scope="paper_forward",
        model_configuration_sha256="a" * 64,
        instrument_set_sha256=paper_promotion_instrument_set_sha256({1: "IBM", 2: "MSFT"}),
        instruments=((1, "IBM"), (2, "MSFT")),
        scoring_semantics="rank_model",
        order_evidence_profile="synchronized_targets",
    )


def test_cross_strategy_direction_conflict_suppresses() -> None:
    # Net ensemble is short (score < 0) but the triggering entry is LONG -> no
    # consensus -> suppressed (never execute the opposite side).
    store = InMemoryScoreStore()
    engine = ScoreEngine(store)
    score = ScoreRecord(target="BTCUSD", scope="asset", score=-0.8, components=[], metadata=_CROSS)
    binding = ScoringUserBinding(user_id="user-1", asset_score_threshold=0.6)

    decisions = engine.evaluate_bindings(score, bindings=[binding], signal_action=SignalAction.LONG)

    assert decisions[0].should_execute is False
    assert "conflicts" in decisions[0].reason


def test_cross_strategy_direction_agreement_executes() -> None:
    # Net ensemble agrees with the entry (both long) -> execute.
    store = InMemoryScoreStore()
    engine = ScoreEngine(store)
    score = ScoreRecord(target="BTCUSD", scope="asset", score=0.8, components=[], metadata=_CROSS)
    binding = ScoringUserBinding(user_id="user-1", asset_score_threshold=0.6)

    decisions = engine.evaluate_bindings(score, bindings=[binding], signal_action=SignalAction.LONG)

    assert decisions[0].should_execute is True


def test_single_strategy_score_skips_direction_conflict_gate() -> None:
    # No cross_strategy metadata (flag-off / single strategy): the conflict gate
    # must NOT fire, preserving the prior magnitude-only behavior.
    store = InMemoryScoreStore()
    engine = ScoreEngine(store)
    score = ScoreRecord(target="BTCUSD", scope="asset", score=-0.8, components=[])
    binding = ScoringUserBinding(user_id="user-1", asset_score_threshold=0.6)

    decisions = engine.evaluate_bindings(score, bindings=[binding], signal_action=SignalAction.LONG)

    assert decisions[0].should_execute is True


def test_binding_threshold_allows_negative_score_when_abs_passes() -> None:
    store = InMemoryScoreStore()
    engine = ScoreEngine(store)

    score = ScoreRecord(target="BTCUSD", scope="asset", score=-0.8, components=[])
    binding = ScoringUserBinding(user_id="user-1", asset_score_threshold=0.6)

    decisions = engine.evaluate_bindings(score, bindings=[binding])

    assert decisions
    assert decisions[0].should_execute is True


def test_binding_threshold_blocks_when_abs_below_threshold() -> None:
    store = InMemoryScoreStore()
    engine = ScoreEngine(store)

    score = ScoreRecord(target="BTCUSD", scope="asset", score=-0.2, components=[])
    binding = ScoringUserBinding(user_id="user-1", asset_score_threshold=0.6)

    decisions = engine.evaluate_bindings(score, bindings=[binding])

    assert decisions
    assert decisions[0].should_execute is False


def test_abstained_score_cannot_execute_even_with_zero_threshold() -> None:
    """Unavailable meta inputs are a hard entry gate, not a numeric zero."""
    engine = ScoreEngine(InMemoryScoreStore())
    score = ScoreRecord(
        target="BTCUSD",
        scope="asset",
        score=0.0,
        components=[],
        metadata={
            "abstained": True,
            "abstain_reason": "meta_inputs_unavailable",
        },
    )
    binding = ScoringUserBinding(
        user_id="user-1",
        asset_score_threshold=0.0,
        autopilot=True,
    )

    decision = engine.evaluate_bindings(
        score,
        bindings=[binding],
        signal_action=SignalAction.LONG,
    )[0]

    assert decision.should_execute is False
    assert decision.reason == "Scoring abstained: meta_inputs_unavailable"


def test_threshold_and_autopilot_reason_contracts() -> None:
    engine = ScoreEngine(InMemoryScoreStore())
    low_score = ScoreRecord(target="BTCUSD", scope="asset", score=-0.2, components=[])
    manual_binding = ScoringUserBinding(
        user_id="user-1",
        binding_id=7,
        broker_account_id=11,
        asset_score_threshold=0.6,
        execution_mode="spot",
        autopilot=False,
    )

    below = engine.evaluate_bindings(low_score, bindings=[manual_binding])
    assert below[0].reason == "Asset score -0.20 (abs 0.20) below threshold 0.6"

    passing_score = ScoreRecord(target="BTCUSD", scope="asset", score=0.8, components=[])
    manual = engine.evaluate_bindings(passing_score, bindings=[manual_binding])
    assert (
        manual[0].reason == "Score threshold met but autopilot=false; requires manual approval. "
        "Score threshold met; direction=long"
    )
    assert manual[0].broker_account_id == 11


def test_strategy_specific_binding_overrides_global_binding_for_same_user() -> None:
    store = InMemoryScoreStore()
    engine = ScoreEngine(store)

    score = ScoreRecord(target="BTCUSD", scope="asset", score=0.8, components=[])
    bindings = [
        ScoringUserBinding(
            user_id="user-1",
            binding_id=1,
            asset_score_threshold=-1.0,
            execution_mode="spot",
            autopilot=False,
        ),
        ScoringUserBinding(
            user_id="user-1",
            binding_id=2,
            strategy_id="swing_high_low_pmo_v1",
            asset_score_threshold=0.3,
            asset_filter=["BTC-USD"],
            execution_mode="spot",
            autopilot=True,
        ),
    ]

    decisions = engine.evaluate_bindings(
        score,
        bindings=bindings,
        signal_strategy_id="swing_high_low_pmo_v1",
    )

    assert len(decisions) == 1
    assert decisions[0].binding_id == 2
    assert decisions[0].should_execute is True


def test_strategy_binding_preference_is_scoped_to_broker_account() -> None:
    engine = ScoreEngine(InMemoryScoreStore())
    score = ScoreRecord(target="BTCUSD", scope="asset", score=0.8, components=[])
    bindings = [
        ScoringUserBinding(
            user_id="user-1",
            binding_id=10,
            broker_account_id=101,
            asset_score_threshold=0.6,
        ),
        ScoringUserBinding(
            user_id="user-1",
            binding_id=11,
            broker_account_id=101,
            strategy_id="swing_high_low_pmo_v1",
            asset_score_threshold=0.6,
        ),
        ScoringUserBinding(
            user_id="user-1",
            binding_id=12,
            broker_account_id=202,
            asset_score_threshold=0.6,
        ),
    ]

    decisions = engine.evaluate_bindings(
        score,
        bindings=bindings,
        signal_strategy_id="swing_high_low_pmo_v1",
    )

    assert {decision.binding_id for decision in decisions} == {11, 12}
    assert {decision.broker_account_id for decision in decisions} == {101, 202}


def test_cloud_paper_promotion_allows_only_exact_binding_route() -> None:
    engine = ScoreEngine(
        InMemoryScoreStore(),
        paper_promotion_scope=_paper_promotion_scope(),
        paper_promotion_required=True,
    )
    score = ScoreRecord(target="BTCUSDC", scope="asset", score=0.8, components=[])
    bindings = [
        ScoringUserBinding(
            user_id="user-1",
            binding_id=201,
            broker_account_id=101,
            strategy_id="swing_high_low_pmo_v1",
            asset_score_threshold=0.6,
            asset_filter=["BTC-USDC"],
            allowed_brokers=["paper"],
        ),
        ScoringUserBinding(
            user_id="user-1",
            binding_id=202,
            broker_account_id=101,
            asset_score_threshold=0.6,
            asset_filter=["BTC-USDC"],
            allowed_brokers=["paper"],
        ),
        ScoringUserBinding(
            user_id="user-1",
            binding_id=203,
            broker_account_id=202,
            strategy_id="swing_high_low_pmo_v1",
            asset_score_threshold=0.6,
            asset_filter=["BTC-USDC"],
            allowed_brokers=["paper"],
        ),
        ScoringUserBinding(
            user_id="user-2",
            binding_id=204,
            broker_account_id=303,
            strategy_id="swing_high_low_pmo_v1",
            asset_score_threshold=0.6,
            asset_filter=["BTC-USDC"],
            allowed_brokers=["paper"],
        ),
    ]

    decisions = engine.evaluate_bindings(
        score,
        bindings=bindings,
        signal_strategy_id="swing_high_low_pmo_v1",
        signal_strategy_version="1.0.1",
        signal_action=SignalAction.LONG,
    )

    assert [decision.binding_id for decision in decisions] == [201]
    assert [decision.broker_account_id for decision in decisions] == [101]


def test_cloud_paper_promotion_rejects_wildcard_or_normalized_duplicate_scope() -> None:
    engine = ScoreEngine(
        InMemoryScoreStore(),
        paper_promotion_scope=_paper_promotion_scope(),
        paper_promotion_required=True,
    )
    score = ScoreRecord(target="BTCUSDC", scope="asset", score=0.8, components=[])
    invalid_scopes = (
        (["*", "BTC-USDC"], ["paper"]),
        (["BTC-USDC", "BTC_USDC"], ["paper"]),
        (["BTC-USDC"], ["paper", "local-paper"]),
        (["BTC-USDC"], ["*", "paper"]),
    )

    for asset_filter, allowed_brokers in invalid_scopes:
        binding = ScoringUserBinding(
            user_id="user-1",
            binding_id=201,
            broker_account_id=101,
            strategy_id="swing_high_low_pmo_v1",
            asset_score_threshold=0.6,
            asset_filter=asset_filter,
            allowed_brokers=allowed_brokers,
        )
        assert (
            engine.evaluate_bindings(
                score,
                bindings=[binding],
                signal_strategy_id="swing_high_low_pmo_v1",
                signal_strategy_version="1.0.1",
                signal_action=SignalAction.LONG,
            )
            == []
        )


def test_cloud_paper_promotion_fails_closed_without_exact_signal_scope() -> None:
    score = ScoreRecord(target="BTCUSDC", scope="asset", score=0.8, components=[])
    binding = ScoringUserBinding(
        user_id="user-1",
        binding_id=201,
        broker_account_id=101,
        strategy_id="swing_high_low_pmo_v1",
        asset_score_threshold=0.6,
        asset_filter=["BTC-USDC"],
        allowed_brokers=["paper"],
    )
    scoped_engine = ScoreEngine(
        InMemoryScoreStore(),
        paper_promotion_scope=_paper_promotion_scope(),
        paper_promotion_required=True,
    )
    missing_authority_engine = ScoreEngine(
        InMemoryScoreStore(),
        paper_promotion_required=True,
    )

    assert (
        scoped_engine.evaluate_bindings(
            score,
            bindings=[binding],
            signal_strategy_id="swing_high_low_pmo_v1",
            signal_strategy_version="1.0.0",
            signal_action=SignalAction.LONG,
        )
        == []
    )
    assert (
        scoped_engine.evaluate_bindings(
            ScoreRecord(target="ETHUSD", scope="asset", score=0.8, components=[]),
            bindings=[binding],
            signal_strategy_id="swing_high_low_pmo_v1",
            signal_strategy_version="1.0.1",
            signal_action=SignalAction.LONG,
        )
        == []
    )
    assert (
        missing_authority_engine.evaluate_bindings(
            score,
            bindings=[binding],
            signal_strategy_id="swing_high_low_pmo_v1",
            signal_strategy_version="1.0.1",
            signal_action=SignalAction.LONG,
        )
        == []
    )


def test_cloud_paper_close_is_limited_to_promoted_binding() -> None:
    engine = ScoreEngine(
        InMemoryScoreStore(),
        paper_promotion_scope=_paper_promotion_scope(),
        paper_promotion_required=True,
    )
    score = ScoreRecord(target="BTC-USDC", scope="asset", score=0.0, components=[])
    bindings = [
        ScoringUserBinding(
            user_id="user-1",
            binding_id=201,
            broker_account_id=101,
            strategy_id="swing_high_low_pmo_v1",
            asset_score_threshold=0.6,
            entries_enabled=False,
            exits_enabled=True,
            asset_filter=["BTC-USDC"],
            allowed_brokers=["paper"],
        ),
        ScoringUserBinding(
            user_id="user-1",
            binding_id=202,
            broker_account_id=202,
            strategy_id="swing_high_low_pmo_v1",
            asset_score_threshold=0.6,
            entries_enabled=False,
            exits_enabled=True,
            asset_filter=["BTC-USDC"],
            allowed_brokers=["paper"],
        ),
    ]

    decisions = engine.evaluate_bindings(
        score,
        bindings=bindings,
        signal_strategy_id="swing_high_low_pmo_v1",
        signal_strategy_version="1.0.1",
        signal_action=SignalAction.CLOSE,
    )

    assert [decision.binding_id for decision in decisions] == [201]
    assert decisions[0].should_execute is True
    assert "close signal" in decisions[0].reason.lower()


def test_portfolio_promotion_rejects_independent_signal_dispatch() -> None:
    scope = _portfolio_promotion_scope()
    engine = ScoreEngine(
        InMemoryScoreStore(),
        paper_promotion_scope=scope,
        paper_promotion_required=True,
    )
    score = ScoreRecord(target="IBM", scope="asset", score=0.8, components=[])
    binding = ScoringUserBinding(
        user_id=scope.user_id,
        binding_id=scope.strategy_binding_id,
        broker_account_id=scope.broker_account_id,
        strategy_id=scope.strategy_id,
        asset_score_threshold=0.0,
        asset_filter=["IBM", "MSFT"],
        allowed_brokers=["ibkr"],
    )

    assert (
        engine.evaluate_bindings(
            score,
            bindings=[binding],
            signal_strategy_id=scope.strategy_id,
            signal_strategy_version=scope.strategy_version,
            signal_action=SignalAction.LONG,
        )
        == []
    )
    decisions = engine.evaluate_bindings(
        score,
        bindings=[binding],
        signal_strategy_id=scope.strategy_id,
        signal_strategy_version=scope.strategy_version,
        signal_action=SignalAction.LONG,
        promotion_model_context=PaperPromotionModelContext(
            asset_class=scope.asset_class,
            data_use_scope=scope.data_use_scope,
            model_configuration_sha256=str(scope.model_configuration_sha256),
            instrument_set_sha256=scope.instrument_set_sha256,
        ),
    )
    assert [decision.binding_id for decision in decisions] == [scope.strategy_binding_id]


def test_development_binding_evaluation_is_unchanged_without_required_gate() -> None:
    engine = ScoreEngine(InMemoryScoreStore())
    score = ScoreRecord(target="BTCUSDC", scope="asset", score=0.8, components=[])
    bindings = [
        ScoringUserBinding(user_id="user-1", binding_id=1, broker_account_id=101),
        ScoringUserBinding(user_id="user-2", binding_id=2, broker_account_id=202),
    ]

    decisions = engine.evaluate_bindings(
        score,
        bindings=bindings,
        signal_strategy_id="swing_high_low_pmo_v1",
        signal_strategy_version="1.0.1",
        signal_action=SignalAction.LONG,
    )

    assert {decision.binding_id for decision in decisions} == {1, 2}


def test_close_signal_bypasses_threshold_for_autopilot_binding() -> None:
    store = InMemoryScoreStore()
    engine = ScoreEngine(store)

    score = ScoreRecord(target="BTCUSD", scope="asset", score=0.0, components=[])
    binding = ScoringUserBinding(
        user_id="user-1",
        strategy_id="swing_high_low_pmo_v1",
        binding_id=3,
        asset_score_threshold=0.3,
        asset_filter=["BTC-USD"],
        execution_mode="spot",
        autopilot=True,
    )

    decisions = engine.evaluate_bindings(
        score,
        bindings=[binding],
        signal_strategy_id="swing_high_low_pmo_v1",
        signal_action="flat",
    )

    assert len(decisions) == 1
    assert decisions[0].binding_id == 3
    assert decisions[0].should_execute is True
    assert "close signal" in decisions[0].reason.lower()


def test_close_is_blocked_only_when_exit_authority_is_disabled() -> None:
    engine = ScoreEngine(InMemoryScoreStore())
    score = ScoreRecord(target="BTCUSD", scope="asset", score=0.0, components=[])
    binding = ScoringUserBinding(
        user_id="user-1",
        binding_id=8,
        broker_account_id=12,
        execution_mode="spot",
        autopilot=False,
        exits_enabled=False,
    )

    decision = engine.evaluate_bindings(score, bindings=[binding], signal_action="flat")[0]

    assert decision.should_execute is False
    assert decision.reason == "Close signal rejected because exit authority is disabled."
    assert decision.broker_account_id == 12


def test_close_only_binding_executes_without_entry_autopilot() -> None:
    engine = ScoreEngine(InMemoryScoreStore())
    score = ScoreRecord(target="BTCUSD", scope="asset", score=0.0, components=[])
    binding = ScoringUserBinding(
        user_id="user-1",
        binding_id=9,
        broker_account_id=12,
        execution_mode="spot",
        autopilot=False,
        entries_enabled=False,
        exits_enabled=True,
    )

    decision = engine.evaluate_bindings(score, bindings=[binding], signal_action="flat")[0]

    assert decision.should_execute is True
    assert decision.reason == "Risk-reducing close signal bypassed score threshold"


def test_close_signal_bypasses_sector_filter_without_sector_score() -> None:
    # BD-1: a risk-reducing close must not be blocked by a configured
    # sector_filter when no sector score is available (indicator signals never
    # set one) — otherwise an open position can never be closed.
    store = InMemoryScoreStore()
    engine = ScoreEngine(store)

    score = ScoreRecord(target="BTCUSD", scope="asset", score=0.0, components=[])
    binding = ScoringUserBinding(
        user_id="user-1",
        strategy_id="swing_high_low_pmo_v1",
        binding_id=4,
        asset_score_threshold=0.3,
        asset_filter=["BTC-USD"],
        sector_filter=["crypto"],  # configured; pre-fix this blocked the close
        execution_mode="spot",
        autopilot=True,
    )

    decisions = engine.evaluate_bindings(
        score,
        bindings=[binding],
        signal_strategy_id="swing_high_low_pmo_v1",
        signal_action="flat",
        # No sector_score passed -> None -> would hit the sector filter pre-fix.
    )

    assert len(decisions) == 1
    assert decisions[0].binding_id == 4
    assert decisions[0].should_execute is True


def test_entry_still_blocked_by_sector_filter_without_sector_score() -> None:
    # The bypass is for closes only; an ENTRY with a sector_filter and no sector
    # score must still be filtered out (no spurious entry).
    store = InMemoryScoreStore()
    engine = ScoreEngine(store)

    score = ScoreRecord(target="BTCUSD", scope="asset", score=0.9, components=[])
    binding = ScoringUserBinding(
        user_id="user-1",
        binding_id=5,
        asset_score_threshold=0.3,
        asset_filter=["BTC-USD"],
        sector_filter=["crypto"],
        execution_mode="spot",
        autopilot=True,
    )

    decisions = engine.evaluate_bindings(score, bindings=[binding], signal_action="long")
    assert decisions == []


def test_inactive_specific_binding_suppresses_wildcard_trading(caplog) -> None:
    # D1: deactivating a strategy-specific binding is an explicit opt-out. The
    # same user's wildcard must not silently re-enable that strategy.
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    with Session(store._engine) as s:
        broker = Broker(code="paper", name="Paper Broker", capabilities={})
        s.add_all(
            [
                app_models.User(user_id="user-1", email="u1@example.com", base_ccy="USD"),
                broker,
            ]
        )
        s.flush()
        s.add_all(
            [
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="user-1",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="User 1 paper primary",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=2,
                    user_id="user-1",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="User 1 paper secondary",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
            ]
        )
        s.flush()
        s.add(
            app_models.UserStrategyBinding(
                binding_id=3,
                user_id="user-1",
                strategy_id="test_strategy_alpha_v1",
                broker_account_id=1,
                is_active=False,
            )
        )
        s.commit()

    engine = ScoreEngine(store)
    score = ScoreRecord(target="BTCUSD", scope="asset", score=0.8, components=[])
    wildcards = [
        ScoringUserBinding(
            user_id="user-1",
            binding_id=1,
            broker_account_id=1,
            asset_score_threshold=0.6,
        ),
        ScoringUserBinding(
            user_id="user-1",
            binding_id=2,
            broker_account_id=2,
            asset_score_threshold=0.6,
        ),
    ]

    with caplog.at_level(logging.WARNING):
        decisions = engine.evaluate_bindings(
            score, bindings=wildcards, signal_strategy_id="test_strategy_alpha_v1"
        )

    assert [decision.binding_id for decision in decisions] == [2]
    assert decisions[0].broker_account_id == 2
    assert "Wildcard binding suppressed" in caplog.text
    assert "user-1" in caplog.text
    assert "test_strategy_alpha_v1" in caplog.text
    assert "[3]" in caplog.text


def test_wildcard_fallback_without_inactive_binding_stays_silent(caplog) -> None:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    engine = ScoreEngine(store)
    score = ScoreRecord(target="BTCUSD", scope="asset", score=0.8, components=[])
    wildcard = ScoringUserBinding(user_id="user-1", binding_id=1, asset_score_threshold=0.6)

    with caplog.at_level(logging.WARNING):
        engine.evaluate_bindings(
            score, bindings=[wildcard], signal_strategy_id="test_strategy_alpha_v1"
        )

    assert "Wildcard binding suppressed" not in caplog.text


def test_in_memory_store_explicitly_reports_no_inactive_bindings(caplog) -> None:
    # The storage contract always exposes the safety lookup. The in-memory
    # store accurately reports none because it retains active test bindings only.
    engine = ScoreEngine(InMemoryScoreStore())
    score = ScoreRecord(target="BTCUSD", scope="asset", score=0.8, components=[])
    wildcard = ScoringUserBinding(user_id="user-1", binding_id=1, asset_score_threshold=0.6)

    with caplog.at_level(logging.WARNING):
        decisions = engine.evaluate_bindings(
            score, bindings=[wildcard], signal_strategy_id="test_strategy_alpha_v1"
        )

    assert decisions[0].should_execute is True
    assert "Wildcard binding suppressed" not in caplog.text


def test_list_inactive_strategy_binding_ids_scopes_to_user_and_strategy() -> None:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    with Session(store._engine) as s:
        broker = Broker(code="paper", name="Paper Broker", capabilities={})
        s.add_all(
            [
                app_models.User(user_id="user-1", email="u1@example.com", base_ccy="USD"),
                app_models.User(user_id="user-2", email="u2@example.com", base_ccy="EUR"),
                broker,
            ]
        )
        s.flush()
        s.add_all(
            [
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="user-1",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="User 1 paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=2,
                    user_id="user-2",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="User 2 paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
            ]
        )
        s.flush()
        s.add(
            app_models.UserStrategyBinding(
                binding_id=3,
                user_id="user-1",
                strategy_id="test_strategy_alpha_v1",
                broker_account_id=1,
                is_active=False,
            )
        )
        # Active binding for the same pair must NOT be reported.
        s.add(
            app_models.UserStrategyBinding(
                binding_id=4,
                user_id="user-1",
                strategy_id="swing_high_low_pmo_v1",
                broker_account_id=1,
                is_active=True,
                autopilot=False,
            )
        )
        # Different user, same strategy: out of scope.
        s.add(
            app_models.UserStrategyBinding(
                binding_id=5,
                user_id="user-2",
                strategy_id="test_strategy_alpha_v1",
                broker_account_id=2,
                is_active=False,
            )
        )
        s.commit()

    assert store.list_inactive_strategy_binding_ids("user-1", "test_strategy_alpha_v1", 1) == [3]
    assert store.list_inactive_strategy_binding_ids("user-1", "swing_high_low_pmo_v1", 1) == []
    assert store.list_inactive_strategy_binding_ids("user-3", "test_strategy_alpha_v1", 1) == []
    assert store.list_inactive_strategy_binding_ids("user-1", "test_strategy_alpha_v1", 2) == []
