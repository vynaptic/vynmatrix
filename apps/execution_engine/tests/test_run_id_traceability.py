"""DB-1: run_id is persisted only as a queryable execution_logs column, so RCA
can join the execution row into the run chain with one ``WHERE run_id = :x``."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from lib_application.db.models import Base
from lib_application.db.models import ExecutionLog as ExecutionLogModel
from lib_infrastructure import SQLAlchemyExecutionRepository
from lib_strategy.domain.entities import ExecutionLog


def _session_local():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _log(run_id: str | None) -> ExecutionLog:
    return ExecutionLog(
        log_id="log-1",
        user_id="user-1",
        account_id=101,
        strategy_id="swing_v1",
        signal_type="long",
        execution_mode="spot",
        canonical_signal_id=None,
        status="executed",
        run_id=run_id,
    )


def test_execution_log_persists_and_round_trips_run_id() -> None:
    session_local = _session_local()
    repo = SQLAlchemyExecutionRepository(session_local)

    repo.log_execution(_log("run-abc"))

    # Round-trips through the domain entity.
    fetched = repo.get_execution_log("log-1")
    assert fetched is not None
    assert fetched.run_id == "run-abc"
    assert fetched.account_id == 101

    # And is set on the queryable ORM column.
    with session_local() as session:
        row = session.query(ExecutionLogModel).filter_by(log_id="log-1").one()
        assert row.run_id == "run-abc"
        assert row.account_id == 101


def test_execution_log_run_id_is_optional() -> None:
    session_local = _session_local()
    repo = SQLAlchemyExecutionRepository(session_local)

    repo.log_execution(_log(None))

    fetched = repo.get_execution_log("log-1")
    assert fetched is not None
    assert fetched.run_id is None
