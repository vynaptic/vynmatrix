from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from execution_engine.broker_resolver import BrokerResolver
from execution_engine.brokers.base import BrokerFill, BrokerOrderResult, OrderStatus
from execution_engine.config import BrokerType
from execution_engine.metrics.pnl_service import PnLLedgerUnavailableError
from execution_engine.pending_orders import PendingOrderRepository
from execution_engine.reconciliation import (
    ReconciliationWorker,
    classify_fifo_position_drift,
    classify_reconciliation,
)
from execution_engine.reconciliation_tracker import ReconciliationTracker
from lib_application.db.models import (
    Base,
    Broker,
    CanonicalSignal,
    Execution,
    Instrument,
    LinkedBrokerAccount,
    Order,
    Strategy,
    User,
)
from lib_application.db.models import OrderIntent as PersistedOrderIntent


def test_classify_reconciliation_detects_blocking_position_mismatches() -> None:
    findings = classify_reconciliation(
        local_positions=[
            {"symbol": "BTC-USD", "side": "long", "quantity": 1.0, "current_price": 100}
        ],
        broker_positions=[],
        pending_orders={},
    )

    assert [(finding.severity, finding.code) for finding in findings] == [
        ("block", "missing_broker_position")
    ]


def test_classify_reconciliation_rejects_unknown_position_side() -> None:
    with pytest.raises(ValueError, match="Unknown position side"):
        classify_reconciliation(
            local_positions=[
                {
                    "symbol": "BTC-USD",
                    "side": "mystery",
                    "quantity": 1.0,
                    "current_price": 100,
                }
            ],
            broker_positions=[],
            pending_orders={},
        )


def test_classify_reconciliation_downgrades_pending_quantity_drift_to_warning() -> None:
    findings = classify_reconciliation(
        local_positions=[
            {"symbol": "BTC-USD", "side": "long", "quantity": 1.0, "current_price": 100}
        ],
        broker_positions=[
            {"symbol": "BTC-USD", "side": "long", "quantity": 0.8, "current_price": 100}
        ],
        pending_orders={"order-1": {"breaker_key": "b", "symbol": "BTC-USD", "side": "BUY"}},
    )

    assert ("warn", "position_quantity_pending") in [
        (finding.severity, finding.code) for finding in findings
    ]


# ── PL-2: FIFO-ledger-vs-broker position drift (pure classifier) ────────────
def test_fifo_drift_within_tolerance_is_clean() -> None:
    assert (
        classify_fifo_position_drift(
            fifo_positions={"BTC-USD": 1.0},
            broker_positions=[{"symbol": "BTC-USD", "side": "long", "quantity": 1.0}],
        )
        == []
    )


def test_fifo_drift_quantity_mismatch_warns() -> None:
    findings = classify_fifo_position_drift(
        fifo_positions={"BTC-USD": 1.0},
        broker_positions=[{"symbol": "BTC-USD", "side": "long", "quantity": 0.5}],
    )
    assert [(f.severity, f.code) for f in findings] == [("warn", "fifo_position_quantity_drift")]
    assert findings[0].context["drift"] == 0.5


def test_fifo_drift_side_flip_warns() -> None:
    # FIFO net long 1, broker reports short 1 -> signed +1 vs -1 -> drift 2.
    findings = classify_fifo_position_drift(
        fifo_positions={"BTC-USD": 1.0},
        broker_positions=[{"symbol": "BTC-USD", "side": "short", "quantity": 1.0}],
    )
    assert findings[0].code == "fifo_position_quantity_drift"
    assert findings[0].context["drift"] == 2.0


def test_fifo_drift_position_missing_at_broker_warns() -> None:
    findings = classify_fifo_position_drift(fifo_positions={"BTC-USD": 1.0}, broker_positions=[])
    assert findings[0].context["broker_quantity"] == 0.0


def test_fifo_drift_phantom_broker_position_warns() -> None:
    findings = classify_fifo_position_drift(
        fifo_positions={},
        broker_positions=[{"symbol": "BTC-USD", "side": "long", "quantity": 1.0}],
    )
    assert findings[0].context["fifo_quantity"] == 0.0


class _FakePnLService:
    def __init__(self, positions: dict[str, Decimal], *, raises: bool = False) -> None:
        self._positions = positions
        self._raises = raises
        self.calls = 0
        self.scopes: list[tuple[str | None, str | None, int | None]] = []

    async def get_fifo_positions(  # type: ignore[no-untyped-def]
        self,
        user_id: str,
        strategy_id: str | None = None,
        broker=None,
        mode=None,
        account_id=None,
    ):
        self.calls += 1
        self.scopes.append((broker, mode, account_id))
        if self._raises:
            msg = "pnl boom"
            raise PnLLedgerUnavailableError(msg)
        return dict(self._positions)


def test_worker_records_and_alerts_fifo_drift_as_warn_not_block() -> None:
    # Broker + mirror agree (1.0 long) so classify_reconciliation is clean; the
    # FIFO ledger says 0.5 -> independent drift the mirror check is blind to.
    broker_pos = [{"symbol": "BTC-USD", "side": "long", "quantity": 1.0, "current_price": 100.0}]
    engine = _FakeEngine(_FakeBroker(broker_pos))
    position_store = _FakePositionStore(list(broker_pos))
    breach_store = _FakeBreachStore()
    pnl = _FakePnLService({"BTC-USD": Decimal("0.5")})
    worker = ReconciliationWorker(
        engine=engine,
        position_store=position_store,
        risk_breach_store=breach_store,
        interval_sec=300,
        pnl_service=pnl,
    )

    asyncio.run(worker.reconcile_once())

    fifo_breach = next(
        record
        for record in breach_store.records
        if record["rule_code"] == "fifo_position_quantity_drift"
    )
    assert fifo_breach["broker_account_id"] == 1
    assert any(a["event_type"] == "reconciliation_position_drift" for a in engine.alerts)
    assert engine.breaker_opens == []  # warn does not open the breaker
    assert engine.health_updates[-1][0] is True  # nor mark unhealthy


def test_worker_without_pnl_service_skips_fifo_check() -> None:
    broker_pos = [{"symbol": "BTC-USD", "side": "long", "quantity": 1.0, "current_price": 100.0}]
    engine = _FakeEngine(_FakeBroker(broker_pos))
    position_store = _FakePositionStore(list(broker_pos))
    breach_store = _FakeBreachStore()
    worker = ReconciliationWorker(
        engine=engine,
        position_store=position_store,
        risk_breach_store=breach_store,
        interval_sec=300,
    )

    asyncio.run(worker.reconcile_once())

    assert not any(r["rule_code"] == "fifo_position_quantity_drift" for r in breach_store.records)


def test_worker_scopes_fifo_ledger_to_broker_and_environment() -> None:
    # PL-2 #2: the ledger query must be scoped to the reconciled broker+environment
    # so another broker's / paper's fills don't pollute the comparison.
    broker_pos = [{"symbol": "BTC-USD", "side": "long", "quantity": 1.0, "current_price": 100.0}]
    engine = _FakeEngine(_FakeBroker(broker_pos))
    pnl = _FakePnLService({"BTC-USD": Decimal("1.0")})
    worker = ReconciliationWorker(
        engine=engine,
        position_store=_FakePositionStore(list(broker_pos)),
        risk_breach_store=_FakeBreachStore(),
        interval_sec=300,
        pnl_service=pnl,
    )

    asyncio.run(worker.reconcile_once())

    assert pnl.scopes == [("coinbase", "paper", 1)]


def test_worker_fifo_check_failure_does_not_open_breaker() -> None:
    # PL-2 #3: a P&L-compute failure is best-effort — it must not abort the cycle.
    broker_pos = [{"symbol": "BTC-USD", "side": "long", "quantity": 1.0, "current_price": 100.0}]
    engine = _FakeEngine(_FakeBroker(broker_pos))
    worker = ReconciliationWorker(
        engine=engine,
        position_store=_FakePositionStore(list(broker_pos)),
        risk_breach_store=_FakeBreachStore(),
        interval_sec=300,
        pnl_service=_FakePnLService({}, raises=True),
    )

    asyncio.run(worker.reconcile_once())

    assert engine.breaker_opens == []
    assert engine.health_updates[-1] == (True, None)


def test_worker_runs_fifo_check_once_per_user_broker() -> None:
    broker_pos = [{"symbol": "BTC-USD", "side": "long", "quantity": 1.0, "current_price": 100.0}]
    engine = _FakeEngine(_FakeBroker(broker_pos))
    # A second strategy context for the SAME user + broker.
    engine._contexts.append(
        {
            **engine._contexts[0],
            "strategy_id": "OtherStrategy",
            "strategy_breaker_key": "strategy:user-1:OtherStrategy:coinbase:coinbase-paper",
        }
    )
    pnl = _FakePnLService({"BTC-USD": Decimal("1.0")})
    worker = ReconciliationWorker(
        engine=engine,
        position_store=_FakePositionStore(list(broker_pos)),
        risk_breach_store=_FakeBreachStore(),
        interval_sec=300,
        pnl_service=pnl,
    )

    asyncio.run(worker.reconcile_once())

    # Account-level check runs once for the (user, broker), not per strategy.
    assert pnl.calls == 1


class _FakePositionStore:
    def __init__(self, local_positions):  # type: ignore[no-untyped-def]
        self._local_positions = list(local_positions)
        self.synced: list[list[dict]] = []

    def list_positions(  # type: ignore[no-untyped-def]
        self,
        *,
        user_id,
        broker_code,
        environment,
        account_id,
    ):
        return list(self._local_positions)

    def sync_positions(  # type: ignore[no-untyped-def]
        self,
        *,
        user_id,
        broker_code,
        positions,
        environment,
        account_id,
    ):
        self.synced.append(list(positions))


class _FakeBreachStore:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, **payload):  # type: ignore[no-untyped-def]
        self.records.append(payload)
        return len(self.records)


class _FakeBroker:
    def __init__(self, positions, open_orders=None):  # type: ignore[no-untyped-def]
        self._positions = list(positions)
        self._open_orders = list(open_orders or [])
        self._connected = True

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def get_positions(self):  # type: ignore[no-untyped-def]
        return list(self._positions)

    async def get_open_orders(self):  # type: ignore[no-untyped-def]
        return list(self._open_orders)


class _FakeEngine:
    def __init__(self, broker):  # type: ignore[no-untyped-def]
        self._broker = broker
        self._contexts = [
            {
                "breaker_key": "user-1:SwingHighLowPMO:coinbase:coinbase-paper",
                "strategy_breaker_key": "strategy:user-1:SwingHighLowPMO:coinbase:coinbase-paper",
                "broker_breaker_key": "broker:coinbase:paper",
                "user_id": "user-1",
                "strategy_id": "SwingHighLowPMO",
                "broker_code": "coinbase",
                "environment": "paper",
                "broker_account_id": 1,
                "credential_ref": "coinbase-paper",
                "profile": {"sandbox": True},
                "user_strategy_config": {},
                "credentials": {},
            }
        ]
        self.breaker_opens: list[dict] = []
        self.health_updates: list[tuple[bool, str | None]] = []
        self.alerts: list[dict] = []
        self._pending = {}

    def iter_reconciliation_contexts(self):  # type: ignore[no-untyped-def]
        return list(self._contexts)

    async def get_reconciliation_broker(self, context):  # type: ignore[no-untyped-def]
        return self._broker

    def pending_orders_for(self, breaker_key: str):  # type: ignore[no-untyped-def]
        return dict(self._pending)

    def open_circuit_breaker(self, **payload):  # type: ignore[no-untyped-def]
        self.breaker_opens.append(payload)

    def emit_alert(self, **payload):  # type: ignore[no-untyped-def]
        self.alerts.append(payload)

    def is_broker_system_error(self, *, error_message: str, error_code=None) -> bool:  # type: ignore[no-untyped-def]
        return "broker unavailable" in error_message.lower()

    def mark_reconciliation_health(self, *, healthy: bool, error: str | None = None) -> None:
        self.health_updates.append((healthy, error))


def test_reconciliation_worker_opens_breaker_on_missing_broker_position() -> None:
    engine = _FakeEngine(_FakeBroker([]))
    position_store = _FakePositionStore(
        [{"symbol": "BTC-USD", "side": "long", "quantity": 1.0, "current_price": 100.0}]
    )
    breach_store = _FakeBreachStore()
    worker = ReconciliationWorker(
        engine=engine,
        position_store=position_store,
        risk_breach_store=breach_store,
        interval_sec=300,
    )

    asyncio.run(worker.reconcile_once())

    assert len(engine.breaker_opens) == 1
    assert breach_store.records[0]["rule_code"] == "missing_broker_position"
    assert position_store.synced == [[]]
    assert engine.health_updates[-1][0] is False


def test_reconciliation_worker_syncs_broker_positions_when_state_matches() -> None:
    engine = _FakeEngine(
        _FakeBroker(
            [{"symbol": "BTC-USD", "side": "long", "quantity": 1.0, "current_price": 101.0}]
        )
    )
    position_store = _FakePositionStore(
        [{"symbol": "BTC-USD", "side": "long", "quantity": 1.0, "current_price": 100.0}]
    )
    worker = ReconciliationWorker(
        engine=engine,
        position_store=position_store,
        risk_breach_store=None,
        interval_sec=300,
    )

    asyncio.run(worker.reconcile_once())

    assert engine.breaker_opens == []
    assert position_store.synced == [
        [
            {
                "symbol": "BTC-USD",
                "side": "long",
                "quantity": 1.0,
                "entry_price": 0.0,
                "current_price": 101.0,
                "quantity_unit": None,
                "contract_multiplier": None,
                "gross_notional": None,
                "notional_currency": None,
            }
        ]
    ]
    assert engine.health_updates[-1] == (True, None)


# ── C1/N11 regression: reconciliation must resolve the exact account-scoped
# broker instance the execution path filled. Anonymous broker books are no
# longer a supported state.


def _resolve_test_paper_broker(
    resolver: BrokerResolver,
    *,
    user_id: str,
    account_id: int,
):
    return asyncio.run(
        resolver.get_broker(
            broker_type=BrokerType.PAPER,
            environment="paper",
            credential_ref="coinbase-paper",
            user_id=user_id,
            broker_account_id=account_id,
            account_currency="USD",
            settlement_currency="USD",
            paper_initial_equity=100_000.0,
            paper_initial_cash=100_000.0,
        )
    )


def test_resolver_reuses_exact_account_scoped_paper_instance() -> None:
    resolver = BrokerResolver()

    first = _resolve_test_paper_broker(resolver, user_id="demo_peer", account_id=101)
    second = _resolve_test_paper_broker(resolver, user_id="demo_peer", account_id=101)

    assert first is second


def test_resolver_isolates_two_explicit_accounts_for_same_user() -> None:
    resolver = BrokerResolver()
    first = _resolve_test_paper_broker(resolver, user_id="demo_peer", account_id=101)
    second = _resolve_test_paper_broker(resolver, user_id="demo_peer", account_id=202)
    assert first is not None
    assert second is not None
    first.load_positions(
        [
            {
                "symbol": "ETH-USD",
                "side": "long",
                "quantity": 6.32,
                "entry_price": 2000.0,
                "current_price": 2000.0,
                "quantity_unit": "asset",
                "contract_multiplier": None,
                "gross_notional": 12_640.0,
                "notional_currency": "USD",
                "account_cost_basis": 12_640.0,
            }
        ]
    )

    assert first is not second
    assert [position.symbol for position in asyncio.run(first.get_positions())] == ["ETH-USD"]
    assert asyncio.run(second.get_positions()) == []


class _ResolverBackedEngine(_FakeEngine):
    """Engine double whose get_reconciliation_broker uses the REAL BrokerResolver
    cache-key path, including the explicit linked-account identity."""

    def __init__(self, resolver: BrokerResolver, *, use_wrong_account: bool = False) -> None:
        super().__init__(_FakeBroker([]))
        self._resolver = resolver
        self._use_wrong_account = use_wrong_account

    async def get_reconciliation_broker(self, context):  # type: ignore[no-untyped-def]
        account_id = int(context["broker_account_id"])
        if self._use_wrong_account:
            account_id += 1
        return await self._resolver.get_broker(
            broker_type=BrokerType.PAPER,
            environment=str(context["environment"]),
            credential_ref=str(context["credential_ref"]),
            user_id=str(context["user_id"]),
            broker_account_id=account_id,
            account_currency="USD",
            settlement_currency="USD",
            paper_initial_equity=100_000.0,
            paper_initial_cash=100_000.0,
        )


def _seed_resolver_with_open_position(resolver: BrokerResolver, *, user_id: str) -> None:
    broker = _resolve_test_paper_broker(resolver, user_id=user_id, account_id=1)
    assert broker is not None
    broker.load_positions(
        [
            {
                "symbol": "BTC-USD",
                "side": "long",
                "quantity": 1.0,
                "entry_price": 100.0,
                "current_price": 100.0,
                "quantity_unit": "asset",
                "contract_multiplier": None,
                "gross_notional": 100.0,
                "notional_currency": "USD",
                "account_cost_basis": 100.0,
            }
        ]
    )


def test_reconcile_once_clean_when_reconciliation_broker_is_user_scoped() -> None:
    """C1/N11 regression at the reconcile_once level using the REAL
    BrokerResolver: with user_id threaded, reconciliation resolves the per-user
    paper book holding the open position, so no missing_broker_position fires and
    no breaker opens."""
    resolver = BrokerResolver()
    _seed_resolver_with_open_position(resolver, user_id="user-1")
    engine = _ResolverBackedEngine(resolver)
    position_store = _FakePositionStore(
        [{"symbol": "BTC-USD", "side": "long", "quantity": 1.0, "current_price": 100.0}]
    )
    breach_store = _FakeBreachStore()
    worker = ReconciliationWorker(
        engine=engine,
        position_store=position_store,
        risk_breach_store=breach_store,
        interval_sec=300,
    )

    asyncio.run(worker.reconcile_once())

    assert engine.breaker_opens == []
    assert not any(r["rule_code"] == "missing_broker_position" for r in breach_store.records)
    assert engine.health_updates[-1][0] is True


def test_reconcile_once_blocks_when_wrong_account_book_is_resolved() -> None:
    """A different explicit account owns a different paper book."""
    resolver = BrokerResolver()
    _seed_resolver_with_open_position(resolver, user_id="user-1")
    engine = _ResolverBackedEngine(resolver, use_wrong_account=True)
    position_store = _FakePositionStore(
        [{"symbol": "BTC-USD", "side": "long", "quantity": 1.0, "current_price": 100.0}]
    )
    breach_store = _FakeBreachStore()
    worker = ReconciliationWorker(
        engine=engine,
        position_store=position_store,
        risk_breach_store=breach_store,
        interval_sec=300,
    )

    asyncio.run(worker.reconcile_once())

    assert len(engine.breaker_opens) == 1
    assert breach_store.records[0]["rule_code"] == "missing_broker_position"
    assert engine.health_updates[-1][0] is False


# ── Symbol-format normalization: the ledger stores "ETH/USD" (Instrument.canonical),
# the broker reports "ETHUSD" (signal symbol) / "ETH-USD" (live Coinbase). The SAME
# held position must not be double-flagged missing+phantom (which flapped the breaker
# every reconciliation cycle a position was held post-00:00-UTC-reset).


def test_classify_reconciliation_matches_across_slash_vs_noslash() -> None:
    findings = classify_reconciliation(
        local_positions=[
            {"symbol": "ETH/USD", "side": "long", "quantity": 6.22, "current_price": 1585.2}
        ],
        broker_positions=[
            {"symbol": "ETHUSD", "side": "long", "quantity": 6.22, "current_price": 1585.2}
        ],
        pending_orders={},
    )
    assert findings == []


def test_classify_reconciliation_matches_across_dash_format() -> None:
    findings = classify_reconciliation(
        local_positions=[
            {"symbol": "ETH/USD", "side": "long", "quantity": 1.0, "current_price": 100.0}
        ],
        broker_positions=[
            {"symbol": "ETH-USD", "side": "long", "quantity": 1.0, "current_price": 100.0}
        ],
        pending_orders={},
    )
    assert findings == []


def test_classify_reconciliation_still_flags_real_quantity_drift_across_formats() -> None:
    # Normalization collides the keys but must NOT mask genuine drift.
    findings = classify_reconciliation(
        local_positions=[
            {"symbol": "ETH/USD", "side": "long", "quantity": 6.22, "current_price": 100.0}
        ],
        broker_positions=[
            {"symbol": "ETHUSD", "side": "long", "quantity": 3.0, "current_price": 100.0}
        ],
        pending_orders={},
    )
    codes = {(f.severity, f.code) for f in findings}
    assert ("block", "position_quantity_mismatch") in codes
    assert ("block", "missing_broker_position") not in codes
    assert ("block", "phantom_broker_position") not in codes


def test_classify_reconciliation_still_flags_side_mismatch_across_formats() -> None:
    findings = classify_reconciliation(
        local_positions=[
            {"symbol": "ETH/USD", "side": "long", "quantity": 1.0, "current_price": 100.0}
        ],
        broker_positions=[
            {"symbol": "ETHUSD", "side": "short", "quantity": 1.0, "current_price": 100.0}
        ],
        pending_orders={},
    )
    assert ("block", "position_side_mismatch") in {(f.severity, f.code) for f in findings}


def test_classify_reconciliation_pending_downgrade_across_formats() -> None:
    # A pending order for the same instrument (no-slash) must still downgrade a
    # quantity diff on the slash-form local position from block to warn.
    findings = classify_reconciliation(
        local_positions=[
            {"symbol": "ETH/USD", "side": "long", "quantity": 1.0, "current_price": 100.0}
        ],
        broker_positions=[
            {"symbol": "ETHUSD", "side": "long", "quantity": 0.8, "current_price": 100.0}
        ],
        pending_orders={"o1": {"breaker_key": "b", "symbol": "ETHUSD", "side": "BUY"}},
    )
    codes = {(f.severity, f.code) for f in findings}
    assert ("warn", "position_quantity_pending") in codes
    assert ("block", "position_quantity_mismatch") not in codes


# ── Terminal-sync of stale pending orders (warn-flood fix): a 'submitted' row the
# broker no longer knows must be marked terminal on FIRST detection instead of
# re-flagging missing_broker_open_order (and writing a risk_breaches row) every
# reconciliation cycle forever.


class _FakePendingOrderRepo:
    def __init__(self, events: list[str] | None = None) -> None:
        self.terminal_calls: list[tuple[str, str]] = []
        self.resolved_results: list[BrokerOrderResult] = []
        self.events = events

    def resolve(self, order_id: str, result: BrokerOrderResult) -> None:
        if self.events is not None:
            self.events.append("pending")
        self.terminal_calls.append((order_id, result.status.value))
        self.resolved_results.append(result)


class _RecordingCanonicalStore:
    def __init__(self, events: list[str], *, failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure
        self.applied: list[dict[str, object]] = []

    def apply_broker_result(self, **kwargs: object) -> None:
        self.events.append("canonical")
        self.applied.append(kwargs)
        if self.failure is not None:
            raise self.failure


class _DurableCanonicalStore(_RecordingCanonicalStore):
    def __init__(
        self,
        events: list[str],
        *,
        result: BrokerOrderResult,
        fills: list[BrokerFill],
    ) -> None:
        super().__init__(events)
        self.result = result
        self.fills = list(fills)
        self.read_order_ids: list[int] = []

    def read_durable_result(
        self,
        *,
        order_id: int,
    ) -> tuple[BrokerOrderResult, list[BrokerFill]]:
        self.read_order_ids.append(order_id)
        return self.result, list(self.fills)


class _WorkingCanonicalStore(_RecordingCanonicalStore):
    def read_durable_result(
        self,
        *,
        order_id: int,
    ) -> None:
        del order_id


class _StatusBroker(_FakeBroker):
    """Fake broker with a configurable get_order_status response."""

    def __init__(  # type: ignore[no-untyped-def]
        self,
        positions,
        open_orders=None,
        status_result: BrokerOrderResult | None = None,
        status_error: Exception | None = None,
        fills: list[BrokerFill] | None = None,
    ) -> None:
        super().__init__(positions, open_orders)
        self._status_result = status_result
        self._status_error = status_error
        self._fills = list(fills or [])
        self.status_queries: list[str] = []

    async def get_order_status(self, order_id: str) -> BrokerOrderResult:
        self.status_queries.append(order_id)
        if self._status_error is not None:
            raise self._status_error
        assert self._status_result is not None
        return self._status_result

    async def get_fills(self, order_id: str) -> list[BrokerFill]:
        assert all(fill.order_id == order_id for fill in self._fills)
        return list(self._fills)


def _exact_fill(order_id: str, trade_id: str) -> BrokerFill:
    return BrokerFill(
        trade_id=trade_id,
        order_id=order_id,
        symbol="BTC-USDC",
        side="buy",
        quantity=Decimal("0.25"),
        price=Decimal("40000"),
        commission=Decimal("2.5"),
        commission_currency="USDC",
        timestamp=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        venue="coinbase",
    )


def _pending_payload(
    order_id: str,
    broker_order_id: str | None = None,
    *,
    canonical_order_id: int | None = None,
    instr_id: int | None = None,
) -> dict:
    return {
        "order_id": order_id,
        "broker_order_id": broker_order_id,
        "breaker_key": "strategy:user-1:SwingHighLowPMO:coinbase:coinbase-paper",
        "symbol": "BTC-USD",
        "side": "BUY",
        "status": "submitted",
        "canonical_order_id": canonical_order_id,
        "instr_id": instr_id,
        "broker": "coinbase",
    }


def _stale_order_worker(
    engine: _FakeEngine,
    breach_store: _FakeBreachStore,
    repo: _FakePendingOrderRepo,
    canonical_store: _RecordingCanonicalStore | None = None,
) -> ReconciliationWorker:
    return ReconciliationWorker(
        engine=engine,
        position_store=_FakePositionStore([]),
        risk_breach_store=breach_store,
        interval_sec=300,
        pending_order_repo=repo,
        canonical_execution_store=canonical_store,
    )


def _missing_open_order_records(breach_store: _FakeBreachStore) -> list[dict]:
    return [r for r in breach_store.records if r["rule_code"] == "missing_broker_open_order"]


def test_legacy_stale_paper_pending_order_expired_on_first_cycle_then_silent() -> None:
    # A legacy row without the durable lifecycle contract cannot progress after
    # PaperBroker memory is wiped, so a definitive unknown response expires it.
    broker = _StatusBroker([], status_result=BrokerOrderResult.rejected("Order not found: pre-1"))
    engine = _FakeEngine(broker)
    engine._pending = {"pre-1": _pending_payload("pre-1")}
    breach_store = _FakeBreachStore()
    repo = _FakePendingOrderRepo()
    worker = _stale_order_worker(engine, breach_store, repo)

    asyncio.run(worker.reconcile_once())

    # First cycle: transition is reported AND the row is expired.
    assert len(_missing_open_order_records(breach_store)) == 1
    assert repo.terminal_calls == [("pre-1", "expired")]
    assert engine.breaker_opens == []  # warn never opens the breaker

    # Second cycle: the fake engine still echoes the row (stale in-memory cache) —
    # no re-warn, no second risk_breaches row, no re-mark.
    asyncio.run(worker.reconcile_once())

    assert len(_missing_open_order_records(breach_store)) == 1
    assert repo.terminal_calls == [("pre-1", "expired")]


def test_reconciliation_preserves_durable_paper_order_after_restart() -> None:
    broker = _StatusBroker(
        [],
        status_result=BrokerOrderResult.rejected("Order not found: paper-resting-1"),
    )
    engine = _FakeEngine(broker)
    payload = _pending_payload(
        "local-resting-1",
        broker_order_id="paper-resting-1",
        canonical_order_id=801,
        instr_id=11,
    )
    payload.update(
        {
            "broker": "paper",
            "status": "partially_filled",
            "order_type": "bracket",
            "trigger_price": 39_000,
            "limit_price": 45_000,
            "market_data_source": "coinbase_live",
            "market_data_timeframe": "1m",
            "oco_group_id": "entry-1",
        }
    )
    engine._pending = {"paper-resting-1": payload}
    breach_store = _FakeBreachStore()
    repo = _FakePendingOrderRepo()
    worker = _stale_order_worker(
        engine,
        breach_store,
        repo,
        _WorkingCanonicalStore([]),
    )

    asyncio.run(worker.reconcile_once())
    asyncio.run(worker.reconcile_once())

    assert broker.status_queries == []
    assert repo.terminal_calls == []
    assert _missing_open_order_records(breach_store) == []
    assert engine.health_updates[-1] == (True, None)


def test_live_pending_order_cancelled_at_broker_marked_cancelled() -> None:
    broker = _StatusBroker(
        [],
        fills=[_exact_fill("uuid-fill-1", "trade-fill-1")],
        status_result=BrokerOrderResult(
            success=True,
            order_id="uuid-1",
            status=OrderStatus.CANCELLED,
            filled_quantity=0.25,
            filled_price=40000.0,
            commission=Decimal("2.5"),
            commission_currency="EUR",
        ),
    )
    engine = _FakeEngine(broker)
    engine._contexts[0]["environment"] = "live"
    engine._pending = {"uuid-1": _pending_payload("loc-1", broker_order_id="uuid-1")}
    breach_store = _FakeBreachStore()
    repo = _FakePendingOrderRepo()
    worker = _stale_order_worker(engine, breach_store, repo)

    asyncio.run(worker.reconcile_once())

    assert broker.status_queries == ["uuid-1"]  # queried by broker_order_id
    assert repo.terminal_calls == [("loc-1", "cancelled")]
    assert repo.resolved_results[0].filled_quantity == 0.25
    assert repo.resolved_results[0].filled_price == 40000.0
    assert repo.resolved_results[0].commission == Decimal("2.5")
    assert len(_missing_open_order_records(breach_store)) == 1  # transition reported


def test_reconciled_fill_commits_before_pending_row_becomes_terminal() -> None:
    events: list[str] = []
    broker = _StatusBroker(
        [],
        fills=[_exact_fill("uuid-fill-1", "trade-fill-1")],
        status_result=BrokerOrderResult(
            success=True,
            order_id="uuid-fill-1",
            status=OrderStatus.FILLED,
            filled_quantity=0.25,
            filled_price=40000.0,
            commission=Decimal("2.5"),
            commission_currency="USDC",
        ),
    )
    engine = _FakeEngine(broker)
    engine._contexts[0]["environment"] = "live"
    engine._pending = {
        "uuid-fill-1": _pending_payload(
            "loc-fill-1",
            broker_order_id="uuid-fill-1",
            canonical_order_id=501,
            instr_id=11,
        )
    }
    repo = _FakePendingOrderRepo(events)
    worker = _stale_order_worker(
        engine,
        _FakeBreachStore(),
        repo,
        _RecordingCanonicalStore(events),
    )

    asyncio.run(worker.reconcile_once())

    assert events == ["canonical", "pending"]


def test_canonical_fill_failure_leaves_pending_row_recoverable() -> None:
    events: list[str] = []
    broker = _StatusBroker(
        [],
        fills=[_exact_fill("uuid-fill-2", "trade-fill-2")],
        status_result=BrokerOrderResult(
            success=True,
            order_id="uuid-fill-2",
            status=OrderStatus.FILLED,
            filled_quantity=0.25,
            filled_price=40000.0,
            commission=Decimal("2.5"),
            commission_currency="USDC",
        ),
    )
    engine = _FakeEngine(broker)
    engine._contexts[0]["environment"] = "live"
    engine._pending = {
        "uuid-fill-2": _pending_payload(
            "loc-fill-2",
            broker_order_id="uuid-fill-2",
            canonical_order_id=502,
            instr_id=11,
        )
    }
    repo = _FakePendingOrderRepo(events)
    worker = _stale_order_worker(
        engine,
        _FakeBreachStore(),
        repo,
        _RecordingCanonicalStore(events, failure=RuntimeError("fill ledger unavailable")),
    )

    asyncio.run(worker.reconcile_once())

    assert events == ["canonical"]
    assert repo.terminal_calls == []
    assert engine.health_updates[-1][0] is False


def test_live_pending_order_unknown_at_broker_marked_expired() -> None:
    broker = _StatusBroker([], status_result=BrokerOrderResult.rejected("Order not found: uuid-2"))
    engine = _FakeEngine(broker)
    engine._contexts[0]["environment"] = "live"
    engine._pending = {"uuid-2": _pending_payload("loc-2", broker_order_id="uuid-2")}
    repo = _FakePendingOrderRepo()
    worker = _stale_order_worker(engine, _FakeBreachStore(), repo)

    asyncio.run(worker.reconcile_once())

    assert repo.terminal_calls == [("loc-2", "expired")]


def test_live_pending_order_api_error_keeps_row_and_warn() -> None:
    # Live safety: a transient status-API failure must NEVER terminal-sync the
    # order — the recurring warn is the real signal.
    broker = _StatusBroker([], status_error=RuntimeError("coinbase 503"))
    engine = _FakeEngine(broker)
    engine._contexts[0]["environment"] = "live"
    engine._pending = {"uuid-3": _pending_payload("loc-3", broker_order_id="uuid-3")}
    breach_store = _FakeBreachStore()
    repo = _FakePendingOrderRepo()
    worker = _stale_order_worker(engine, breach_store, repo)

    asyncio.run(worker.reconcile_once())
    asyncio.run(worker.reconcile_once())

    assert repo.terminal_calls == []  # row untouched
    assert len(_missing_open_order_records(breach_store)) == 2  # warn each cycle
    assert engine.breaker_opens == []  # per-order lookup failure never aborts the cycle


def test_fifo_drift_matches_across_symbol_formats() -> None:
    # FIFO ledger keyed ETHUSD vs broker reporting ETH/USD = same position, no drift.
    assert (
        classify_fifo_position_drift(
            fifo_positions={"ETHUSD": 1.0},
            broker_positions=[{"symbol": "ETH/USD", "side": "long", "quantity": 1.0}],
        )
        == []
    )


# ── Context rehydration after restart: reconciliation contexts are registered on
# order execution and were lost on every restart, leaving the worker blind (stale
# pending orders never re-checked or terminal-synced) until new traffic flowed.
# The tracker now rebuilds non-live contexts from the restored pending rows.


class _LoadingPendingOrderRepo:
    """Fake repo whose load() returns canned pending rows (real repo signature)."""

    def __init__(self, rows: dict[str, dict]) -> None:
        self._rows = rows

    def load(
        self,
        *,
        breaker_key: str | None = None,
        statuses: list[str] | None = None,
    ) -> dict[str, dict]:
        return {
            order_id: dict(payload)
            for order_id, payload in self._rows.items()
            if breaker_key is None or payload.get("breaker_key") == breaker_key
        }


def _restored_row(
    order_id: str,
    *,
    breaker_key: str,
    environment: str = "paper",
    broker: str = "paper",
    user_id: str = "demo_peer",
) -> dict:
    return {
        "order_id": order_id,
        "breaker_key": breaker_key,
        "broker": broker,
        "broker_environment": environment,
        "credential_ref": f"{broker}-{environment}",
        "user_id": user_id,
        "symbol": "BTC-USD",
        "side": "SELL",
        "status": "submitted",
    }


def test_tracker_rehydrates_paper_contexts_from_pending_orders() -> None:
    key = "strategy:demo_peer:test_strategy_alpha_v1:paper:paper-paper"
    repo = _LoadingPendingOrderRepo(
        {
            "a-1": _restored_row("a-1", breaker_key=key),
            # Second row on the SAME breaker key must not duplicate the context.
            "a-2": _restored_row("a-2", breaker_key=key),
        }
    )
    tracker = ReconciliationTracker(pending_order_repo=repo)

    contexts = tracker.iter_contexts()
    assert len(contexts) == 1
    ctx = contexts[0]
    assert ctx["breaker_key"] == key
    assert ctx["user_id"] == "demo_peer"
    assert ctx["strategy_id"] == "test_strategy_alpha_v1"
    assert ctx["broker_code"] == "paper"
    assert ctx["environment"] == "paper"


def test_tracker_discovers_no_traffic_account_from_canonical_open_position() -> None:
    db = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(db)
    sessions = sessionmaker(db, expire_on_commit=False)
    with sessions() as session:
        session.add_all(
            [
                User(
                    user_id="user-1",
                    email="user-1@example.invalid",
                    base_ccy="USD",
                ),
                Strategy(
                    strategy_id="swing_high_low_pmo_v1",
                    strategy_name="Swing High Low PMO",
                    asset_class="crypto",
                ),
                Instrument(
                    instr_id=1,
                    asset_class="crypto",
                    canonical="BTC/USDC",
                    settlement_currency="USDC",
                ),
            ]
        )
        broker = Broker(code="paper", name="Local paper", capabilities={})
        session.add(broker)
        session.flush()
        session.add(
            LinkedBrokerAccount(
                account_id=101,
                user_id="user-1",
                broker_id=broker.broker_id,
                environment="paper",
                display_name="BTC canary",
                base_ccy="USD",
                paper_initial_equity=Decimal("100000"),
                paper_initial_cash=Decimal("100000"),
                status="connected",
            )
        )
        session.add(
            CanonicalSignal(
                signal_id=501,
                strategy_id="swing_high_low_pmo_v1",
                instr_id=1,
                action="long",
                external_signal_id="real-history-btc-usdc-long",
                ts=datetime(2026, 7, 25, 12, 0),
            )
        )
        session.flush()
        intent = PersistedOrderIntent(
            user_id="user-1",
            account_id=101,
            strategy_id="swing_high_low_pmo_v1",
            canonical_signal_id=501,
            idempotency_key="no-traffic-open-position",
            side="BUY",
            execution_mode="paper",
            broker_environment="paper",
            method="SPOT",
            payload={"symbol": "BTC-USDC", "quantity": "1"},
            status="routed",
        )
        session.add(intent)
        session.flush()
        order = Order(
            intent_id=intent.intent_id,
            broker_id=broker.broker_id,
            account_id=101,
            settlement_currency="USDC",
            client_order_id="no-traffic-open-position",
            broker_order_ref="paper-order-1",
            state="filled",
        )
        session.add(order)
        session.flush()
        session.add(
            Execution(
                order_id=order.order_id,
                instr_id=1,
                fill_ts=datetime(2026, 7, 25, 12, 0),
                qty=Decimal("1"),
                price=Decimal("100000"),
                fee_ccy="USDC",
                fee_amount=Decimal("0"),
                venue="paper",
                trade_id="paper-trade-1",
            )
        )
        session.commit()

    tracker = ReconciliationTracker(
        pending_order_repo=PendingOrderRepository(sessions),
        session_factory=sessions,
    )

    contexts = tracker.iter_contexts()
    assert len(contexts) == 1
    assert contexts[0]["broker_account_id"] == 101
    assert contexts[0]["settlement_currency"] == "USDC"
    assert tracker.status()["initial_reconciliation_complete"] is False
    assert tracker.is_healthy() is False

    tracker.mark_health(healthy=True)
    assert tracker.status()["initial_reconciliation_complete"] is True
    assert tracker.is_healthy() is True


def test_tracker_does_not_rehydrate_live_contexts() -> None:
    # A live context without its credential dict would fail broker resolution
    # every cycle and flip reconciliation unhealthy (blocking the live gate) —
    # live rows wait for real live traffic to re-register a working context.
    repo = _LoadingPendingOrderRepo(
        {
            "l-1": _restored_row(
                "l-1",
                breaker_key="strategy:demo_user:test_strategy_alpha_v1:coinbase:coinbase-live-main",
                environment="live",
                broker="coinbase",
                user_id="demo_user",
            ),
            "junk": {"order_id": "junk", "status": "submitted"},  # no breaker_key
        }
    )
    tracker = ReconciliationTracker(pending_order_repo=repo)

    assert tracker.iter_contexts() == []


def test_restart_closes_pending_projection_from_durable_paper_fill() -> None:
    """A crash after the canonical commit must not expire the recovered fill."""
    events: list[str] = []
    broker_order_id = "paper-fill-1"
    fill = _exact_fill(broker_order_id, "paper-trade-1")
    durable_result = BrokerOrderResult(
        success=True,
        order_id=broker_order_id,
        status=OrderStatus.FILLED,
        filled_quantity=0.25,
        filled_price=40000.0,
        commission=Decimal("2.5"),
        commission_currency="USDC",
    )
    canonical_store = _DurableCanonicalStore(
        events,
        result=durable_result,
        fills=[fill],
    )
    broker = _StatusBroker(
        [],
        status_result=BrokerOrderResult.rejected("Order not found: pre-crash"),
    )
    engine = _FakeEngine(broker)
    engine._pending = {
        "pre-crash": _pending_payload(
            "pre-crash",
            canonical_order_id=801,
            instr_id=11,
        )
    }
    tracker = ReconciliationTracker(
        pending_order_repo=_LoadingPendingOrderRepo({}),
        canonical_execution_store=canonical_store,
    )
    engine._reconciliation_tracker = tracker
    repo = _FakePendingOrderRepo(events)
    worker = ReconciliationWorker(
        engine=engine,
        position_store=_FakePositionStore([]),
        risk_breach_store=_FakeBreachStore(),
        interval_sec=300,
        pending_order_repo=repo,
    )

    asyncio.run(worker.reconcile_once())

    assert canonical_store.read_order_ids == [801]
    assert broker.status_queries == []
    assert events == ["canonical", "pending"]
    assert canonical_store.applied[0]["fills"] == [fill]
    assert repo.terminal_calls == [("pre-crash", "filled")]
    assert repo.resolved_results[0].order_id == broker_order_id
    assert engine.health_updates[-1] == (True, None)


def test_strategy_id_parse_falls_back_to_unknown() -> None:
    repo = _LoadingPendingOrderRepo(
        {
            "b-1": _restored_row("b-1", breaker_key="broker:paper:paper"),
        }
    )
    tracker = ReconciliationTracker(pending_order_repo=repo)

    contexts = tracker.iter_contexts()
    assert len(contexts) == 1
    assert contexts[0]["strategy_id"] == "unknown"


def test_submit_path_commits_canonical_result_before_resolving_pending_row() -> None:
    events: list[str] = []

    class _Repo(_LoadingPendingOrderRepo):
        def resolve(self, order_id: str, _result: BrokerOrderResult) -> str:
            events.append("pending")
            return order_id

    tracker = ReconciliationTracker(
        pending_order_repo=_Repo({}),
        canonical_execution_store=_RecordingCanonicalStore(events),
    )
    result = BrokerOrderResult(
        success=True,
        order_id="broker-1",
        status=OrderStatus.FILLED,
        filled_quantity=1,
        filled_price=100,
    )

    assert (
        tracker.resolve_order(
            "local-1",
            result,
            fills=[_exact_fill("broker-1", "paper-trade-1")],
            canonical_order_id=701,
            instr_id=11,
        )
        == "local-1"
    )
    assert events == ["canonical", "pending"]


def test_cancel_working_transitions_only_non_terminal_rows() -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from lib_application.db.models import Base, PendingOrder

    db = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(db)
    sessions = sessionmaker(db, expire_on_commit=False)
    now = datetime.now(tz=UTC).replace(tzinfo=None)

    def _row(order_id: str, status: str) -> PendingOrder:
        return PendingOrder(
            order_id=order_id,
            client_order_id=f"{order_id}:client",
            user_id="user-1",
            signal_id="sig-1",
            instr_id=1,
            broker_account_id=101,
            symbol="BTC-USDC",
            settlement_currency="USD",
            side="SELL",
            order_type="bracket",
            quantity=Decimal("1"),
            purpose="bracket",
            reduce_only=True,
            execution_mode="paper",
            broker="paper",
            broker_environment="paper",
            status=status,
            broker_order_id=f"{order_id}:broker",
            strategy_id="swing_high_low_pmo_v1",
            created_at=now,
        )

    with sessions() as session:
        session.add_all([_row("working-1", "working"), _row("filled-1", "filled")])
        session.commit()
    repo = PendingOrderRepository(sessions)

    assert repo.cancel_working("working-1:broker", reason="Superseded by close") is True
    assert repo.cancel_working("filled-1", reason="Superseded by close") is False
    assert repo.cancel_working("missing", reason="Superseded by close") is False

    with sessions() as session:
        cancelled = session.get(PendingOrder, "working-1")
        assert cancelled.status == "cancelled"
        assert cancelled.error_message == "Superseded by close"
        assert cancelled.resolved_at is not None
        assert session.get(PendingOrder, "filled-1").status == "filled"
