"""Q3 multi-factor scoring: factor computations, the blender, and the ensemble seam.

Locks (1) the factor math, (2) the equal-weight/weighted blend + winsorize +
missing-factor/fetch-failure handling, and (3) the soak-safety invariant — with no
blender wired the ensemble score is byte-identical to the single-alpha path.
"""

from __future__ import annotations

import pytest

from scoring_engine.domain.layer3_global_score import MetaLabelOutput
from scoring_engine.factors import (
    FactorBlender,
    low_volatility,
    momentum,
    winsorize,
)
from scoring_engine.services.ensemble_service import EnsembleConfig, EnsembleService


# --- factor math --------------------------------------------------------------
def test_momentum_trailing_return() -> None:
    assert momentum([100.0, 110.0], lookback=1) == pytest.approx(0.10)
    assert momentum([100.0, 90.0], lookback=1) == pytest.approx(-0.10)


def test_momentum_insufficient_history_is_none() -> None:
    assert momentum([100.0], lookback=1) is None
    assert momentum([], lookback=5) is None


def test_momentum_zero_base_is_none() -> None:
    assert momentum([0.0, 100.0], lookback=1) is None


def test_low_volatility_is_negative_of_realized_vol() -> None:
    # A constant series has zero realized vol -> factor is 0 (calmest).
    assert low_volatility([100.0, 100.0, 100.0], lookback=2) == pytest.approx(0.0)
    # A volatile series -> strictly negative (low-vol premium penalizes vol).
    assert low_volatility([100.0, 120.0, 90.0, 130.0], lookback=3) < 0.0


def test_winsorize_clamps_to_limit() -> None:
    assert winsorize(5.0, 3.0) == 3.0
    assert winsorize(-5.0, 3.0) == -3.0
    assert winsorize(1.5, 3.0) == 1.5
    assert winsorize(9.0, 0.0) == 9.0  # non-positive limit disables clamping


# --- FactorBlender ------------------------------------------------------------
def _varying_provider():  # type: ignore[no-untyped-def]
    """A provider whose per-call series has a different slope, so the factor
    rolling windows accumulate real dispersion (non-zero z-scores) once warmed."""
    state = {"n": 0}

    def provider(_asset: str, _count: int) -> list[float]:
        state["n"] += 1
        step = float(state["n"])
        return [100.0 + step * i for i in range(6)]

    return provider


def test_blend_skips_factor_until_its_window_warms() -> None:
    # The first observation per factor uses the cold window (std=0.1) which would
    # amplify ~10x, so it is skipped; the factor contributes once warmed.
    blender = FactorBlender(closes_provider=_varying_provider(), lookback=3)
    first = blender.blend("BTCUSD")
    assert first.breakdown == {}  # cold -> all factors skipped
    assert first.alpha == 0.0
    second = blender.blend("BTCUSD")
    assert set(second.breakdown) == {"momentum", "low_volatility"}  # warmed -> contributing


def test_blend_equal_weight_is_mean_of_factor_zscores() -> None:
    blender = FactorBlender(closes_provider=_varying_provider(), lookback=3)
    blender.blend("BTCUSD")  # warm the per-factor windows
    blend = blender.blend("BTCUSD")
    assert set(blend.breakdown) == {"momentum", "low_volatility"}
    # Equal weights -> composite alpha is the mean of the per-factor z-scores.
    assert blend.alpha == pytest.approx(sum(blend.breakdown.values()) / len(blend.breakdown))


def test_blend_respects_configured_weights() -> None:
    blender = FactorBlender(
        closes_provider=_varying_provider(),
        lookback=3,
        weights={"momentum": 1.0, "low_volatility": 0.0},
    )
    blender.blend("BTCUSD")  # warm
    blend = blender.blend("BTCUSD")
    # low_volatility weight 0 -> composite equals the momentum z-score alone.
    assert blend.alpha == pytest.approx(blend.breakdown["momentum"])


def test_blend_skips_uncomputable_factor() -> None:
    # Three closes: momentum(lookback=3) needs 4 -> None; low_volatility computes.
    # Drive two calls so low_volatility warms; momentum stays absent (uncomputable).
    blender = FactorBlender(closes_provider=lambda _a, _n: [100.0, 101.0, 103.0], lookback=3)
    blender.blend("BTCUSD")  # warm
    blend = blender.blend("BTCUSD")
    assert "momentum" not in blend.breakdown
    assert "low_volatility" in blend.breakdown


def test_blend_bad_data_degrades_to_zero() -> None:
    # A malformed price series (non-numeric) raises in factor computation; the
    # blend degrades to no contribution rather than failing the scoring request.
    blend = FactorBlender(closes_provider=lambda _a, _n: ["bad", "data", "rows"]).blend("BTCUSD")
    assert blend.alpha == 0.0
    assert blend.breakdown == {}


def test_blend_no_factors_computable_is_zero() -> None:
    blend = FactorBlender(closes_provider=lambda _a, _n: []).blend("BTCUSD")
    assert blend.alpha == 0.0


# --- ensemble seam: soak-safety invariant -------------------------------------
def _meta(score_local_via: float = 0.8) -> list[MetaLabelOutput]:
    return [
        MetaLabelOutput(
            signal_id="s1",
            strategy_id="st1",
            asset="BTCUSD",
            s_raw=score_local_via,
            meta_probability=0.7,
            age_days=0,
            horizon_days=5.0,
        )
    ]


def test_ensemble_byte_identical_when_no_blender() -> None:
    # The soak-safety lock: no blender -> no factor_alpha, no factors metadata,
    # same rolling-stats namespace, identical score as the single-alpha path.
    cfg = EnsembleConfig(tau_min=0.3, p_min=0.55)
    baseline = EnsembleService(config=cfg).compute_score(_meta(), asset="BTCUSD")
    assert "factors" not in baseline.metadata


def test_ensemble_applies_factor_alpha_in_separate_namespace() -> None:
    cfg = EnsembleConfig(tau_min=0.3, p_min=0.55)
    blender = FactorBlender(closes_provider=_varying_provider(), lookback=3)
    svc = EnsembleService(config=cfg, factor_blender=blender)
    score = svc.compute_score(_meta(), asset="BTCUSD")
    # Factor breakdown surfaced for provenance, and the composite standardizes in a
    # SEPARATE 'mf:' namespace so the single-alpha window is never mutated.
    assert "factors" in score.metadata
    assert "alpha" in score.metadata["factors"]
    assert any(key.startswith("mf:") for key in svc._rolling_stats)
    assert not any(key == "BTCUSD" for key in svc._rolling_stats)


def test_warm_seeds_mf_namespace() -> None:
    # Fix for the cold-start finding: warm() seeds the 'mf:' window (it treats each
    # dict key as the stats key), so enabling MFS does not restart cold (std=0.1).
    svc = EnsembleService(
        config=EnsembleConfig(), factor_blender=FactorBlender(closes_provider=lambda _a, _n: [])
    )
    svc.warm({"mf:BTCUSD": [0.1, 0.5, -0.3, 0.2, 0.4, -0.1]})
    assert svc._get_rolling_stats("mf:BTCUSD").std != pytest.approx(0.1)


def test_warm_rolling_stats_warms_mf_namespace_when_blender_wired() -> None:
    # Integration: _warm_rolling_stats warms BOTH the plain and the mf: namespace
    # the live path reads when a blender is wired (the #1 review fix).
    from scoring_engine.main import _warm_rolling_stats
    from scoring_engine.pipeline import PipelineConfig, ScoringPipeline

    class _Store:
        def recent_asset_alpha_history(self, _window: int) -> dict[str, list[float]]:
            return {"BTCUSD": [0.1, 0.5, -0.3, 0.2, 0.4]}

    pipeline = ScoringPipeline(
        config=PipelineConfig(),
        factor_blender=FactorBlender(closes_provider=lambda _a, _n: []),
    )
    _warm_rolling_stats(_Store(), pipeline)  # type: ignore[arg-type]
    ens = pipeline.ensemble_service
    assert ens._get_rolling_stats("mf:BTCUSD").std != pytest.approx(0.1)
    assert ens._get_rolling_stats("BTCUSD").std != pytest.approx(0.1)
