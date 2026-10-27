"""Opt-in research evidence guards on an isolated migrated PostgreSQL database.

RESEARCH_TEST_DATABASE_URL names migration authority in an unconfigured test DB.
Fixture market values prove storage contracts only; they are not market evidence.
Every test rolls back. This file creates no database, role or container.
"""

from __future__ import annotations

import copy
import importlib.util
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from lib_application.db.models import Instrument, User
from lib_application.db.models.research_data import ResearchBar, ResearchDataset
from lib_application.services import research_data as data
from lib_data.research_dataset import iso_timestamp

pytestmark = pytest.mark.integration
OWNER = "research-data-acceptance-owner"
CONTENT = b"ts,open,high,low,close,volume\n2026-01-01T00:00:00Z,100,102,99,101,4\n2026-01-01T00:01:00Z,101,103,100,102,5\n"


@pytest.fixture
def store() -> Iterator[tuple[Session, dict[str, Any]]]:
    url = os.environ.get("RESEARCH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("RESEARCH_TEST_DATABASE_URL requires isolated PostgreSQL acceptance")
    engine = sa.create_engine(url, hide_parameters=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("Research evidence proof requires PostgreSQL")
    try:
        with engine.connect() as connection, connection.begin():
            assert (
                connection.scalar(sa.text("SELECT count(*) FROM users WHERE is_deployment_owner"))
                == 0
            ), "Use an isolated unconfigured acceptance database"
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                owner = User(
                    user_id=OWNER,
                    email="owner@research-data.invalid",
                    base_ccy="USD",
                    is_deployment_owner=True,
                )
                instrument = Instrument(
                    canonical="RESEARCH-DATA-TEST", asset_class="crypto", settlement_currency="USD"
                )
                session.add_all([owner, instrument])
                session.flush()
                metadata = {
                    "instrument_id": instrument.instr_id,
                    "source": "recorded-fixture",
                    "provenance": "deterministic storage fixture",
                    "currency": "USD",
                    "timeframe": "1m",
                    "timestamp_semantics": "bar_open",
                    "adjustment_policy": "raw",
                }
                session.commit()
            connection.execute(sa.text("SET LOCAL ROLE vm_backend"))
            connection.execute(
                sa.text("SELECT set_config('app.current_tenant',:owner,true)"), {"owner": OWNER}
            )
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                yield session, metadata
            connection.rollback()
    finally:
        engine.dispose()


def values(receipt: dict[str, Any]) -> dict[str, Any]:
    """Independently construct the documented exact two-bar freeze manifest."""
    manifest = {
        "schema": "research-dataset-v1",
        "content_hash": receipt["content_hash"],
        "original_hash": receipt["original_hash"],
        "metadata": receipt["metadata"],
        "validation": receipt["report"],
        "eligibility": receipt["report"]["eligibility"],
        "bar_count": 2,
        "first_ts": "2026-01-01T00:00:00Z",
        "last_ts": "2026-01-01T00:01:00Z",
        "price_revisions": [],
        "original_recorded_at": receipt["created_at"],
    }
    return {
        "dataset_id": str(uuid4()),
        "owner_id": OWNER,
        "import_id": receipt["import_id"],
        "supersedes_id": None,
        "name": "Guard fixture",
        "content_hash": receipt["content_hash"],
        "original_hash": receipt["original_hash"],
        "metadata_json": receipt["metadata"],
        "manifest_json": manifest,
        "bar_count": 2,
        "stored_bytes": 0,
    }


@pytest.mark.parametrize(
    "mutation", ["eligibility", "validation", "extra_manifest_field", "supersedes"]
)
def test_postgres_dataset_authority_cannot_override_receipt(
    store: tuple[Session, dict[str, Any]], mutation: str
) -> None:
    session, metadata = store
    previous = data.import_dataset(
        session, OWNER, {**metadata, "provenance": "prior deterministic fixture"}, CONTENT
    )
    prior_dataset = data.freeze_dataset(
        session, OWNER, previous["import_id"], previous["content_hash"], "Prior"
    )
    receipt = data.import_dataset(session, OWNER, metadata, CONTENT)
    candidate = copy.deepcopy(values(receipt))
    if mutation == "eligibility":
        candidate["manifest_json"]["eligibility"]["certified"] = True
    elif mutation == "validation":
        candidate["manifest_json"]["validation"]["bar_count"] = 999
    elif mutation == "extra_manifest_field":
        candidate["manifest_json"]["release_authorized"] = True
    else:
        candidate["supersedes_id"] = prior_dataset["dataset_id"]
    with pytest.raises(sa.exc.DBAPIError, match="receipt"), session.begin_nested():
        session.execute(sa.insert(ResearchDataset.__table__).values(**candidate))


@pytest.mark.parametrize("incomplete", [False, True])
def test_postgres_freeze_completion_rejects_changed_or_missing_bars(
    store: tuple[Session, dict[str, Any]], incomplete: bool
) -> None:
    session, metadata = store
    receipt = data.import_dataset(session, OWNER, metadata, CONTENT)
    candidate = values(receipt)
    bars = [
        {
            "ts": "2026-01-01T00:00:00Z",
            "open": "100",
            "high": "102",
            "low": "99",
            "close": "101",
            "volume": "4",
            "bar_open": "2026-01-01T00:00:00Z",
            "bar_close": "2026-01-01T00:01:00Z",
        },
        {
            "ts": "2026-01-01T00:01:00Z",
            "open": "101",
            "high": "103",
            "low": "100",
            "close": "102",
            "volume": "5",
            "bar_open": "2026-01-01T00:01:00Z",
            "bar_close": "2026-01-01T00:02:00Z",
        },
    ]
    if incomplete:
        bars.pop()
    else:
        bars[1]["close"] = "101"

    def incomplete_or_changed_freeze() -> None:
        with session.begin_nested():
            session.execute(sa.insert(ResearchDataset.__table__).values(**candidate))
            session.execute(
                sa.insert(ResearchBar.__table__),
                [
                    {
                        "owner_id": OWNER,
                        "dataset_id": candidate["dataset_id"],
                        "ordinal": index,
                        "ts": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
                        "bar_json": bar,
                    }
                    for index, bar in enumerate(bars)
                ],
            )
            session.execute(sa.text("SET CONSTRAINTS research_dataset_complete IMMEDIATE"))

    with pytest.raises(sa.exc.DBAPIError, match=r"receipt|incomplete"):
        incomplete_or_changed_freeze()


def test_postgres_100000_bar_freeze_has_bounded_statements(
    store: tuple[Session, dict[str, Any]],
) -> None:
    session, metadata = store
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    content = (
        "ts,open,high,low,close,volume\n"
        + "".join(
            f"{iso_timestamp(origin + timedelta(minutes=index))},100,102,99,101,4\n"
            for index in range(100_000)
        )
    ).encode()
    receipt = data.import_dataset(session, OWNER, metadata, content)
    assert receipt["valid"] is True
    # The old per-row extraction decompresses the whole array100000 times.
    # Bound each statement so that regression fails without an unbounded run.
    session.execute(sa.text("SET LOCAL statement_timeout='15s'"))
    frozen = data.freeze_dataset(
        session, OWNER, receipt["import_id"], receipt["content_hash"], "Maximum retained fixture"
    )
    session.execute(sa.text("SET CONSTRAINTS research_dataset_complete IMMEDIATE"))
    assert (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(ResearchBar)
            .where(ResearchBar.dataset_id == frozen["dataset_id"])
        )
        == 100_000
    )
    assert (
        data.get_dataset(session, OWNER, frozen["dataset_id"], include_bars=True)["content_hash"]
        == receipt["content_hash"]
    )


def test_postgres_downgrade_waits_for_inflight_import_before_any_drop(
    store: tuple[Session, dict[str, Any]],
) -> None:
    session, metadata = store
    receipt = data.import_dataset(session, OWNER, metadata, b"invalid header\n")
    assert receipt["valid"] is False
    # Keep the exact import uncommitted in this connection. The second
    # transaction cannot count it, and must acquire its DDL fence before counts.
    engine = session.get_bind().engine
    path = Path(
        os.environ.get("RESEARCH_DATA_MIGRATION_PATH")
        or Path(__file__).resolve().parents[1] / "scripts/db/alembic/versions/0113_research_data.py"
    )
    spec = importlib.util.spec_from_file_location("research_data_downgrade_guard", path)
    assert spec
    assert spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.connect() as maintenance, maintenance.begin():
        maintenance.execute(sa.text("SET LOCAL lock_timeout='1s'"))
        maintenance.execute(sa.text("SET LOCAL statement_timeout='8s'"))
        migration.op = Operations(MigrationContext.configure(maintenance))
        with pytest.raises(sa.exc.DBAPIError) as error:
            migration.downgrade()
        assert getattr(error.value.orig, "pgcode", None) == "55P03"
        # A DROP that eventually blocks is too late: counts have raced already.
        # Prove contention stopped at the explicit pre-count admission fence.
        assert str(error.value.statement).lstrip().startswith("LOCK TABLE")
    assert session.scalar(sa.select(sa.func.count()).select_from(ResearchDataset)) == 0
