"""N8: ingest must dispatch the signal it just ingested, not _build_latest_signal(symbol).

Under concurrent same-symbol ingestion — a multi-strategy instrument (e.g. ETH/USD)
receives signals from two strategy workers at different replay positions — the "latest
signal for the symbol" is frequently a DIFFERENT signal than the one being ingested.
Dispatching that latest signal evaluated bindings against the wrong strategy/action and
dropped the just-ingested signal's decisions (ETH got 90/226 decisions vs SOL 198/198).
"""

import datetime as dt
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lib_application.db import models as app_models
from scoring_engine.api import create_app
from scoring_engine.dispatcher import DispatchProviderContexts
from scoring_engine.engine import ScoreEngine
from scoring_engine.models import ScoringUserBinding
from scoring_engine.storage import AppScoreStore


def _payload(strategy_id: str, ts: dt.datetime) -> dict[str, Any]:
    return {
        "ts": ts.isoformat(),
        "strategy_id": strategy_id,
        "symbol": "BTCUSD",
        "insight": {"direction": "Up", "magnitude": 0.5, "confidence": 0.9, "horizon": "1D"},
        "context": {
            "strategy_version": "1.0.0",
            "asset_class": "crypto",
            "sector": "crypto",
            "industry": "layer1",
            "index": "crypto_index",
            "entry_price": 30000,
        },
    }


def test_ingest_dispatches_the_ingested_signal_not_latest_for_symbol(
    provision_scoring_catalogue,
) -> None:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    engine = ScoreEngine(store=store, default_weight=1.0, half_life_bars=10)
    provision_scoring_catalogue(
        store,
        strategy_ids=["rsi_strat", "ema_strat"],
    )
    # Stub list_bindings with an in-memory match-all binding (autopilot, threshold 0,
    # empty asset_filter = match all) so every ingested signal yields a decision. This
    # isolates the test to dispatch signal-selection and bypasses the unrelated
    # the unrelated production binding-configuration API.
    binding = ScoringUserBinding(
        user_id="u1", strategy_id=None, asset_score_threshold=0.0, autopilot=True
    )

    def _bindings() -> list[ScoringUserBinding]:
        return [binding]

    engine.store.list_bindings = _bindings  # type: ignore[method-assign]

    dispatched_signals: list[Any] = []

    class _RecordingDispatcher:
        # Mirrors the production two-phase dispatch API: provider contexts are
        # resolved on the event loop, then the sync dispatch runs inside the
        # worker-thread ingest unit of work.
        async def resolve_provider_contexts(self, **_kw: Any) -> DispatchProviderContexts:
            return DispatchProviderContexts()

        def dispatch_resolved(self, *, signal: Any, decisions: Any, **_kw: Any) -> list[dict]:
            dispatched_signals.append(signal)
            return [{"user_id": d.user_id, "status": "queued"} for d in decisions]

    client = TestClient(create_app(engine, dispatcher=_RecordingDispatcher()))
    now = dt.datetime.now(tz=dt.UTC)

    # B is the NEWER signal for BTCUSD (becomes _build_latest_signal) from a DIFFERENT strategy.
    assert client.post("/api/v1/signals", json=_payload("rsi_strat", now)).status_code == 200
    # C is OLDER but is the signal being ingested now. With the old bug, dispatch saw B (rsi).
    assert (
        client.post(
            "/api/v1/signals", json=_payload("ema_strat", now - dt.timedelta(seconds=60))
        ).status_code
        == 200
    )

    assert dispatched_signals, "dispatcher was never invoked"
    dispatched = dispatched_signals[-1]
    assert dispatched.strategy_id == "ema_strat", (
        "ingest dispatched the latest-for-symbol signal instead of the ingested one "
        f"(got {dispatched.strategy_id!r})"
    )
    with Session(store._engine) as session:
        canonical = (
            session.query(app_models.CanonicalSignal).filter_by(strategy_id="ema_strat").one()
        )
        # signals.ingested is retired: the canonical_signals row is the surviving record.
        retired_events = (
            session.query(app_models.OutboxEvent)
            .filter_by(event_key=f"canonical-signal:{dispatched.signal_id}")
            .count()
        )
    assert dispatched.metadata["canonical_signal_id"] == canonical.signal_id
    assert retired_events == 0


def test_redelivery_dispatches_persisted_origin_to_a_new_account(provision_scoring_catalogue):
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    engine = ScoreEngine(store=store)
    provision_scoring_catalogue(store, strategy_ids=["rsi_strat"])
    account = {"id": 1}
    store.list_bindings = lambda: [
        ScoringUserBinding(
            user_id="u1",
            strategy_id="rsi_strat",
            broker_account_id=account["id"],
            asset_score_threshold=0,
            autopilot=True,
        )
    ]
    seen = []

    class Recorder:
        async def resolve_provider_contexts(self, **kwargs):
            return DispatchProviderContexts()

        def dispatch_resolved(self, *, signal, decisions, **kwargs):
            seen.append((signal, decisions[0].broker_account_id))
            return []

    client = TestClient(create_app(engine, dispatcher=Recorder()))
    payload = _payload("rsi_strat", dt.datetime.now(dt.UTC))
    payload["context"]["external_signal_id"] = "same-bar-new-account"
    for account_id, run_id in [(1, "first-run"), (2, "retry-run")]:
        account["id"] = account_id
        assert (
            client.post("/api/v1/signals", json=payload, headers={"x-run-id": run_id}).status_code
            == 200
        )
    assert [account_id for _, account_id in seen] == [1, 2]
    first, retry = (signal for signal, _ in seen)
    assert first.run_id == retry.run_id == "first-run"
    assert first.signal_id == retry.signal_id
    assert first.external_signal_id == retry.external_signal_id == "same-bar-new-account"
