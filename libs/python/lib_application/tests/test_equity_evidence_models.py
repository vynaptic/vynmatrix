"""ORM contracts for immutable point-in-time equity evidence."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import CheckConstraint, create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    EquityFactorEvidence,
    EquityFactorSnapshot,
    EquityFactorSnapshotDetail,
    EquityObservation,
    EquityObservationValue,
    EquityRankSnapshot,
    EquityRankSnapshotRow,
    EquitySourceLineage,
)

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def _source_lineage() -> EquitySourceLineage:
    return EquitySourceLineage(
        lineage_id=_DIGEST_A,
        provider="sec",
        product="edgar-companyfacts",
        endpoint="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
        dataset_version="retrieved-2026-08-01",
        tool_version="equity-evidence-v1",
        source_identity="CIK0000320193/companyfacts",
        source_revision="sha256-content",
        retrieved_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
        timestamp_semantics={"available_at": "filing acceptance timestamp"},
        adjustment_policy="not-applicable",
        entitlement_scope="public-sec",
        missing_data_policy="fail-closed",
        content_sha256=_DIGEST_B,
    )


def test_equity_evidence_tables_are_public_and_registered() -> None:
    expected = {
        EquitySourceLineage: "equity_source_lineages",
        EquityObservation: "equity_observations",
        EquityObservationValue: "equity_observation_values",
        EquityFactorSnapshot: "equity_factor_snapshots",
        EquityFactorSnapshotDetail: "equity_factor_snapshot_details",
        EquityFactorEvidence: "equity_factor_evidence",
        EquityRankSnapshot: "equity_rank_snapshots",
        EquityRankSnapshotRow: "equity_rank_snapshot_rows",
    }

    for model, table_name in expected.items():
        assert model.__tablename__ == table_name
        assert Base.metadata.tables[table_name] is model.__table__


def test_equity_evidence_tables_create_with_database_constraints() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    inspector = inspect(engine)

    assert {
        "equity_source_lineages",
        "equity_observations",
        "equity_observation_values",
        "equity_factor_snapshots",
        "equity_factor_snapshot_details",
        "equity_factor_evidence",
        "equity_rank_snapshots",
        "equity_rank_snapshot_rows",
    } <= set(inspector.get_table_names())
    assert {
        constraint.name
        for constraint in EquityObservation.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    } >= {
        "ck_equity_observation_disposition",
        "ck_equity_observation_revision",
        "ck_equity_observed_availability",
    }
    assert {index["name"] for index in inspector.get_indexes("equity_rank_snapshot_rows")} >= {
        "ix_equity_rank_row_factor",
        "ix_equity_rank_row_position",
    }
    observation_kind_constraint = next(
        constraint["sqltext"]
        for constraint in inspector.get_check_constraints("equity_observations")
        if constraint["name"] == "ck_equity_observation_kind"
    )
    assert all(
        kind in observation_kind_constraint
        for kind in (
            "analyst_estimate",
            "call_transcript",
            "insider_transaction",
            "macro_release",
            "news_event",
            "positioning",
        )
    )


def test_equity_source_lineage_rejects_orm_update_and_delete() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(_source_lineage())
        session.commit()

        row = session.get(EquitySourceLineage, _DIGEST_A)
        assert row is not None
        row.product = "rewritten-product"
        with pytest.raises(ValueError, match="EquitySourceLineage rows are immutable"):
            session.commit()
        session.rollback()

        row = session.get(EquitySourceLineage, _DIGEST_A)
        assert row is not None
        session.delete(row)
        with pytest.raises(ValueError, match="append-only"):
            session.commit()


def test_observed_sec_fact_requires_availability_and_filing_identity() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(_source_lineage())
        session.flush()
        session.add(
            EquityObservation(
                observation_id=_DIGEST_B,
                lineage_id=_DIGEST_A,
                observation_kind="xbrl_fact",
                source_record_identity="0000320193-25-000079/us-gaap/Revenues/USD/CY2025Q2",
                event_at=datetime(2025, 6, 28, tzinfo=UTC),
                available_at=None,
                revision=1,
                sic_code="3571",
                disposition="observed",
                content_sha256=_DIGEST_A,
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()
