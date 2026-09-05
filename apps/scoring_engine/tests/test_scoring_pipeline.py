"""
Unit tests for the scoring pipeline.

Tests the flow from Signal → GlobalScore.
"""

from datetime import UTC, datetime, timedelta

import pytest

from lib_strategy.signals.adapters.scoring import build_scoring_view
from lib_strategy.signals.signal import Signal, SignalAction
from scoring_engine.domain import (
    EnsembleConfig,
    GlobalScore,
    MarketContext,
    MarketRegime,
    MetaLabelOutput,
    ScoreInterpretation,
    compute_global_score,
    logit,
    sigmoid,
)
from scoring_engine.pipeline import (
    PipelineConfig,
    PipelineResult,
    ScoringPipeline,
)


def _make_signal(
    *,
    strategy_id: str,
    symbol: str,
    direction: int,
    expected_return: float,
    predicted_risk: float,
    horizon_days: float,
    strategy_type: str = "indicator",
    confidence: float = 0.8,
    timestamp: datetime | None = None,
) -> Signal:
    if direction > 0:
        action = SignalAction.LONG
    elif direction < 0:
        action = SignalAction.SHORT
    else:
        action = SignalAction.HOLD
    return Signal(
        strategy_id=strategy_id,
        strategy_type=strategy_type,
        symbol=symbol,
        action=action,
        confidence=confidence,
        timestamp=timestamp or datetime.now(tz=UTC),
        expected_return=expected_return,
        predicted_risk=predicted_risk,
        horizon_days=horizon_days,
    )


class TestScoringSignalView:
    """Tests for canonical Signal scoring adapter."""

    def test_create_basic_signal(self):
        """Test creating a basic strategy signal."""
        signal = _make_signal(
            strategy_id="supertrend_mtf_001",
            symbol="BTCUSD",
            direction=1,
            expected_return=0.008,
            predicted_risk=0.02,
            horizon_days=5.0,
        )
        view = build_scoring_view(signal)

        assert view.direction == 1
        assert view.expected_return == 0.008
        assert view.predicted_risk == 0.02
        assert view.horizon_days == 5.0
        assert view.strategy_type == "indicator"
        assert view.metadata["scoring_input_source"] == "explicit"

    def test_sharpe_raw_calculation(self):
        """Test S_raw = d x (mu/sigma) calculation."""
        signal = _make_signal(
            strategy_id="test_strategy",
            symbol="SPY",
            direction=1,
            expected_return=0.008,  # 0.8%
            predicted_risk=0.02,  # 2%
            horizon_days=5.0,
        )

        view = build_scoring_view(signal)
        # S_raw = 1 x (0.008 / 0.02) = 0.4
        assert abs(view.sharpe_raw - 0.4) < 0.001

    def test_sharpe_raw_short_signal(self):
        """Test S_raw for short signal."""
        signal = _make_signal(
            strategy_id="test_strategy",
            symbol="SPY",
            direction=-1,
            expected_return=0.01,
            predicted_risk=0.02,
            horizon_days=5.0,
        )

        view = build_scoring_view(signal)
        # S_raw = -1 x (0.01 / 0.02) = -0.5
        assert abs(view.sharpe_raw - (-0.5)) < 0.001

    @pytest.mark.parametrize("invalid_type", ["bad_type", "ml", "rl", "agentic"])
    def test_invalid_strategy_type_raises(self, invalid_type: str):
        """Only the implemented indicator strategy type is accepted."""
        with pytest.raises(ValueError, match="Invalid strategy_type"):
            Signal(
                strategy_id="test",
                strategy_type=invalid_type,
                symbol="SPY",
                action=SignalAction.LONG,
                confidence=0.5,
            )

    def test_compact_week_horizon(self):
        """Test converting an explicit compact week horizon."""
        signal = Signal(
            strategy_id="weekly_strat",
            strategy_type="indicator",
            symbol="BTCUSD",
            action=SignalAction.LONG,
            confidence=0.8,
            horizon="1W",
            entry_price=45000,
            stop_loss=44000,
            take_profit=48000,
            timestamp=datetime.now(tz=UTC),
        )
        view = build_scoring_view(signal)

        assert view.direction == 1
        assert view.horizon_days == 5.0  # 1W = 5 days
        assert view.expected_return > 0
        assert view.predicted_risk > 0
        assert view.metadata["scoring_input_source"] == "price_ladder"

    def test_compact_multi_day_horizon_is_not_silently_collapsed(self):
        signal = Signal(
            strategy_id="test_strategy_v1",
            strategy_type="indicator",
            symbol="BTCUSDC",
            action=SignalAction.LONG,
            confidence=0.8,
            horizon="5d",
            timestamp=datetime.now(tz=UTC),
        )

        assert build_scoring_view(signal).horizon_days == 5.0


class TestMetaLabelOutput:
    """Tests for Layer 2 MetaLabelOutput."""

    def test_compute_meta_label(self):
        """Test meta-label computation with worked example."""
        # Given: direction=1, mu=0.008, sigma=0.02, p=0.7, age=1 day, H=5 days
        output = MetaLabelOutput.compute(
            signal_id="sig_001",
            strategy_id="strat_001",
            asset="BTCUSD",
            direction=1,
            expected_return=0.008,
            predicted_risk=0.02,
            horizon_days=5.0,
            signal_timestamp=datetime.now(tz=UTC) - timedelta(days=1),
            meta_probability=0.7,
        )

        # s_raw = 1 x (0.008 / 0.02) = 0.4
        assert abs(output.s_raw - 0.4) < 0.01

        # tilt = 2 x 0.7 - 1 = 0.4
        assert abs(output.tilt - 0.4) < 0.01

        # w_age = max(0, 1 - 1/5) = 0.8
        assert abs(output.w_age - 0.8) < 0.01

        # score_local = 0.4 x 0.4 x 0.8 = 0.128
        assert abs(output.score_local - 0.128) < 0.01

    def test_age_decay(self):
        """Test age decay formula: w_age = max(0, 1 - age/H)."""
        # Signal just created (age ≈ 0)
        output_fresh = MetaLabelOutput.compute(
            signal_id="sig_001",
            strategy_id="strat_001",
            asset="BTCUSD",
            direction=1,
            expected_return=0.008,
            predicted_risk=0.02,
            horizon_days=5.0,
            signal_timestamp=datetime.now(tz=UTC),
            meta_probability=0.7,
        )
        assert output_fresh.w_age > 0.99  # Nearly 1.0

        # Signal at half horizon
        output_mid = MetaLabelOutput.compute(
            signal_id="sig_002",
            strategy_id="strat_001",
            asset="BTCUSD",
            direction=1,
            expected_return=0.008,
            predicted_risk=0.02,
            horizon_days=5.0,
            signal_timestamp=datetime.now(tz=UTC) - timedelta(days=2.5),
            meta_probability=0.7,
        )
        assert abs(output_mid.w_age - 0.5) < 0.01

        # Expired signal (age > horizon)
        output_expired = MetaLabelOutput.compute(
            signal_id="sig_003",
            strategy_id="strat_001",
            asset="BTCUSD",
            direction=1,
            expected_return=0.008,
            predicted_risk=0.02,
            horizon_days=5.0,
            signal_timestamp=datetime.now(tz=UTC) - timedelta(days=6),
            meta_probability=0.7,
        )
        assert output_expired.w_age == 0.0
        assert output_expired.is_expired

    def test_probability_tilt(self):
        """Test tilt = 2*p - 1 transformation."""
        # p = 0.5 → tilt = 0 (neutral)
        output_neutral = MetaLabelOutput(
            signal_id="s1",
            strategy_id="st1",
            asset="X",
            s_raw=1.0,
            meta_probability=0.5,
            age_days=0,
            horizon_days=5.0,
        )
        assert abs(output_neutral.tilt) < 0.001

        # p = 1.0 → tilt = 1 (max positive)
        output_high = MetaLabelOutput(
            signal_id="s2",
            strategy_id="st1",
            asset="X",
            s_raw=1.0,
            meta_probability=1.0,
            age_days=0,
            horizon_days=5.0,
        )
        assert abs(output_high.tilt - 1.0) < 0.001

        # p = 0.0 → tilt = -1 (max negative)
        # Note: 0.0 is edge case, use small epsilon
        output_low = MetaLabelOutput(
            signal_id="s3",
            strategy_id="st1",
            asset="X",
            s_raw=1.0,
            meta_probability=0.01,
            age_days=0,
            horizon_days=5.0,
        )
        assert output_low.tilt < -0.95


class TestGlobalScore:
    """Tests for Layer 3 GlobalScore."""

    def test_compute_global_score_basic(self):
        """Test basic global score computation."""
        meta_outputs = [
            MetaLabelOutput(
                signal_id="s1",
                strategy_id="strat_1",
                asset="BTCUSD",
                s_raw=0.4,
                meta_probability=0.7,
                age_days=0.5,
                horizon_days=5.0,
            ),
            MetaLabelOutput(
                signal_id="s2",
                strategy_id="strat_2",
                asset="BTCUSD",
                s_raw=0.3,
                meta_probability=0.65,
                age_days=1.0,
                horizon_days=5.0,
            ),
        ]

        weights = {"strat_1": 0.6, "strat_2": 0.4}
        config = EnsembleConfig(tau_min=0.1, p_min=0.52)

        score = compute_global_score(
            meta_outputs=meta_outputs,
            strategy_weights=weights,
            config=config,
        )

        assert score.asset == "BTCUSD"
        assert score.strategy_count == 2
        assert score.alpha_core != 0.0
        assert 0 < score.p_agg < 1

    def test_score_interpretation(self):
        """Test score interpretation categories."""
        # Create score with known value
        score = GlobalScore(
            asset="TEST",
            alpha_core=0.0,
            alpha_raw=0.0,
            score=1.8,  # Strong long
            p_agg=0.75,
        )
        assert score.interpretation == ScoreInterpretation.STRONG_LONG

        score2 = GlobalScore(
            asset="TEST",
            alpha_core=0.0,
            alpha_raw=0.0,
            score=-0.8,  # Moderate short
            p_agg=0.35,
        )
        assert score2.interpretation == ScoreInterpretation.MODERATE_SHORT


class TestScoringPipeline:
    """Tests for integrated ScoringPipeline."""

    def test_end_to_end_pipeline(self):
        """Test complete signal → decision flow."""
        pipeline = ScoringPipeline(
            config=PipelineConfig(
                tau_min=0.2,
                p_min=0.52,
            )
        )
        pipeline.set_market_context(
            "BTCUSD",
            MarketContext(
                regime=MarketRegime.BULL_QUIET,
                as_of=datetime.now(tz=UTC),
                realized_vol_20d=0.01,
            ),
        )

        # Create test signals
        signals = [
            _make_signal(
                strategy_id="momentum_001",
                symbol="BTCUSD",
                direction=1,
                expected_return=0.012,
                predicted_risk=0.025,
                horizon_days=5.0,
            ),
            _make_signal(
                strategy_id="mean_reversion_001",
                symbol="BTCUSD",
                direction=1,
                expected_return=0.008,
                predicted_risk=0.02,
                horizon_days=5.0,
            ),
        ]

        # Process through pipeline
        result = pipeline.process_signals(signals)

        # Verify result structure
        assert isinstance(result, PipelineResult)
        assert result.asset == "BTCUSD"
        assert len(result.signals) == 2
        assert len(result.meta_outputs) == 2
        assert isinstance(result.global_score, GlobalScore)

        # Verify score computed
        assert result.global_score.strategy_count == 2

    def test_pipeline_with_market_context(self):
        """Test pipeline with custom market context."""
        pipeline = ScoringPipeline()

        # Set market context
        context = MarketContext(
            regime=MarketRegime.BULL_VOLATILE,
            as_of=datetime.now(tz=UTC),
            vix_level=25.0,
            vix_percentile=75.0,
        )
        pipeline.set_market_context("BTCUSD", context)

        signals = [
            _make_signal(
                strategy_id="strat_1",
                symbol="BTCUSD",
                direction=1,
                expected_return=0.01,
                predicted_risk=0.02,
                horizon_days=5.0,
            ),
        ]

        result = pipeline.process_signals(signals)

        # Context should be used in meta-labeling
        assert len(result.meta_outputs) == 1
        assert result.meta_outputs[0].market_context is not None

    def test_missing_observed_context_is_non_actionable(self):
        """No default context may produce an actionable scoring output."""
        pipeline = ScoringPipeline()
        signal = _make_signal(
            strategy_id="strat_1",
            symbol="BTCUSD",
            direction=1,
            expected_return=0.01,
            predicted_risk=0.02,
            horizon_days=5.0,
        )

        result = pipeline.process_signals([signal])

        assert result.meta_outputs == []
        assert result.global_score.score == 0.0
        assert result.global_score.p_agg == 0.5
        assert result.metadata["abstained"] is True
        assert result.metadata["abstain_reason"] == "meta_inputs_unavailable"


class TestPipelineMixedSymbolGuard:
    """Tests for mixed-symbol validation guard in process_signals."""

    def test_mixed_symbols_logs_warning(self, caplog):
        """Test that mixed symbols in process_signals logs a warning."""
        import logging

        pipeline = ScoringPipeline()

        signals = [
            _make_signal(
                strategy_id="strat_1",
                symbol="BTCUSD",
                direction=1,
                expected_return=0.01,
                predicted_risk=0.02,
                horizon_days=5.0,
            ),
            _make_signal(
                strategy_id="strat_2",
                symbol="ETHUSD",  # Different symbol
                direction=1,
                expected_return=0.008,
                predicted_risk=0.02,
                horizon_days=5.0,
            ),
        ]

        with caplog.at_level(logging.WARNING):
            result = pipeline.process_signals(signals)

        # Should use first signal's symbol
        assert result.asset == "BTCUSD"
        # Should log warning about mixed symbols
        assert any("mixed symbols" in record.message for record in caplog.records)

    def test_single_symbol_no_warning(self, caplog):
        """Test that single symbol signals don't log warning."""
        import logging

        pipeline = ScoringPipeline()

        signals = [
            _make_signal(
                strategy_id="strat_1",
                symbol="BTCUSD",
                direction=1,
                expected_return=0.01,
                predicted_risk=0.02,
                horizon_days=5.0,
            ),
            _make_signal(
                strategy_id="strat_2",
                symbol="BTCUSD",  # Same symbol
                direction=1,
                expected_return=0.008,
                predicted_risk=0.02,
                horizon_days=5.0,
            ),
        ]

        with caplog.at_level(logging.WARNING):
            result = pipeline.process_signals(signals)

        assert result.asset == "BTCUSD"
        # Should NOT log warning
        assert not any("mixed symbols" in record.message for record in caplog.records)


class TestMathematicalFunctions:
    """Tests for mathematical helper functions."""

    def test_logit_sigmoid_inverse(self):
        """Test that sigmoid(logit(p)) ≈ p."""
        for p in [0.1, 0.3, 0.5, 0.7, 0.9]:
            assert abs(sigmoid(logit(p)) - p) < 0.001

    def test_logit_values(self):
        """Test logit at known values."""
        # logit(0.5) = 0
        assert abs(logit(0.5)) < 0.001

        # logit(0.73...) ≈ 1 (e/(1+e) ≈ 0.7311)
        assert abs(logit(0.7311) - 1.0) < 0.01

    def test_sigmoid_values(self):
        """Test sigmoid at known values."""
        # sigmoid(0) = 0.5
        assert abs(sigmoid(0) - 0.5) < 0.001

        # sigmoid(large) → 1
        assert sigmoid(10) > 0.999

        # sigmoid(-large) → 0
        assert sigmoid(-10) < 0.001
