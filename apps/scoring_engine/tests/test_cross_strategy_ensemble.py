"""SC-asset: the gating asset score is a cross-strategy ensemble when enabled.

The pipeline already aggregates across strategies in layer 3, but ingest only
ever fed it the lone incoming signal. With SCORING_CROSS_STRATEGY_ENSEMBLE on,
ingest_signal blends the recent one-per-strategy signals for the asset. These
tests lock the safety invariant (single strategy == pre-change behavior) and the
gather rules (dedup, freshness, neutral-drop, latest-per-strategy, malformed
skip, rolling-stats isolation).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from lib_strategy.signals.signal import Signal, SignalAction
from scoring_engine.domain import MarketContext, MarketRegime
from scoring_engine.engine import ScoreEngine, signal_from_record
from scoring_engine.models import SignalRecord
from scoring_engine.storage_memory import InMemoryScoreStore

NOW = dt.datetime(2026, 6, 26, 12, 0, tzinfo=dt.UTC)


def _engine(*, cross: bool, freshness: int = 3600) -> ScoreEngine:
    engine = ScoreEngine(InMemoryScoreStore(), default_weight=1.0, half_life_bars=10)
    engine.pipeline.set_market_context(
        "BTCUSD",
        MarketContext(
            regime=MarketRegime.BULL_QUIET,
            as_of=NOW - dt.timedelta(days=1),
            realized_vol_20d=0.01,
        ),
    )
    engine._cross_strategy_ensemble = cross
    engine._sibling_freshness_seconds = freshness
    return engine


def _sig(strategy_id: str, action: SignalAction, ts: dt.datetime, *, conf: float = 0.8) -> Signal:
    return Signal(
        strategy_id=strategy_id,
        strategy_type="indicator",
        symbol="BTCUSD",
        action=action,
        confidence=conf,
        timestamp=ts,
        entry_price=30000.0,
        expected_return=0.05,
        predicted_risk=0.10,
        horizon_days=1.0,
        asset_class="crypto",
        source="coinbase_live",
    )


def _asset_score(engine: ScoreEngine) -> float:
    rec = engine.store.get_latest_score("BTCUSD", scope="asset")
    assert rec is not None
    return rec.score


# --- Safety invariant: single strategy is byte-identical to the old path ------


def test_single_strategy_score_identical_flag_off_vs_on() -> None:
    off = _engine(cross=False)
    on = _engine(cross=True)
    s1 = _sig("alpha", SignalAction.LONG, NOW)
    # Same strategy re-emitting later; both engines see an identical sequence.
    s2 = _sig("alpha", SignalAction.LONG, NOW + dt.timedelta(minutes=15))
    off.ingest_signal(s1)
    on.ingest_signal(s1)
    off.ingest_signal(s2)
    on.ingest_signal(s2)
    assert _asset_score(on) == _asset_score(off)


def test_flag_off_gather_short_circuits_without_store_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(cross=False)
    calls: list[str] = []
    monkeypatch.setattr(engine.store, "list_signals", lambda *a, **k: calls.append("read") or [])
    incoming = _sig("alpha", SignalAction.LONG, NOW)
    # The gather returns the lone incoming and never touches the store.
    assert engine._gather_cross_strategy_batch(incoming) == [incoming]
    assert calls == []


# --- Gather rules -------------------------------------------------------------


def _score_record(engine: ScoreEngine):  # type: ignore[no-untyped-def]
    rec = engine.store.get_latest_score("BTCUSD", scope="asset")
    assert rec is not None
    return rec


def test_two_strategies_blend_into_one_score() -> None:
    engine = _engine(cross=True)
    engine.ingest_signal(_sig("beta", SignalAction.LONG, NOW))
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW + dt.timedelta(minutes=1)))
    rec = _score_record(engine)
    # Both strategies contributed: components + provenance reflect the blend.
    assert {c.strategy_id for c in rec.components} == {"alpha", "beta"}
    incoming = engine.store.list_signals("BTCUSD", limit=1)[0]
    assert incoming.metadata["cross_strategy"]["batch_size"] == 2
    assert incoming.metadata["cross_strategy"]["siblings"] == ["beta"]


def test_neutral_sibling_dropped_no_dilution() -> None:
    engine = _engine(cross=True)
    # A CLOSE from beta must NOT join the batch (would only dilute magnitude).
    engine.ingest_signal(_sig("beta", SignalAction.CLOSE, NOW))
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW + dt.timedelta(minutes=1)))
    rec = _score_record(engine)
    assert {c.strategy_id for c in rec.components} == {"alpha"}
    incoming = engine.store.list_signals("BTCUSD", limit=1)[0]
    assert "cross_strategy" not in incoming.metadata  # batch of 1


def test_same_strategy_reemit_not_double_counted() -> None:
    engine = _engine(cross=True)
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW))
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW + dt.timedelta(minutes=15)))
    rec = _score_record(engine)
    assert [c.strategy_id for c in rec.components] == ["alpha"]


def test_stale_sibling_excluded_two_sided() -> None:
    engine = _engine(cross=True, freshness=3600)
    # beta 2h before incoming -> outside the 1h window.
    engine.ingest_signal(_sig("beta", SignalAction.LONG, NOW - dt.timedelta(hours=2)))
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW))
    assert {c.strategy_id for c in _score_record(engine).components} == {"alpha"}


def test_future_sibling_excluded_two_sided() -> None:
    engine = _engine(cross=True, freshness=3600)
    # beta 2h AFTER incoming (clock skew / replay) -> excluded by abs() window.
    engine.ingest_signal(_sig("beta", SignalAction.LONG, NOW + dt.timedelta(hours=2)))
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW))
    assert {c.strategy_id for c in _score_record(engine).components} == {"alpha"}


def test_latest_per_strategy_selected() -> None:
    engine = _engine(cross=True)
    engine.ingest_signal(_sig("beta", SignalAction.LONG, NOW - dt.timedelta(minutes=30)))
    engine.ingest_signal(_sig("beta", SignalAction.LONG, NOW - dt.timedelta(minutes=5)))
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW))
    rec = _score_record(engine)
    # Exactly one beta contribution (its latest), plus alpha.
    assert sorted(c.strategy_id for c in rec.components) == ["alpha", "beta"]


def test_gather_canonicalizes_incoming_symbol_from_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Incoming carries an alias symbol; siblings are stamped with the instrument's
    # canonical symbol (the store maps rows through one resolved instrument). The
    # gather re-stamps the incoming to canonical so the batch is symbol-homogeneous
    # (no spurious "mixed symbols" pipeline warning) and the xstrat rolling-stats
    # namespace is per-canonical-asset, not per-alias. signal_id is preserved.
    engine = _engine(cross=True)
    sibling = SignalRecord(
        signal_id="beta-1",
        strategy_id="beta",
        strategy_type="indicator",
        symbol="BTCUSD",  # canonical, as the store stamps it
        action="long",
        confidence=0.8,
        timestamp=NOW,
    )
    monkeypatch.setattr(engine.store, "list_latest_signal_per_strategy", lambda *a, **k: [sibling])
    incoming = replace(_sig("alpha", SignalAction.LONG, NOW), symbol="BTC-USD")
    batch = engine._gather_cross_strategy_batch(incoming)
    assert [s.symbol for s in batch] == ["BTCUSD", "BTCUSD"]  # homogeneous
    assert batch[0].signal_id == incoming.signal_id  # identity preserved
    assert batch[0].strategy_id == "alpha"


def test_gather_no_siblings_leaves_incoming_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(cross=True)
    monkeypatch.setattr(engine.store, "list_latest_signal_per_strategy", lambda *a, **k: [])
    incoming = replace(_sig("alpha", SignalAction.LONG, NOW), symbol="BTC-USD")
    # No siblings -> batch is the lone incoming, alias symbol untouched.
    assert engine._gather_cross_strategy_batch(incoming) == [incoming]


def test_malformed_sibling_skipped_not_double_count() -> None:
    engine = _engine(cross=True)
    # Inject a beta row with an unrecognised strategy_type directly into the store.
    engine.store.add_signal(
        SignalRecord(
            signal_id="bad-1",
            strategy_id="beta",
            strategy_type="unknown",
            symbol="BTCUSD",
            action="long",
            confidence=0.8,
            timestamp=NOW - dt.timedelta(minutes=1),
        )
    )
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW))
    # Malformed beta dropped -> only alpha gates; ingest did not raise.
    assert {c.strategy_id for c in _score_record(engine).components} == {"alpha"}


def test_exited_strategy_latest_close_suppresses_older_long() -> None:
    # beta went LONG then CLOSED (exited). Its LATEST row decides: a stale older
    # LONG must NOT sneak into the blend as an active directional vote.
    engine = _engine(cross=True)
    engine.ingest_signal(_sig("beta", SignalAction.LONG, NOW - dt.timedelta(minutes=30)))
    engine.ingest_signal(_sig("beta", SignalAction.CLOSE, NOW - dt.timedelta(minutes=1)))
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW))
    rec = _score_record(engine)
    assert {c.strategy_id for c in rec.components} == {"alpha"}
    incoming = engine.store.list_signals("BTCUSD", limit=1)[0]
    assert "cross_strategy" not in incoming.metadata  # beta dropped -> batch of 1


def test_null_timestamp_sibling_skipped_not_abort() -> None:
    # A sibling row with a null timestamp must be skipped, never raise out of
    # ingest (the freshness check runs before reconstruction).
    engine = _engine(cross=True)
    engine.store.add_signal(
        SignalRecord(
            signal_id="no-ts",
            strategy_id="beta",
            strategy_type="indicator",
            symbol="BTCUSD",
            action="long",
            confidence=0.8,
            timestamp=None,  # type: ignore[arg-type]
        )
    )
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW))
    assert {c.strategy_id for c in _score_record(engine).components} == {"alpha"}


def test_max_siblings_caps_batch() -> None:
    engine = _engine(cross=True)
    engine._max_siblings = 1
    engine.ingest_signal(_sig("beta", SignalAction.LONG, NOW - dt.timedelta(minutes=3)))
    engine.ingest_signal(_sig("gamma", SignalAction.LONG, NOW - dt.timedelta(minutes=2)))
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW))
    # alpha + exactly one sibling (cap=1), not both.
    assert len(_score_record(engine).components) == 2


# --- Rolling-stats isolation --------------------------------------------------


def test_multi_strategy_ingest_does_not_poison_single_window() -> None:
    engine = _engine(cross=True)
    stats = engine.pipeline.ensemble_service._rolling_stats
    # Two lone ingests update the default per-asset window twice.
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW))
    engine.ingest_signal(_sig("beta", SignalAction.LONG, NOW + dt.timedelta(minutes=1)))
    lone_window_len = len(stats["BTCUSD"].values)
    # A blend ingest must use the xstrat namespace and leave the default window alone.
    engine.ingest_signal(_sig("alpha", SignalAction.LONG, NOW + dt.timedelta(minutes=2)))
    assert "xstrat:BTCUSD" in stats
    assert len(stats["BTCUSD"].values) == lone_window_len  # default window untouched


# --- signal_from_record -------------------------------------------------------


def test_signal_from_record_roundtrip() -> None:
    rec = SignalRecord(
        signal_id="s-1",
        strategy_id="alpha",
        strategy_type="INDICATOR",  # case-normalised on reconstruction
        symbol="BTCUSD",
        action="long",
        confidence=0.7,
        timestamp=NOW,
        expected_return=0.05,
        predicted_risk=0.1,
        horizon_days=1.0,
    )
    sig = signal_from_record(rec)
    assert sig is not None
    assert sig.strategy_type == "indicator"
    assert sig.action is SignalAction.LONG
    assert sig.signal_id == "s-1"


def test_signal_from_record_rejects_unknown_strategy_type() -> None:
    rec = SignalRecord(
        signal_id="s-2",
        strategy_id="alpha",
        strategy_type="unknown",
        symbol="BTCUSD",
        action="long",
        confidence=0.7,
        timestamp=NOW,
    )
    assert signal_from_record(rec) is None
