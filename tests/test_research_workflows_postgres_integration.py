"""Opt-in role and fencing proof against an isolated migrated PostgreSQL database.

RESEARCH_TEST_DATABASE_URL must use its migration identity and have no owner or
research jobs. Every change rolls back; this module creates no database or role.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from lib_application.db.models import User
from lib_application.services import research_authoring as drafts
from lib_application.services import research_jobs as jobs

pytestmark = pytest.mark.integration
OWNER = "research-acceptance-owner"
PEER = "research-acceptance-peer"


@pytest.fixture
def database() -> Iterator[sa.Connection]:
    url = os.environ.get("RESEARCH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("RESEARCH_TEST_DATABASE_URL requires isolated PostgreSQL acceptance")
    engine = sa.create_engine(url, hide_parameters=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("Research authority proof requires PostgreSQL")
    try:
        with engine.connect() as connection, connection.begin():
            assert (
                connection.scalar(sa.text("SELECT count(*) FROM users WHERE is_deployment_owner"))
                == 0
            ), "Use an isolated unconfigured acceptance database"
            assert connection.scalar(sa.text("SELECT count(*) FROM research_jobs")) == 0, (
                "Use an isolated acceptance database without research jobs"
            )
            with Session(bind=connection, join_transaction_mode="create_savepoint") as store:
                store.add_all(
                    [
                        User(
                            user_id=OWNER,
                            email="owner@research.invalid",
                            base_ccy="USD",
                            is_deployment_owner=True,
                        ),
                        User(user_id=PEER, email="peer@research.invalid", base_ccy="USD"),
                    ]
                )
                store.commit()
            yield connection
            connection.rollback()
    finally:
        engine.dispose()


def role(connection: sa.Connection, name: str, owner: str = OWNER) -> None:
    assert name in {"vm_backend", "vm_research", "maintenance"}
    connection.execute(sa.text("RESET ROLE"))
    if name != "maintenance":
        connection.execute(sa.text("SET LOCAL ROLE " + name))
    connection.execute(
        sa.text("SELECT set_config('app.current_tenant',:owner,true)"), {"owner": owner}
    )


def enqueue(connection: sa.Connection) -> dict[str, Any]:
    role(connection, "vm_backend")
    with Session(bind=connection, join_transaction_mode="create_savepoint") as store:
        revision = drafts.create_draft(
            store,
            OWNER,
            request_id="draft",
            name="Fixture",
            source="class UserStrategy:\n    pass\n",
            config={"class_name": "UserStrategy", "strategy_id": "Acceptance", "parameters": {}},
        )
        job = jobs.enqueue_job(
            store,
            OWNER,
            request_id="validate",
            kind="validate_strategy",
            inputs={"revision_id": revision["revision_id"]},
        )
        store.commit()
        return job


def claimed(connection: sa.Connection) -> dict[str, Any]:
    enqueue(connection)
    role(connection, "vm_research")
    with Session(bind=connection, join_transaction_mode="create_savepoint") as store:
        generation = jobs.supervisor_status(store)["generation"]
        jobs.supervisor_heartbeat(
            store,
            worker_id="worker",
            generation=generation,
            image_hash="a" * 64,
            bundle_hash="b" * 64,
        )
        claim = jobs.claim_job(
            store,
            worker_id="worker",
            generation=generation,
            image_hash="a" * 64,
            bundle_hash="b" * 64,
        )
        assert claim is not None
        store.commit()
        return claim


def test_postgres_research_role_has_no_trading_or_maintenance_authority(
    database: sa.Connection,
) -> None:
    for table in (
        "orders",
        "executions",
        "order_intents",
        "prices",
        "strategy_versions",
        "user_strategy_bindings",
        "broker_credentials",
        "users",
    ):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert (
                database.scalar(
                    sa.text("SELECT has_table_privilege('vm_research',:table,:privilege)"),
                    {"table": table, "privilege": privilege},
                )
                is False
            )
    role(database, "vm_research")
    with pytest.raises(sa.exc.DBAPIError) as error, database.begin_nested():
        database.execute(
            sa.text("UPDATE research_control SET generation=:generation"),
            {"generation": str(uuid4())},
        )
    assert getattr(error.value.orig, "pgcode", None) == "42501"
    with (
        pytest.raises(jobs.ResearchWorkflowError, match="maintenance database authority"),
        Session(bind=database, join_transaction_mode="create_savepoint") as store,
    ):
        jobs.set_maintenance(
            store, enabled=True, expected_generation=jobs.supervisor_status(store)["generation"]
        )


def test_postgres_exact_claim_finish_and_owner_rls(database: sa.Connection) -> None:
    claim = claimed(database)
    role(database, "vm_research", PEER)
    assert database.scalar(sa.text("SELECT count(*) FROM research_jobs")) == 0
    role(database, "vm_research")
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        finished = jobs.finish_job(
            store,
            OWNER,
            claim["job_id"],
            attempt_id=claim["attempt_id"],
            token=claim["claim_token"],
            generation=claim["generation"],
            result={"valid": True},
        )
        assert finished["status"] == "completed"
        store.commit()
    database.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
    with pytest.raises(sa.exc.DBAPIError, match="immutable"), database.begin_nested():
        database.execute(
            sa.text("UPDATE research_job_attempts SET result_json='{}' WHERE attempt_id=:attempt"),
            {"attempt": claim["attempt_id"]},
        )


def test_postgres_maintenance_generation_fences_old_results(database: sa.Connection) -> None:
    claim = claimed(database)
    role(database, "maintenance")
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        jobs.set_maintenance(store, enabled=True, expected_generation=claim["generation"])
        store.commit()
    role(database, "vm_research")
    with (
        Session(bind=database, join_transaction_mode="create_savepoint") as store,
        pytest.raises(jobs.ResearchWorkflowError, match="fenced"),
    ):
        jobs.finish_job(
            store,
            OWNER,
            claim["job_id"],
            attempt_id=claim["attempt_id"],
            token=claim["claim_token"],
            generation=claim["generation"],
            result={"valid": True},
        )
    role(database, "vm_backend")
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        jobs.cancel_job(store, OWNER, claim["job_id"])
        store.commit()
    role(database, "maintenance")
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        resumed = jobs.set_maintenance(
            store, enabled=False, expected_generation=claim["generation"]
        )
        assert resumed["generation"] != claim["generation"]
        store.commit()
    database.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_postgres_raw_draft_admission_refuses_maintenance(database: sa.Connection) -> None:
    claim = claimed(database)
    role(database, "maintenance")
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        jobs.set_maintenance(store, enabled=True, expected_generation=claim["generation"])
        store.commit()
    role(database, "vm_backend")
    with pytest.raises(sa.exc.DBAPIError, match="maintenance"), database.begin_nested():
        database.execute(
            sa.text(
                "INSERT INTO research_drafts (draft_id,owner_id,request_id,request_hash,name,revision,created_at) VALUES (:id,:owner,'fenced','hash','Fenced',0,CURRENT_TIMESTAMP)"
            ),
            {"id": str(uuid4()), "owner": OWNER},
        )


def test_postgres_session_maintenance_lock_refuses_admission_without_table_deadlock(
    database: sa.Connection,
) -> None:
    from lib_application.services.research_admission import require_research_admission

    database.execute(
        sa.text("SELECT pg_advisory_lock(hashtextextended('vynmatrix.research.control',0))")
    )
    try:
        with database.engine.connect() as peer, peer.begin():
            role(peer, "vm_backend")
            peer.execute(sa.text("SET LOCAL statement_timeout='1s'"))
            with (
                Session(bind=peer, join_transaction_mode="create_savepoint") as store,
                pytest.raises(jobs.ResearchWorkflowError, match="maintenance") as error,
            ):
                require_research_admission(store)
            assert error.value.status_code == 503
            with pytest.raises(sa.exc.DBAPIError, match="admission is busy"), peer.begin_nested():
                peer.execute(
                    sa.text(
                        "INSERT INTO research_drafts (draft_id,owner_id,request_id,request_hash,name,revision,created_at) VALUES (:id,:owner,'locked','hash','Locked',0,CURRENT_TIMESTAMP)"
                    ),
                    {"id": str(uuid4()), "owner": OWNER},
                )
            peer.rollback()
        database.execute(sa.text("LOCK TABLE research_drafts IN ACCESS EXCLUSIVE MODE NOWAIT"))
    finally:
        database.execute(
            sa.text("SELECT pg_advisory_unlock(hashtextextended('vynmatrix.research.control',0))")
        )


def workflow_migration(connection: sa.Connection) -> Any:
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(
        os.environ.get("RESEARCH_WORKFLOW_MIGRATION_PATH")
        or (
            Path(__file__).resolve().parents[1]
            / "scripts/db/alembic/versions/0114_research_workflows.py"
        )
    )
    spec = importlib.util.spec_from_file_location("research_workflows_downgrade", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


def test_postgres_empty_workflow_downgrade_is_reversible(database: sa.Connection) -> None:
    module = workflow_migration(database)
    with database.begin_nested() as restore_schema:
        module.downgrade()
        assert database.scalar(sa.text("SELECT to_regclass('public.research_jobs')")) is None
        assert (
            database.scalar(sa.text("SELECT to_regclass('public.research_datasets')")) is not None
        )
        restore_schema.rollback()
    assert database.scalar(sa.text("SELECT to_regclass('public.research_jobs')")) is not None


def test_postgres_retained_workflow_refuses_downgrade_without_dropping_tables(
    database: sa.Connection,
) -> None:
    enqueue(database)
    role(database, "maintenance")
    module = workflow_migration(database)
    with database.begin_nested() as restore_locks:
        with pytest.raises(RuntimeError, match="retained research"):
            module.downgrade()
        assert database.scalar(sa.text("SELECT count(*) FROM research_jobs")) == 1
        restore_locks.rollback()


def test_postgres_workflow_retention_check_fences_racing_admission(database: sa.Connection) -> None:
    from lib_application.services.research_admission import require_research_admission

    module = workflow_migration(database)
    raced: list[bool] = []

    def during_count(_connection, _cursor, statement, _parameters, _context, _many):
        if raced or statement.strip() != "SELECT count(*) FROM research_drafts":
            return
        with database.engine.connect() as peer, peer.begin():
            peer.execute(sa.text("SET LOCAL statement_timeout='1s'"))
            with (
                Session(bind=peer, join_transaction_mode="create_savepoint") as store,
                pytest.raises(jobs.ResearchWorkflowError, match="maintenance") as refusal,
            ):
                require_research_admission(store)
            assert refusal.value.status_code == 503
            with (
                pytest.raises(sa.exc.DBAPIError, match="could not obtain lock"),
                peer.begin_nested(),
            ):
                peer.execute(sa.text("LOCK TABLE research_drafts IN ROW EXCLUSIVE MODE NOWAIT"))
            raced.append(True)

    sa.event.listen(database, "after_cursor_execute", during_count)
    try:
        with database.begin_nested() as restore_schema:
            module.downgrade()
            restore_schema.rollback()
    finally:
        sa.event.remove(database, "after_cursor_execute", during_count)
    assert raced == [True]


def test_postgres_launch_barrier_keeps_heartbeat_free_then_excludes_delayed_start(
    database: sa.Connection,
) -> None:
    from lib_application.services.research_admission import (
        RESEARCH_CONTROL_LOCK,
        RESEARCH_LAUNCH_LOCK,
        require_research_admission,
    )

    database.execute(
        sa.text("SELECT pg_advisory_lock(hashtextextended(:key,0))"), {"key": RESEARCH_LAUNCH_LOCK}
    )
    control_held = False
    try:
        with database.engine.connect() as peer, peer.begin():
            peer.execute(sa.text("SET LOCAL statement_timeout='1s'"))
            assert (
                peer.scalar(
                    sa.text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"),
                    {"key": RESEARCH_LAUNCH_LOCK},
                )
                is False
            )
            role(peer, "vm_research")
            with Session(bind=peer, join_transaction_mode="create_savepoint") as store:
                state = jobs.supervisor_status(store)
                heartbeat = jobs.supervisor_heartbeat(
                    store,
                    worker_id="heartbeat-peer",
                    generation=state["generation"],
                    image_hash="a" * 64,
                    bundle_hash="b" * 64,
                )
                assert heartbeat["available"] is True
            peer.rollback()
        database.execute(
            sa.text("SELECT pg_advisory_lock(hashtextextended(:key,0))"),
            {"key": RESEARCH_CONTROL_LOCK},
        )
        control_held = True
        with Session(bind=database, join_transaction_mode="create_savepoint") as store:
            state = jobs.supervisor_status(store)
            jobs.set_maintenance(store, enabled=True, expected_generation=state["generation"])
            store.commit()
        with database.engine.connect() as peer, peer.begin():
            peer.execute(sa.text("SET LOCAL statement_timeout='1s'"))
            assert (
                peer.scalar(
                    sa.text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"),
                    {"key": RESEARCH_LAUNCH_LOCK},
                )
                is False
            )
            with (
                Session(bind=peer, join_transaction_mode="create_savepoint") as store,
                pytest.raises(jobs.ResearchWorkflowError, match="maintenance"),
            ):
                require_research_admission(store)
    finally:
        if control_held:
            database.execute(
                sa.text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"),
                {"key": RESEARCH_CONTROL_LOCK},
            )
        database.execute(
            sa.text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"),
            {"key": RESEARCH_LAUNCH_LOCK},
        )


def test_postgres_cancel_retry_retains_attempt_and_refuses_late_result(
    database: sa.Connection,
) -> None:
    claim = claimed(database)
    role(database, "vm_backend")
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        assert jobs.cancel_job(store, OWNER, claim["job_id"])["status"] == "cancelled"
        retried = jobs.retry_job(store, OWNER, claim["job_id"], request_id="explicit-retry")
        assert retried["status"] == "queued"
        assert retried["attempt_count"] == 2
        assert retried["attempts"][0]["status"] == "cancelled"
        assert (
            jobs.retry_job(store, OWNER, claim["job_id"], request_id="explicit-retry")[
                "attempt_count"
            ]
            == 2
        )
        store.commit()
    role(database, "vm_research")
    with (
        Session(bind=database, join_transaction_mode="create_savepoint") as store,
        pytest.raises(jobs.ResearchWorkflowError, match="fenced"),
    ):
        jobs.finish_job(
            store,
            OWNER,
            claim["job_id"],
            attempt_id=claim["attempt_id"],
            token=claim["claim_token"],
            generation=claim["generation"],
            result={"valid": True},
        )


def test_postgres_replacement_supervisor_fences_old_claim(database: sa.Connection) -> None:
    claim = claimed(database)
    role(database, "maintenance")
    database.execute(
        sa.text(
            "UPDATE research_control SET heartbeat_at=clock_timestamp()-interval '31 seconds' WHERE control_id=1"
        )
    )
    role(database, "vm_research")
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        jobs.supervisor_heartbeat(
            store,
            worker_id="replacement",
            generation=claim["generation"],
            image_hash="a" * 64,
            bundle_hash="b" * 64,
        )
        with pytest.raises(jobs.ResearchWorkflowError, match="fenced"):
            jobs.heartbeat_job(
                store,
                OWNER,
                claim["job_id"],
                attempt_id=claim["attempt_id"],
                token=claim["claim_token"],
                generation=claim["generation"],
            )
        stale = jobs.reconcile_expired_attempts(store)
        assert stale[0]["attempt_id"] == claim["attempt_id"]
        assert jobs.get_job(store, OWNER, claim["job_id"])["status"] == "interrupted"
        store.commit()


def test_postgres_expired_lease_retains_interrupted_attempt(database: sa.Connection) -> None:
    import time

    enqueue(database)
    role(database, "vm_research")
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        generation = jobs.supervisor_status(store)["generation"]
        jobs.supervisor_heartbeat(
            store,
            worker_id="short-lease",
            generation=generation,
            image_hash="a" * 64,
            bundle_hash="b" * 64,
        )
        claim = jobs.claim_job(
            store,
            worker_id="short-lease",
            generation=generation,
            image_hash="a" * 64,
            bundle_hash="b" * 64,
            lease_seconds=5,
        )
        assert claim is not None
        store.commit()
    time.sleep(5.05)
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        assert (
            jobs.claim_job(
                store,
                worker_id="short-lease",
                generation=generation,
                image_hash="a" * 64,
                bundle_hash="b" * 64,
            )
            is None
        )
        assert jobs.get_job(store, OWNER, claim["job_id"])["status"] == "interrupted"
        with pytest.raises(jobs.ResearchWorkflowError, match="fenced"):
            jobs.finish_job(
                store,
                OWNER,
                claim["job_id"],
                attempt_id=claim["attempt_id"],
                token=claim["claim_token"],
                generation=generation,
                result={"valid": True},
            )
        store.commit()


def test_postgres_successful_train_registration_freezes_exact_artifact(
    database: sa.Connection,
) -> None:
    import math
    from datetime import UTC, datetime, timedelta

    from dev_cli.core.research.backtest import model_rows
    from lib_application.db.models import Instrument
    from lib_application.services import research_data, research_registry
    from lib_strategy.research import train_return_model

    role(database, "maintenance")
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        instrument_id = (
            int(store.scalar(sa.select(sa.func.coalesce(sa.func.max(Instrument.instr_id), 0))) or 0)
            + 1
        )
        store.add(
            Instrument(
                instr_id=instrument_id,
                canonical="RMUNITUSD",
                asset_class="crypto",
                settlement_currency="USD",
            )
        )
        store.commit()
    lines = ["ts,open,high,low,close,volume"]
    for index in range(1300):
        price = 100 + math.sin(index / 7)
        timestamp = (datetime(2020, 1, 1, tzinfo=UTC) + timedelta(minutes=index)).isoformat()
        lines.append(f"{timestamp},{price},{price + 1},{price - 1},{price},100")
    csv = ("\n".join(lines) + "\n").encode()
    metadata = {
        "instrument_id": instrument_id,
        "source": "research-pg-unit",
        "provenance": "deterministic unit fixture; not market evidence",
        "currency": "USD",
        "timeframe": "1m",
        "timestamp_semantics": "bar_close",
        "adjustment_policy": "raw",
    }
    role(database, "vm_backend")
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        receipt = research_data.import_dataset(store, OWNER, metadata, csv)
        dataset = research_data.freeze_dataset(
            store, OWNER, receipt["import_id"], receipt["content_hash"], "Model contract fixture"
        )
        job = jobs.enqueue_job(
            store,
            OWNER,
            request_id="train-unit",
            kind="train",
            inputs={"dataset_id": dataset["dataset_id"], "horizon": 1},
        )
        store.commit()
    role(database, "vm_research")
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        generation = jobs.supervisor_status(store)["generation"]
        jobs.supervisor_heartbeat(
            store,
            worker_id="fit-unit",
            generation=generation,
            image_hash="a" * 64,
            bundle_hash="b" * 64,
        )
        claim = jobs.claim_job(
            store,
            worker_id="fit-unit",
            generation=generation,
            image_hash="a" * 64,
            bundle_hash="b" * 64,
        )
        assert claim is not None
        frozen = research_data.get_dataset(store, OWNER, dataset["dataset_id"], include_bars=True)
        fitted = train_return_model(
            model_rows(frozen),
            identity={
                "instrument_id": instrument_id,
                "source": metadata["source"],
                "timeframe": metadata["timeframe"],
                "dataset_hash": dataset["content_hash"],
                "image_hash": "a" * 64,
            },
        )
        jobs.finish_job(
            store,
            OWNER,
            job["job_id"],
            attempt_id=claim["attempt_id"],
            token=claim["claim_token"],
            generation=generation,
            result=fitted,
        )
        store.commit()
    role(database, "vm_backend")
    with Session(bind=database, join_transaction_mode="create_savepoint") as store:
        registered = research_registry.register_model(
            store, OWNER, request_id="register-unit", job_id=job["job_id"], name="Fitted model"
        )
        assert registered["artifact"] == fitted["model"]
        assert registered["artifact_hash"] == fitted["model"]["artifact_hash"]
        assert (
            research_registry.register_model(
                store, OWNER, request_id="register-unit", job_id=job["job_id"], name="Fitted model"
            )["model_id"]
            == registered["model_id"]
        )
        with pytest.raises(jobs.ResearchWorkflowError, match="not found"):
            research_registry.get_model(store, PEER, registered["model_id"])
        store.commit()
    role(database, "maintenance")
    with pytest.raises(sa.exc.DBAPIError, match="immutable"), database.begin_nested():
        database.execute(
            sa.text("UPDATE research_model_versions SET name='mutated' WHERE model_id=:model_id"),
            {"model_id": registered["model_id"]},
        )
