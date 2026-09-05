"""Paper-broker restart-safety (G3).

A PaperBroker is process-memory only, so without rehydration a service restart
would reset every user's paper book to flat + full starting
balance — making open positions unclosable (the CLOSE path reads
get_account_info().positions) and letting an empty reconciliation snapshot wipe
the persisted ledger. These guard the DB-as-source-of-truth fix.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from execution_engine.broker_resolver import (
    BrokerResolver,
    PaperAccountStateError,
)
from execution_engine.brokers.base import PositionValuationError
from execution_engine.brokers.paper import PaperBroker
from execution_engine.config import BrokerType
from execution_engine.execution_position_store import ExecutionPositionStore
from execution_engine.models import OrderCurrencyContext, OrderIntent
from execution_engine.paper_ledger_recovery import (
    PaperLedgerPosition,
    PaperLedgerSeed,
)
from lib_application.db.models import Base, Broker, Instrument, LinkedBrokerAccount, User

_OPEN_LONG = {
    "symbol": "BTCUSD",
    "side": "long",
    "quantity": 0.5,
    "entry_price": 100_000.0,
    "current_price": 101_000.0,
    "quantity_unit": "asset",
    "contract_multiplier": None,
    "gross_notional": 50_500.0,
    "notional_currency": "USD",
    "account_cost_basis": 50_000.0,
}


def _paper_broker(
    *,
    starting_balance: float = 100_000.0,
    available_cash: float | None = None,
) -> PaperBroker:
    return PaperBroker(
        account_id="paper-test-account",
        starting_balance=starting_balance,
        available_cash=starting_balance if available_cash is None else available_cash,
        currency="USD",
    )


def _open_position_seed() -> PaperLedgerPosition:
    return PaperLedgerPosition(
        symbol="BTCUSD",
        side="LONG",
        quantity=Decimal("0.5"),
        entry_price=Decimal("100000"),
        current_price=Decimal("101000"),
        quantity_unit="asset",
        contract_multiplier=None,
        gross_notional=Decimal("50500"),
        notional_currency="USD",
        account_cost_basis=Decimal("50000"),
    )


def _seed_ledger(
    *,
    account_id: int,
    **_kwargs,
) -> PaperLedgerSeed:
    if account_id == 101:
        return PaperLedgerSeed(
            initial_equity=Decimal("100000"),
            cash_balance=Decimal("50000"),
            realized_pnl=Decimal("0"),
            currency="USD",
            positions=(_open_position_seed(),),
            execution_count=1,
            last_execution_id=1,
        )
    return PaperLedgerSeed(
        initial_equity=Decimal("100000"),
        cash_balance=Decimal("100000"),
        realized_pnl=Decimal("0"),
        currency="USD",
        positions=(),
        execution_count=0,
        last_execution_id=None,
    )


def _session_local():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed_paper_universe(session_local) -> None:
    with session_local() as s:
        s.add(User(user_id="u1", email="u1@example.com", full_name="U1", base_ccy="USD"))
        broker = Broker(code="paper", name="Paper", capabilities={"assets": ["crypto"]})
        s.add(broker)
        s.flush()
        s.add(
            LinkedBrokerAccount(
                account_id=1,
                user_id="u1",
                broker_id=broker.broker_id,
                environment="paper",
                display_name="U1 Paper",
                base_ccy="USD",
                paper_initial_equity=100_000,
                paper_initial_cash=100_000,
                status="connected",
            )
        )
        s.add(
            Instrument(
                asset_class="crypto",
                canonical="BTCUSD",
                exchange="paper",
                settlement_currency="USD",
            )
        )
        s.commit()


# ── PaperBroker.load_positions (no DB) ───────────────────────────────────────


def test_load_positions_seeds_open_book_and_exposure() -> None:
    broker = _paper_broker()
    broker.load_positions([_OPEN_LONG])

    info = asyncio.run(broker.get_account_info())
    symbols = {p.symbol for p in info.positions}
    assert "BTCUSD" in symbols
    # LONG consumed cash, so available balance reflects deployed capital.
    assert broker._balance == 100_000.0 - (100_000.0 * 0.5)

    # Idempotent: re-seeding a warm broker is a no-op.
    broker.load_positions([_OPEN_LONG])
    assert len(asyncio.run(broker.get_positions())) == 1


def test_load_positions_rejects_unknown_side_without_mutation() -> None:
    broker = _paper_broker()
    invalid = {
        **_OPEN_LONG,
        "symbol": "ETHUSD",
        "side": "mystery",
    }

    with pytest.raises(ValueError, match="Unknown position side"):
        broker.load_positions([_OPEN_LONG, invalid])

    assert asyncio.run(broker.get_positions()) == []
    assert broker._balance == 100_000.0


def test_load_positions_preserves_contract_valuation_semantics() -> None:
    broker = _paper_broker()
    broker.load_positions(
        [
            {
                "symbol": "BTC-USDC:2026-08-28:50000:CALL",
                "side": "long",
                "quantity": 2,
                "entry_price": 5.0,
                "current_price": 6.0,
                "quantity_unit": "contracts",
                "contract_multiplier": 100.0,
                "gross_notional": 1_200.0,
                "notional_currency": "USD",
                "account_cost_basis": 1_000.0,
            }
        ]
    )

    position = asyncio.run(broker.get_positions())[0]
    assert position.quantity_unit == "contracts"
    assert position.contract_multiplier == 100.0
    assert position.gross_notional == 1_200.0
    assert position.notional_currency == "USD"
    assert position.unrealized_pnl == 200.0
    assert broker._balance == 99_000.0


def test_load_contract_position_requires_explicit_multiplier_without_mutation() -> None:
    broker = _paper_broker()

    with pytest.raises(ValueError, match="positive multiplier"):
        broker.load_positions(
            [
                {
                    **_OPEN_LONG,
                    "quantity_unit": "contracts",
                }
            ]
        )

    assert asyncio.run(broker.get_positions()) == []
    assert broker._balance == 100_000.0


def test_load_position_refuses_to_infer_account_currency_basis() -> None:
    broker = _paper_broker()
    missing_basis = dict(_OPEN_LONG)
    missing_basis.pop("account_cost_basis")

    with pytest.raises(ValueError, match="explicit account-currency basis"):
        broker.load_positions([missing_basis])

    assert asyncio.run(broker.get_positions()) == []
    assert broker._balance == 100_000.0


def test_rehydrated_long_can_be_closed() -> None:
    """The core Critical: a position recovered after restart must be closeable."""
    broker = _paper_broker()
    broker.load_positions([_OPEN_LONG])

    result = asyncio.run(
        broker.submit_order(
            OrderIntent(
                broker_code="paper",
                symbol="BTCUSD",
                side="SELL",
                quantity=0.5,
                order_type="market",
                currency_context=OrderCurrencyContext(
                    account_currency="USD",
                    settlement_currency="USD",
                    account_to_settlement_rate=Decimal("1"),
                    requested_at=datetime(2026, 7, 15, tzinfo=UTC),
                    observed_at=datetime(2026, 7, 15, tzinfo=UTC),
                    source="identity",
                ),
            )
        )
    )
    assert result.status.value in {"filled", "FILLED"} or result.filled_quantity == 0.5
    assert asyncio.run(broker.get_positions()) == []


def test_resolver_isolates_paper_books_for_two_accounts_of_same_user() -> None:
    seeded_accounts: list[int] = []

    def seed_ledger(**kwargs) -> PaperLedgerSeed:  # type: ignore[no-untyped-def]
        account_id = int(kwargs["account_id"])
        seeded_accounts.append(account_id)
        return _seed_ledger(account_id=account_id)

    resolver = BrokerResolver(ledger_seeder=seed_ledger)

    async def resolve(account_id: int) -> PaperBroker:
        broker = await resolver.get_broker(
            broker_type=BrokerType.PAPER,
            environment="paper",
            credential_ref="shared-paper-ref",
            user_id="u1",
            broker_account_id=account_id,
            account_currency="USD",
            settlement_currency="USD",
        )
        assert isinstance(broker, PaperBroker)
        return broker

    first = asyncio.run(resolve(101))
    first_again = asyncio.run(resolve(101))
    second = asyncio.run(resolve(202))

    assert first_again is first
    assert second is not first
    assert seeded_accounts == [101, 202]
    assert [position.symbol for position in asyncio.run(first.get_positions())] == ["BTCUSD"]
    assert asyncio.run(second.get_positions()) == []


def test_resolver_blocks_and_does_not_cache_when_paper_rehydration_fails() -> None:
    fail = True
    calls = 0

    def seed_ledger(**_kwargs) -> PaperLedgerSeed:  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if fail:
            raise SQLAlchemyError("position store unavailable")
        return _seed_ledger(account_id=101)

    resolver = BrokerResolver(ledger_seeder=seed_ledger)

    async def resolve() -> PaperBroker:
        broker = await resolver.get_broker(
            broker_type=BrokerType.PAPER,
            environment="paper",
            credential_ref="paper-ref",
            user_id="u1",
            broker_account_id=101,
            account_currency="USD",
            settlement_currency="USD",
        )
        assert isinstance(broker, PaperBroker)
        return broker

    with pytest.raises(PaperAccountStateError, match="could not be rehydrated"):
        asyncio.run(resolve())
    assert resolver.cache == {}
    assert resolver.is_paper_rehydration_healthy() is False

    fail = False
    recovered = asyncio.run(resolve())
    assert isinstance(recovered, PaperBroker)
    assert calls == 2
    assert resolver.is_paper_rehydration_healthy() is True
    assert [position.symbol for position in asyncio.run(recovered.get_positions())] == ["BTCUSD"]


def test_resolver_blocks_malformed_canonical_ledger_seed() -> None:
    invalid_position = replace(
        _open_position_seed(),
        account_cost_basis=Decimal("0"),
    )
    invalid_seed = PaperLedgerSeed(
        initial_equity=Decimal("100000"),
        cash_balance=Decimal("50000"),
        realized_pnl=Decimal("0"),
        currency="USD",
        positions=(invalid_position,),
        execution_count=1,
        last_execution_id=1,
    )
    resolver = BrokerResolver(ledger_seeder=lambda **_kwargs: invalid_seed)

    with pytest.raises(PaperAccountStateError, match="could not be rehydrated"):
        asyncio.run(
            resolver.get_broker(
                BrokerType.PAPER,
                "paper",
                "paper-ref",
                user_id="u1",
                broker_account_id=101,
                account_currency="USD",
                settlement_currency="USD",
            )
        )

    assert resolver.cache == {}
    assert resolver.is_paper_rehydration_healthy() is False


def test_alternate_broker_paper_route_is_owned_by_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def seed_ledger(**kwargs) -> PaperLedgerSeed:  # type: ignore[no-untyped-def]
        observed.update(kwargs)
        return PaperLedgerSeed(
            initial_equity=Decimal("75000"),
            cash_balance=Decimal("75000"),
            realized_pnl=Decimal("0"),
            currency="EUR",
            positions=(),
            execution_count=0,
            last_execution_id=None,
        )

    def reject_bridge(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Local paper routing must not construct BrokerBridge")

    monkeypatch.setattr("execution_engine.broker_resolver.BrokerBridge", reject_bridge)
    resolver = BrokerResolver(
        ledger_seeder=seed_ledger,
        use_local_paper_broker=True,
    )

    broker = asyncio.run(
        resolver.get_broker(
            BrokerType.DELTA,
            "paper",
            "delta-paper-account",
            user_id="u1",
            broker_account_id=303,
            account_currency="EUR",
            settlement_currency="USD",
        )
    )

    assert isinstance(broker, PaperBroker)
    account = asyncio.run(broker.get_account_info())
    assert account.account_id == "303"
    assert account.currency == "EUR"
    assert account.equity == 75_000.0
    assert observed["broker_code"] == "delta"
    assert resolver.is_paper_rehydration_healthy() is True


def test_resolver_rejects_missing_account_identity_before_cache_lookup() -> None:
    resolver = BrokerResolver()

    with pytest.raises(ValueError, match="positive broker_account_id"):
        asyncio.run(
            resolver.get_broker(
                BrokerType.PAPER,
                "paper",
                "paper-account",
                user_id="u1",
                broker_account_id=0,
                account_currency="USD",
                settlement_currency="USD",
                paper_initial_equity=100_000.0,
                paper_initial_cash=100_000.0,
            )
        )

    assert resolver.cache == {}


# ── sync_positions empty-snapshot guard (sqlite) ─────────────────────────────


def test_empty_snapshot_prune_is_guarded() -> None:
    session_local = _session_local()
    _seed_paper_universe(session_local)
    store = ExecutionPositionStore(session_factory=session_local)
    kw = {
        "user_id": "u1",
        "broker_code": "paper",
        "account_id": 1,
        "environment": "paper",
    }

    store.sync_positions(**kw, positions=[_OPEN_LONG])
    assert store.list_positions(**kw)

    # Non-authoritative empty snapshot (e.g. cold broker on reconciliation):
    # must NOT wipe the ledger.
    store.sync_positions(**kw, positions=[])
    assert store.list_positions(**kw), "guarded empty snapshot must not prune"

    # Authoritative post-fill empty snapshot: genuine flat, prunes.
    store.sync_positions(**kw, positions=[], allow_empty_prune=True)
    assert not store.list_positions(**kw)


def test_position_store_rejects_unknown_side_without_persisting() -> None:
    session_local = _session_local()
    _seed_paper_universe(session_local)
    store = ExecutionPositionStore(session_factory=session_local)
    kw = {
        "user_id": "u1",
        "broker_code": "paper",
        "account_id": 1,
        "environment": "paper",
    }

    with pytest.raises(ValueError, match="Unknown position side"):
        store.sync_positions(
            **kw,
            positions=[{**_OPEN_LONG, "side": "mystery"}],
        )

    assert store.list_positions(**kw) == []


def test_position_store_rejects_unresolved_instrument_without_partial_projection() -> None:
    session_local = _session_local()
    _seed_paper_universe(session_local)
    store = ExecutionPositionStore(session_factory=session_local)
    kw = {
        "user_id": "u1",
        "broker_code": "paper",
        "account_id": 1,
        "environment": "paper",
    }

    with pytest.raises(PositionValuationError, match="canonical instrument"):
        store.sync_positions(
            **kw,
            positions=[
                _OPEN_LONG,
                {
                    **_OPEN_LONG,
                    "symbol": "UNREGISTERED-CONTRACT",
                },
            ],
            allow_empty_prune=True,
        )

    assert store.list_positions(**kw) == []
