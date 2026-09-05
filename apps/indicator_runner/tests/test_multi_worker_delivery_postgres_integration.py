"""Twenty strategy workers waking on one committed bar batch (PostgreSQL, opt-in).

Proves, on a real database and a real LISTEN/NOTIFY channel, that the delivery
loop keeps connections bounded, delivers every committed signal to a stub
scoring endpoint, preserves per-worker order, does not duplicate under a repeated
notification or a reclaimed lease, recovers after a scoring outage, and stops
cleanly mid-drain. Latency from row commit to stub receipt is recorded, not
promised.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select, text

from indicator_runner.runtime_journal import (
    BufferedSignalEmitter,
    DurableSignalRelay,
    StrategyRuntimeIdentity,
    StrategyRuntimeStore,
)
from indicator_runner.signal_delivery import SignalDeliveryLoop
from indicator_runner.signal_worker import SignalWorker
from lib_application.db.models import (
    ConsumerWatermark,
    Instrument,
    InstrumentAlias,
    OutboxEvent,
    Strategy,
    StrategyDecision,
    StrategyRuntimeState,
    User,
)
from lib_application.db.session import create_engine_for_env, dispose_engine, get_session_factory
from lib_application.outbox import OutboxStore
from lib_application.services.price_ingestion_service import PriceIngestionService
from lib_data.market_data import CandleRow
from lib_infrastructure.market_data.coinbase_client import CoinbaseCandleClient
from lib_strategy.signals.emitter import HttpSignalEmitter
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import SignalAction
from lib_strategy.signals.utils import compute_external_signal_id, extract_price_provenance

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = _REPO_ROOT / "tests/fixtures/market_data/coinbase_btcusdc_1m_integration_2026-06-10.json"
_SOURCE = CoinbaseCandleClient.source
_SYMBOL = "BTCUSDC"
_PRODUCT_ID = "BTC-USDC"
_WORKERS = 20
_WORKER_PREFIX = "multiworker-probe"
_STRATEGY_PREFIX = "multiworker_probe"
_OWNER_ID = "multiworker-probe-owner"
_WAIT = 20.0


# ---------------------------------------------------------------------------
# Stub scoring endpoint
# ---------------------------------------------------------------------------


class _StubScoring:
    """Accepts /api/v1/signals, records arrivals, can fail or slow down on demand."""

    def __init__(self) -> None:
        self.received: list[tuple[float, str, str]] = []  # (monotonic, worker_id, external id)
        self.fail = threading.Event()
        self.delay_seconds = 0.0
        self._lock = threading.Lock()
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if stub.delay_seconds:
                    time.sleep(stub.delay_seconds)
                if stub.fail.is_set():
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'{"status":"unavailable"}')
                    return
                context = body.get("context") or {}
                with stub._lock:
                    stub.received.append(
                        (
                            time.monotonic(),
                            str(body.get("strategy_id")),
                            str(context.get("external_signal_id")),
                        )
                    )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def log_message(self, _format: str, *_args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def ids_for(self, strategy_id: str) -> list[str]:
        with self._lock:
            return [ext for _, sid, ext in self.received if sid == strategy_id]


# ---------------------------------------------------------------------------
# Probe strategy: one LONG per scheduled bar close
# ---------------------------------------------------------------------------


class _ProbeStrategy(PureSignalStrategy):
    def __init__(self, strategy_id: str, entry_timestamps: set[datetime]) -> None:
        super().__init__(
            strategy_id=strategy_id,
            strategy_type="indicator",
            config={"strategy_version": "1.0.0"},
        )
        self._entries = entry_timestamps

    @property
    def warmup_bars_needed(self) -> int:
        return 1

    def initialize(self) -> None:
        return None

    def on_data(self, state: MarketState) -> None:
        if state.timestamp not in self._entries:
            return
        ext_id = compute_external_signal_id(
            strategy_id=self.strategy_id,
            symbol=state.symbol,
            action=SignalAction.LONG,
            bar_close_ts=state.timestamp,
            strategy_version="1.0.0",
            reason="probe_entry",
        )
        self.emit_long(
            symbol=state.symbol,
            entry_price=state.close,
            confidence=0.7,
            timestamp=state.timestamp,
            horizon="1H",
            stop_loss=state.close * 0.994,
            take_profit=state.close * 1.004,
            external_signal_id=ext_id,
            expires_at=state.timestamp + timedelta(seconds=60),
            strategy_version="1.0.0",
            metadata={"external_signal_id": ext_id, **extract_price_provenance(state.metadata)},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fixture_rows(instr_id: int) -> list[CandleRow]:
    fixture: dict[str, Any] = json.loads(_FIXTURE.read_text())
    rows = CoinbaseCandleClient.normalize_candles(
        instr_id=instr_id, product_id=_PRODUCT_ID, candles=fixture["candles"], timeframe="1m"
    )
    assert len(rows) == 11
    return sorted(rows, key=lambda row: row.ts)


def _wait_until(predicate: Any, *, what: str, timeout: float = _WAIT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    msg = f"Timed out waiting for {what}"
    raise AssertionError(msg)


def _connections(session_factory: Any) -> int:
    with session_factory() as session:
        return int(
            session.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
            ).scalar_one()
        )


def _notify(session_factory: Any, latest_ts: datetime) -> None:
    payload = json.dumps(
        {
            "symbol": _SYMBOL,
            "timeframe": "1m",
            "source": _SOURCE,
            "latest_ts": latest_ts.isoformat(),
        }
    )
    with session_factory() as session:
        session.execute(text("NOTIFY new_market_data, :payload"), {"payload": payload})
        session.commit()


def _provision(session_factory: Any) -> int:
    with session_factory() as session:
        if session.get(User, _OWNER_ID) is None:
            session.add(
                User(
                    user_id=_OWNER_ID,
                    email="multiworker@example.invalid",
                    base_ccy="USD",
                    is_deployment_owner=True,
                )
            )
        instrument = session.scalars(
            select(Instrument).where(Instrument.canonical == _SYMBOL)
        ).one_or_none()
        if instrument is not None:
            pytest.skip(
                "Refusing to reuse an existing BTCUSDC catalogue row; use an isolated database"
            )
        instrument = Instrument(
            asset_class="crypto",
            canonical=_SYMBOL,
            settlement_currency="USD",
            is_tradable=True,
            market_session_policy="continuous",
        )
        session.add(instrument)
        session.flush()
        session.add(
            InstrumentAlias(instr_id=instrument.instr_id, alias=_SYMBOL, source="canonical")
        )
        for index in range(_WORKERS):
            session.add(
                Strategy(
                    strategy_id=f"{_STRATEGY_PREFIX}_{index:02d}",
                    strategy_name=f"Probe {index:02d}",
                )
            )
        session.commit()
        return int(instrument.instr_id)


def _cleanup(session_factory: Any, instr_id: int | None) -> None:
    with session_factory() as session:
        for index in range(_WORKERS):
            worker_id = f"{_WORKER_PREFIX}-{index:02d}"
            session.execute(delete(StrategyDecision).where(StrategyDecision.worker_id == worker_id))
            session.execute(
                delete(StrategyRuntimeState).where(StrategyRuntimeState.worker_id == worker_id)
            )
            session.execute(delete(OutboxEvent).where(OutboxEvent.ordering_key == worker_id))
            session.execute(
                delete(ConsumerWatermark).where(ConsumerWatermark.worker_id == worker_id)
            )
        if instr_id is not None:
            session.execute(text("DELETE FROM prices WHERE instr_id = :i"), {"i": instr_id})
            session.execute(delete(InstrumentAlias).where(InstrumentAlias.instr_id == instr_id))
            session.execute(delete(Instrument).where(Instrument.instr_id == instr_id))
        session.execute(delete(Strategy).where(Strategy.strategy_id.like(f"{_STRATEGY_PREFIX}_%")))
        session.execute(delete(User).where(User.user_id == _OWNER_ID))
        session.commit()


class _Fleet:
    """Twenty workers, each with its own engine, relay, delivery loop and LISTEN connection."""

    def __init__(self, dsn: str, stub_url: str, entries: set[datetime]) -> None:
        self.engines: list[Any] = []
        self.workers: list[SignalWorker] = []
        self.loops: list[SignalDeliveryLoop] = []
        for index in range(_WORKERS):
            strategy_id = f"{_STRATEGY_PREFIX}_{index:02d}"
            worker_id = f"{_WORKER_PREFIX}-{index:02d}"
            engine = create_engine_for_env(db_url=dsn)
            factory = get_session_factory(engine=engine)
            buffer = BufferedSignalEmitter()
            strategy = _ProbeStrategy(strategy_id, entries)
            strategy._emitter = buffer
            identity = StrategyRuntimeIdentity.from_strategy(
                worker_id=worker_id,
                strategy=strategy,
                symbols=[_SYMBOL],
                source=_SOURCE,
                timeframe="1m",
                consolidation_minutes=0,
            )
            relay = DurableSignalRelay(
                outbox=OutboxStore(factory),
                emitter=HttpSignalEmitter(base_url=stub_url, max_retries=0),
                worker_id=worker_id,
                strategy_id=strategy_id,
                ordering_key=identity.ordering_key,
                retry_base_seconds=1,
            )
            loop = SignalDeliveryLoop(
                run_once=relay.run_once, worker_id=worker_id, idle_interval_seconds=1.0
            )
            worker = SignalWorker(
                strategy=strategy,
                session_factory=factory,
                worker_id=worker_id,
                symbols=[_SYMBOL],
                consolidation_minutes=0,
                dsn=dsn,
                bootstrap_bars=3,
                source=_SOURCE,
                timeframe="1m",
                signal_buffer=buffer,
                runtime_store=StrategyRuntimeStore(factory, identity),
                signal_relay=relay,
                delivery_loop=loop,
            )
            self.engines.append(engine)
            self.workers.append(worker)
            self.loops.append(loop)

    def start(self) -> None:
        for worker in self.workers:
            worker.start()

    def listening(self) -> bool:
        """True once PostgreSQL has acknowledged every worker's LISTEN.

        The test has no supervising main loop, so a NOTIFY published before a
        worker's LISTEN is registered would never be recovered by catch-up.
        """
        return all(
            worker._listener is not None and worker._listener.wait_until_listening(0.0)
            for worker in self.workers
        )

    def stop(self) -> float:
        started = time.monotonic()
        for worker in self.workers:
            worker.stop()
        elapsed = time.monotonic() - started
        for engine in self.engines:
            dispose_engine(engine)
        return elapsed


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_twenty_workers_wake_together_with_bounded_connections(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL not set; skipping PostgreSQL integration")
    # The launcher gives strategy workers this pool; mirror it for every engine below.
    monkeypatch.setenv("DB_POOL_SIZE", "2")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("DB_POOL_CONNECTION_BUDGET", "2")

    control_engine = create_engine_for_env(db_url=dsn)
    control = get_session_factory(engine=control_engine)
    ingestion = PriceIngestionService(control)
    stub = _StubScoring()
    stub.start()
    instr_id: int | None = None
    fleet: _Fleet | None = None
    try:
        instr_id = _provision(control)
        rows = _fixture_rows(instr_id)
        # Bars 0-2 warm the workers; 3, 5 and 6 carry probe entries; 7 is the
        # outage bar; 8-10 are drained slowly while the fleet stops.
        entries = {rows[i].ts.replace(tzinfo=UTC) for i in (3, 5, 6, 7, 8, 9, 10)}
        ingestion.upsert_candles(rows[:3])

        baseline = _connections(control)
        fleet = _Fleet(dsn, stub.url, entries)
        fleet.start()
        _wait_until(fleet.listening, what="LISTEN registration on every worker")

        # 1. Connections stay within 3 per worker (2 pooled + 1 LISTEN).
        _wait_until(
            lambda: _connections(control) - baseline <= _WORKERS * 3,
            what="bounded connections",
            timeout=5.0,
        )
        assert _connections(control) - baseline <= _WORKERS * 3

        # 2. One committed bar + one NOTIFY -> twenty deliveries; latency recorded.
        committed_at = time.monotonic()
        ingestion.upsert_candles([rows[3]])
        _notify(control, rows[3].ts.replace(tzinfo=UTC))
        _wait_until(lambda: len(stub.received) >= _WORKERS, what="first wave delivery")
        first_wave = [t - committed_at for t, _, _ in stub.received[:_WORKERS]]
        assert len({ext for _, _, ext in stub.received}) == _WORKERS
        print(
            f"delivery latency s: p50={statistics.median(first_wave):.3f} "
            f"max={max(first_wave):.3f} n={len(first_wave)}"
        )
        with control() as session:
            statuses = session.execute(
                text(
                    "SELECT status, count(*) FROM outbox_events "
                    "WHERE topic = 'signals.submit' AND ordering_key LIKE :prefix GROUP BY status"
                ),
                {"prefix": f"{_WORKER_PREFIX}-%"},
            ).all()
        assert dict(statuses) == {"published": _WORKERS}

        # 3. A repeated NOTIFY without a new bar delivers nothing new.
        _notify(control, rows[3].ts.replace(tzinfo=UTC))
        time.sleep(2.5)
        assert len(stub.received) == _WORKERS

        # 4. A reclaimed lease redelivers exactly that row and it ends published again.
        with control() as session:
            target = session.execute(
                text(
                    "UPDATE outbox_events SET status = 'in_progress', claim_owner = 'dead-worker', "
                    "claimed_at = now() - interval '10 minutes', published_at = NULL "
                    "WHERE topic = 'signals.submit' AND ordering_key = :key RETURNING event_id, attempts"
                ),
                {"key": f"{_WORKER_PREFIX}-00"},
            ).one()
            session.commit()
        _wait_until(lambda: len(stub.received) == _WORKERS + 1, what="lease reclaim redelivery")
        with control() as session:
            reclaimed = session.execute(
                text("SELECT status, attempts FROM outbox_events WHERE event_id = :id"),
                {"id": target.event_id},
            ).one()
        assert reclaimed.status == "published"
        assert reclaimed.attempts == target.attempts + 1

        # 5. Two more bars: per-worker order follows bar order.
        ingestion.upsert_candles([rows[4], rows[5], rows[6]])
        _notify(control, rows[6].ts.replace(tzinfo=UTC))
        _wait_until(lambda: len(stub.received) >= _WORKERS * 3 + 1, what="second and third wave")
        for index in range(_WORKERS):
            strategy_id = f"{_STRATEGY_PREFIX}_{index:02d}"
            ids = stub.ids_for(strategy_id)
            expected = [
                compute_external_signal_id(
                    strategy_id=strategy_id,
                    symbol=_SYMBOL,
                    action=SignalAction.LONG,
                    bar_close_ts=rows[i].ts.replace(tzinfo=UTC),
                    strategy_version="1.0.0",
                    reason="probe_entry",
                )
                for i in (3, 5, 6)
            ]
            # The reclaimed row of worker 00 appears twice; order of first appearances holds.
            first_seen = list(dict.fromkeys(ids))
            assert first_seen == expected, strategy_id

        # 6. Outage: deliveries fail, back off, then recover through the idle pass.
        stub.fail.set()
        ingestion.upsert_candles([rows[7]])
        _notify(control, rows[7].ts.replace(tzinfo=UTC))
        _wait_until(lambda: _count(control, "failed") == _WORKERS, what="failed rows during outage")
        stub.fail.clear()
        _wait_until(
            lambda: _count(control, "published") == _WORKERS * 4, what="recovery after outage"
        )

        # 7. Stop every worker while a slow drain is in flight.
        stub.delay_seconds = 0.5
        ingestion.upsert_candles(rows[8:])
        _notify(control, rows[-1].ts.replace(tzinfo=UTC))
        time.sleep(0.2)
        elapsed = fleet.stop()
        fleet = None
        assert elapsed < _WORKERS * 15.0
        with control() as session:
            stale = session.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE topic = 'signals.submit' "
                    "AND ordering_key LIKE :prefix AND status = 'in_progress' "
                    "AND claimed_at < now() - interval '60 seconds'"
                ),
                {"prefix": f"{_WORKER_PREFIX}-%"},
            ).scalar_one()
        assert stale == 0
    finally:
        if fleet is not None:
            fleet.stop()
        stub.stop()
        _cleanup(control, instr_id)
        dispose_engine(control_engine)


def _count(session_factory: Any, status: str) -> int:
    with session_factory() as session:
        return int(
            session.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE topic = 'signals.submit' "
                    "AND ordering_key LIKE :prefix AND status = :status"
                ),
                {"prefix": f"{_WORKER_PREFIX}-%", "status": status},
            ).scalar_one()
        )
