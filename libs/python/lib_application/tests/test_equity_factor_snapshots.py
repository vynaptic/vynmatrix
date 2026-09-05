"""Atomic cutoff-safe persistence tests for equity factor snapshots."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    EquityFactorEvidence,
    EquityFactorSnapshot,
    EquityFactorSnapshotDetail,
    EquityObservation,
    EquitySourceLineage,
    Instrument,
    Strategy,
    StrategyVersion,
)
from lib_application.services.equity_factor_snapshots import (
    EquityEvidenceReference,
    EquityFactorDetailInput,
    EquityFactorSnapshotPersistenceError,
    EquityFactorSnapshotSubmission,
    EquityFactorState,
    equity_factor_snapshot_identity,
    persist_equity_factor_snapshot,
)
from lib_strategy.cross_sectional import FactorDirection, PeerScaleMethod
from lib_strategy.equity_optional_factors import optional_factor_source_registry_sha256

_CUTOFF = datetime(2025, 2, 3, 22, tzinfo=UTC)
_OBSERVATION_ID = "1" * 64
_LINEAGE_ID = "2" * 64
_CONFIGURATION_DIGEST = "3" * 64


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _seed(
    session: Session,
    *,
    event_at: datetime | None = None,
    available_at: datetime | None = None,
) -> int:
    session.add(
        Strategy(
            strategy_id="sp500-rotation",
            strategy_name="S&P 500 rotation",
            asset_class="equity",
        )
    )
    version = StrategyVersion(
        strategy_id="sp500-rotation",
        semver="1.0.0",
        param_schema={},
        default_params={},
    )
    session.add(version)
    session.add(
        Instrument(
            instr_id=1,
            asset_class="equity",
            canonical="AAA",
            settlement_currency="USD",
            market_session_policy="scheduled",
        )
    )
    session.flush()
    session.add(
        EquitySourceLineage(
            lineage_id=_LINEAGE_ID,
            provider="sec",
            product="edgar-companyfacts",
            endpoint="https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
            dataset_version="retrieved-2025-02-03",
            tool_version="sec-edgar-v1",
            source_identity="CIK0000000001/companyfacts",
            source_revision="source-sha256",
            retrieved_at=_CUTOFF,
            timestamp_semantics={"available_at": "filing acceptance timestamp"},
            adjustment_policy="not-applicable",
            entitlement_scope="public-sec",
            missing_data_policy="fail-closed",
            content_sha256="4" * 64,
        )
    )
    session.flush()
    session.add(
        EquityObservation(
            observation_id=_OBSERVATION_ID,
            lineage_id=_LINEAGE_ID,
            instr_id=1,
            observation_kind="xbrl_fact",
            source_record_identity="0000000001-25-000001/us-gaap/Revenues/USD/CY2024",
            event_at=event_at or (_CUTOFF - timedelta(days=3)),
            available_at=available_at or (_CUTOFF - timedelta(days=2)),
            revision=1,
            accession_number="0000000001-25-000001",
            filing_form="10-K",
            sic_code="3571",
            disposition="observed",
            content_sha256="5" * 64,
        )
    )
    session.commit()
    return int(version.strat_ver_id)


def _detail() -> EquityFactorDetailInput:
    return EquityFactorDetailInput(
        factor_name="quality",
        sleeve_name="fundamental_quality",
        factor_version="sec-fundamental-components-v1",
        direction=FactorDirection.HIGHER_IS_BETTER,
        enabled=True,
        state=EquityFactorState.COMPLETE,
        raw_value=Decimal("0.25"),
        peer_group="Information Technology",
        peer_count=20,
        peer_center=Decimal("0.125"),
        peer_scale=Decimal("0.0625"),
        peer_scale_method=PeerScaleMethod.MEDIAN_ABSOLUTE_DEVIATION,
        unbounded_normalized_value=Decimal("2.0"),
        normalized_value=Decimal("2.0"),
        factor_rank=Decimal("1"),
        weight=Decimal("0.5"),
        contribution=Decimal("1.0"),
        evidence=(EquityEvidenceReference(_OBSERVATION_ID),),
    )


def _submission(strategy_version_id: int) -> EquityFactorSnapshotSubmission:
    return EquityFactorSnapshotSubmission(
        strategy_id="sp500-rotation",
        strategy_version_id=strategy_version_id,
        instrument_id=1,
        effective_session=date(2025, 2, 3),
        cutoff_at=_CUTOFF,
        calculation_version="fundamental-panel-v1",
        configuration_digest=_CONFIGURATION_DIGEST,
        source_contract_registry_sha256=optional_factor_source_registry_sha256(),
        peer_taxonomy_version="sec-sic-2025-v1",
        peer_group="Information Technology",
        details=(_detail(),),
    )


def test_factor_snapshot_is_atomic_and_exactly_replayable() -> None:
    engine = _engine()
    with Session(engine) as session:
        version_id = _seed(session)
        submission = _submission(version_id)
        expected_identity = equity_factor_snapshot_identity(submission)
        first = persist_equity_factor_snapshot(session, submission)
        session.commit()

    assert first.created is True
    assert first.factor_snapshot_id == expected_identity
    with Session(engine) as session:
        replay = persist_equity_factor_snapshot(session, submission)
        session.commit()

        assert replay.factor_snapshot_id == first.factor_snapshot_id
        assert replay.created is False
        assert session.scalar(sa.select(sa.func.count()).select_from(EquityFactorSnapshot)) == 1
        assert (
            session.scalar(sa.select(sa.func.count()).select_from(EquityFactorSnapshotDetail)) == 1
        )
        assert session.scalar(sa.select(sa.func.count()).select_from(EquityFactorEvidence)) == 1


def test_completed_cutoff_rejects_different_factor_content() -> None:
    engine = _engine()
    with Session(engine) as session:
        version_id = _seed(session)
        submission = _submission(version_id)
        persist_equity_factor_snapshot(session, submission)
        session.commit()

        changed_detail = replace(
            submission.details[0],
            normalized_value=Decimal("1.5"),
            contribution=Decimal("0.75"),
        )
        changed = replace(submission, details=(changed_detail,))
        with pytest.raises(
            EquityFactorSnapshotPersistenceError,
            match="cannot be corrected or replaced",
        ):
            persist_equity_factor_snapshot(session, changed)
        session.rollback()

        assert session.scalar(sa.select(sa.func.count()).select_from(EquityFactorSnapshot)) == 1


@pytest.mark.parametrize(
    ("event_at", "available_at", "message"),
    [
        (
            _CUTOFF - timedelta(days=1),
            _CUTOFF + timedelta(seconds=1),
            "unavailable at the decision cutoff",
        ),
        (
            _CUTOFF + timedelta(seconds=1),
            _CUTOFF - timedelta(days=1),
            "future event timestamp",
        ),
    ],
)
def test_factor_evidence_must_be_cutoff_safe(
    event_at: datetime,
    available_at: datetime,
    message: str,
) -> None:
    engine = _engine()
    with Session(engine) as session:
        version_id = _seed(
            session,
            event_at=event_at,
            available_at=available_at,
        )
        with pytest.raises(EquityFactorSnapshotPersistenceError, match=message):
            persist_equity_factor_snapshot(session, _submission(version_id))
        session.rollback()

        assert session.scalar(sa.select(sa.func.count()).select_from(EquityFactorSnapshot)) == 0


def test_exact_replay_detects_child_row_tampering() -> None:
    engine = _engine()
    with Session(engine) as session:
        version_id = _seed(session)
        submission = _submission(version_id)
        persist_equity_factor_snapshot(session, submission)
        session.commit()

        session.execute(
            sa.update(EquityFactorSnapshotDetail)
            .where(EquityFactorSnapshotDetail.factor_name == "quality")
            .values(factor_version="tampered")
        )
        session.commit()

        with pytest.raises(
            EquityFactorSnapshotPersistenceError,
            match="details do not match",
        ):
            persist_equity_factor_snapshot(session, submission)
