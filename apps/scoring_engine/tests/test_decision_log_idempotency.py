"""SC-1: the scoring engine pre-creates the execution_decision_logs row, and it
must derive idempotency_key from the SAME stable identity the execution engine
uses (external_signal_id). Otherwise a redelivered signal (fresh run_id ->
fresh signal_id) writes a second decision row and the execution claim never
matches it, leaving an orphan + a possible duplicate order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import SQLAlchemyError

from lib_application.db.models import CanonicalSignal, ExecutionDecisionLog
from lib_strategy.scoring.types import ExecutionDecision
from scoring_engine.storage import AppScoreStore


def _store() -> AppScoreStore:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    with store.get_session() as session:
        session.add_all(
            [
                CanonicalSignal(
                    signal_id=1,
                    strategy_id="swing_v1",
                    instr_id=1,
                    action="long",
                    external_signal_id="ext-1",
                    ts=datetime.now(tz=UTC),
                ),
                CanonicalSignal(
                    signal_id=2,
                    strategy_id="swing_v1",
                    instr_id=1,
                    action="long",
                    external_signal_id="ext-2",
                    ts=datetime.now(tz=UTC),
                ),
            ]
        )
        session.commit()
    return store


def _decision(signal_id: str, *, broker_account_id: int = 1) -> ExecutionDecision:
    return ExecutionDecision(
        user_id="user-1",
        binding_id=1,
        instrument_id=1,
        broker_account_id=broker_account_id,
        asset_score=Decimal("3.0"),
        should_execute=True,
        execution_mode="spot",
        direction="long",
        signal_id=signal_id,
        strategy_id="swing_v1",
        symbol="BTCUSD",
    )


def _rows(store: AppScoreStore) -> list[ExecutionDecisionLog]:
    with store.get_session() as session:
        return list(session.query(ExecutionDecisionLog).all())


def test_redelivery_with_same_external_id_upserts_one_decision_row() -> None:
    store = _store()
    # Same bar, redelivered: identical external_signal_id but a fresh signal_id.
    store.persist_decision_log(
        _decision("sig-A"),
        signal_action="long",
        dedup_identity="ext-1",
    )
    store.persist_decision_log(
        _decision("sig-B"),
        signal_action="long",
        dedup_identity="ext-1",
    )

    rows = _rows(store)
    assert len(rows) == 1  # upsert matched the stable idempotency_key
    assert rows[0].idempotency_key is not None
    assert rows[0].canonical_signal_id == 1
    assert rows[0].broker_account_id == 1
    assert rows[0].lineage_schema_version == "v1"


def test_distinct_external_ids_write_distinct_rows() -> None:
    store = _store()
    store.persist_decision_log(_decision("sig-A"), signal_action="long", dedup_identity="ext-1")
    store.persist_decision_log(_decision("sig-B"), signal_action="long", dedup_identity="ext-2")
    assert len(_rows(store)) == 2


def test_same_signal_can_route_once_to_each_broker_account() -> None:
    store = _store()
    store.persist_decision_log(
        _decision("sig-A", broker_account_id=1),
        signal_action="long",
        dedup_identity="ext-1",
    )
    store.persist_decision_log(
        _decision("sig-A", broker_account_id=2),
        signal_action="long",
        dedup_identity="ext-1",
    )

    rows = _rows(store)
    assert len(rows) == 2
    assert len({row.idempotency_key for row in rows}) == 2
    assert {row.broker_account_id for row in rows} == {1, 2}
    assert {row.canonical_signal_id for row in rows} == {1}


def test_decision_persistence_refuses_unknown_canonical_identity() -> None:
    store = _store()

    with pytest.raises(ValueError, match="requires an exact canonical signal"):
        store.persist_decision_log(
            _decision("sig-A"),
            signal_action="long",
            dedup_identity="unknown-external-id",
        )


@pytest.mark.parametrize("dedup_identity", ["", "   "])
def test_decision_persistence_refuses_missing_external_identity(
    dedup_identity: str,
) -> None:
    store = _store()
    with pytest.raises(ValueError, match="requires user, external signal, and action identity"):
        store.persist_decision_log(
            _decision("sig-A"),
            signal_action="long",
            dedup_identity=dedup_identity,
        )

    assert _rows(store) == []


class _RaisingSession:
    def query(self, *_a: object, **_k: object) -> object:
        raise SQLAlchemyError("boom")

    def add(self, *_a: object, **_k: object) -> None: ...


class _RaisingCM:
    def __enter__(self) -> _RaisingSession:
        return _RaisingSession()

    def __exit__(self, *_a: object) -> bool:
        return False


def test_persist_decision_log_reraises_db_error(monkeypatch) -> None:
    # OB-2: a DB error must propagate (so the ingest unit_of_work rolls back
    # atomically and reports correctly), not be swallowed into a poisoned txn.
    store = _store()
    monkeypatch.setattr(store, "_session", lambda: _RaisingCM())
    with pytest.raises(SQLAlchemyError):
        store.persist_decision_log(
            _decision("sig-A"),
            signal_action="long",
            dedup_identity="ext-1",
        )


def test_decision_log_persists_run_id() -> None:
    # DB-1: the decision row carries run_id so RCA can join it into the run chain.
    store = _store()
    store.persist_decision_log(
        _decision("sig-A"),
        signal_action="long",
        dedup_identity="ext-1",
        run_id="run-xyz",
    )
    rows = _rows(store)
    assert len(rows) == 1
    assert rows[0].run_id == "run-xyz"
