"""Tests for the default-off quality-compounder quarter-end gate."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from lib_application.db.models import (
    Base,
    MarketCalendar,
    MarketSession,
    Strategy,
    StrategyPanelInputRevision,
    StrategyVersion,
)
from market_data_ingestor.quality_compounder_quarterly import (
    QualityCompounderQuarterlyError,
    QualityCompounderQuarterlyJob,
    QualityCompounderQuarterlyStatus,
    QualityCompounderQuarterlyWindow,
)

_NOW = datetime(2026, 6, 30, 21, 0, tzinfo=UTC)


def _factory(
    *,
    decision_date: date = date(2026, 6, 30),
    execution_date: date = date(2026, 7, 1),
    active: bool = True,
    version_status: str = "active",
) -> sessionmaker[Session]:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        strategy = Strategy(
            strategy_id="us_quality_compounder_v1",
            strategy_name="USQualityCompounder",
            asset_class="equity",
            is_active=active,
        )
        session.add(strategy)
        session.flush()
        session.add(
            StrategyVersion(
                strat_ver_id=1401,
                strategy_id=strategy.strategy_id,
                semver="0.2.0",
                param_schema={},
                default_params={},
                status=version_status,
                released_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        calendar = MarketCalendar(
            code="XNYS",
            source_kind="exchange",
            provider="NYSE",
            source_reference="test official calendar",
        )
        session.add(calendar)
        session.flush()
        for session_date in (decision_date, execution_date):
            opens_at = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC).replace(
                hour=13,
                minute=30,
            )
            session.add(
                MarketSession(
                    calendar_id=int(calendar.calendar_id),
                    opens_at=opens_at,
                    closes_at=opens_at.replace(hour=20, minute=0),
                )
            )
    return factory


class _Producer:
    def __init__(self, factory: sessionmaker[Session] | None = None) -> None:
        self.calls = 0
        self._factory = factory

    def produce(
        self,
        *,
        window: QualityCompounderQuarterlyWindow,
        started_at: datetime,
        complete_before: datetime,
    ) -> None:
        self.calls += 1
        assert started_at < complete_before
        if self._factory is None:
            return
        with self._factory.begin() as session:
            session.add(
                StrategyPanelInputRevision(
                    input_sha256="1" * 64,
                    strategy_id="us_quality_compounder_v1",
                    strategy_version="0.2.0",
                    universe_code="SP500",
                    cutoff_at=started_at,
                    official_session_date=window.decision_opens_at.date(),
                    execute_not_before=window.execution_opens_at,
                    data_use_scope="paper_forward",
                    entitlement_owner_user_id=None,
                    provider_authority_sha256="2" * 64,
                    membership_sha256="3" * 64,
                    factor_snapshot_sha256="4" * 64,
                    panel_sha256="5" * 64,
                    strategy_validator_id="test-validator",
                    strategy_validator_version="1.0.0",
                    strategy_input_authority_sha256="6" * 64,
                    strategy_input_authority_payload={"test": True},
                    panel_payload={"test": True},
                    strategy_input_payload={"test": True},
                )
            )


def test_job_is_disabled_without_reading_registration() -> None:
    producer = _Producer()
    result = QualityCompounderQuarterlyJob(
        session_factory=_factory(active=False),
        producer=producer,
        enabled=False,
        clock=lambda: _NOW,
    ).run_once()

    assert result.status is QualityCompounderQuarterlyStatus.DISABLED
    assert producer.calls == 0


def test_job_runs_once_only_at_official_quarter_end() -> None:
    factory = _factory()
    producer = _Producer(factory)
    job = QualityCompounderQuarterlyJob(
        session_factory=factory,
        producer=producer,
        enabled=True,
        clock=lambda: _NOW,
    )

    first = job.run_once()
    replay = job.run_once()

    assert first.status is QualityCompounderQuarterlyStatus.PRODUCED
    assert first.decision_session == date(2026, 6, 30)
    assert replay.status is QualityCompounderQuarterlyStatus.ALREADY_COMPLETE
    assert replay.input_sha256 == "1" * 64
    assert producer.calls == 1


def test_job_does_not_run_before_the_quarter_end_session() -> None:
    producer = _Producer()
    result = QualityCompounderQuarterlyJob(
        session_factory=_factory(
            decision_date=date(2026, 6, 29),
            execution_date=date(2026, 6, 30),
        ),
        producer=producer,
        enabled=True,
        clock=lambda: datetime(2026, 6, 29, 21, 0, tzinfo=UTC),
    ).run_once()

    assert result.status is QualityCompounderQuarterlyStatus.NOT_QUARTER_END
    assert producer.calls == 0


def test_enabled_job_can_produce_evidence_while_strategy_execution_is_inactive() -> None:
    factory = _factory(active=False)
    producer = _Producer(factory)

    result = QualityCompounderQuarterlyJob(
        session_factory=factory,
        producer=producer,
        enabled=True,
        clock=lambda: _NOW,
    ).run_once()

    assert result.status is QualityCompounderQuarterlyStatus.PRODUCED
    assert producer.calls == 1


def test_enabled_job_requires_active_registered_model_version() -> None:
    with pytest.raises(QualityCompounderQuarterlyError, match="version is not active"):
        QualityCompounderQuarterlyJob(
            session_factory=_factory(version_status="deprecated"),
            producer=_Producer(),
            enabled=True,
            clock=lambda: _NOW,
        ).run_once()
