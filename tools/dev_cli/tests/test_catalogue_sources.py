"""Installation reads validated existing sources without granting execution."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    ApiAuditLog,
    Base,
    Broker,
    BrokerEnvironment,
    Instrument,
    InstrumentAlias,
    InstrumentBrokerSymbol,
    InstrumentSector,
    Sector,
    Strategy,
    StrategyVersion,
    User,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def catalogue_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """SQLite unit composition; role checks are injected, never PostgreSQL evidence."""
    from dev_cli.core import catalogue
    from lib_application.services import catalogue_changes

    def unit_role_check(session: Session) -> None:
        assert session.get_bind().dialect.name == "sqlite"

    monkeypatch.setattr(catalogue, "require_backend_database_role", unit_role_check)
    monkeypatch.setattr(catalogue, "require_maintenance_database_role", unit_role_check)
    monkeypatch.setattr(catalogue_changes, "require_maintenance_database_role", unit_role_check)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _catalogue_snapshot(session: Session) -> dict[str, list[tuple]]:
    models = (
        Broker,
        BrokerEnvironment,
        Instrument,
        InstrumentAlias,
        InstrumentBrokerSymbol,
        InstrumentSector,
        Sector,
        Strategy,
        StrategyVersion,
        ApiAuditLog,
    )
    return {
        model.__tablename__: [tuple(row) for row in session.execute(select(model.__table__))]
        for model in models
    }


def test_complete_repository_sources_preview_apply_and_retry(catalogue_session: Session) -> None:
    from dev_cli.core.catalogue import load_catalogue, reconcile_catalogue

    session = catalogue_session
    sources = load_catalogue(ROOT)
    before = _catalogue_snapshot(session)
    preview = reconcile_catalogue(session, sources, apply=False, maintenance=True)
    assert preview["missing"]["instruments"] == len(sources["instruments"])
    assert preview["links"]
    assert all(item["record_id"] is None for item in preview["links"])
    assert _catalogue_snapshot(session) == before
    result = reconcile_catalogue(session, sources, apply=True, maintenance=True)
    assert result["created"] == preview["missing"]
    assert all(not row.is_active for row in session.scalars(select(Strategy)))
    assert {row.status for row in session.scalars(select(StrategyVersion))} == {"registered"}
    session.commit()
    # A lost acknowledgement is retried from a new identity map, as on CLI restart.
    session.expunge_all()
    installed = _catalogue_snapshot(session)
    repeated = reconcile_catalogue(session, sources, apply=True, maintenance=True)
    assert not any(repeated["created"].values())
    assert all(not item["changed_fields"] for item in repeated["links"])
    assert _catalogue_snapshot(session) == installed


@pytest.mark.parametrize("status", ["active", "deprecated", "pulled"])
def test_repository_rerun_preserves_existing_release_authority(
    catalogue_session: Session, status: str
) -> None:
    from dev_cli.core.catalogue import load_catalogue, reconcile_catalogue

    session = catalogue_session
    sources = load_catalogue(ROOT)
    reconcile_catalogue(session, sources, apply=True, maintenance=True)
    for row in session.scalars(select(Strategy)):
        row.is_active = status == "active"
    for row in session.scalars(select(StrategyVersion)):
        row.status = status
    session.commit()
    before = _catalogue_snapshot(session)
    reconcile_catalogue(session, sources, apply=True, maintenance=True)
    assert _catalogue_snapshot(session) == before


def test_complete_batch_conflict_is_rejected_before_any_write(catalogue_session: Session) -> None:
    from dev_cli.core.catalogue import load_catalogue, reconcile_catalogue

    session = catalogue_session
    sources = load_catalogue(ROOT)
    first = sources["releases"][0]
    sources["releases"].append(replace(first, semver="99.0.0", strategy_name="Conflicting"))
    before = _catalogue_snapshot(session)
    with pytest.raises(ValueError, match="Conflicting strategy definitions"):
        reconcile_catalogue(session, sources, apply=True, maintenance=True)
    assert _catalogue_snapshot(session) == before


def test_owner_preview_refuses_pending_work_before_owner_query(catalogue_session: Session) -> None:
    from dev_cli.core.catalogue import load_catalogue, reconcile_catalogue

    session = catalogue_session
    session.add(
        User(
            user_id="text-owner",
            email="owner@example.test",
            base_ccy="EUR",
            is_deployment_owner=True,
        )
    )
    session.commit()
    pending = Sector(code="pending", name="Pending", asset_class="crypto")
    session.add(pending)
    flushes = []
    event.listen(session, "before_flush", lambda *args: flushes.append(True))
    with pytest.raises(ValueError, match="clean caller session"):
        reconcile_catalogue(session, load_catalogue(ROOT), apply=False)
    assert flushes == []
    assert pending in session.new


def test_repository_registration_rolls_back_with_caller(catalogue_session: Session) -> None:
    from dev_cli.core.catalogue import load_catalogue, reconcile_catalogue

    session = catalogue_session
    before = _catalogue_snapshot(session)
    reconcile_catalogue(session, load_catalogue(ROOT), apply=True, maintenance=True)
    session.rollback()
    assert _catalogue_snapshot(session) == before


def test_repository_conflicting_operator_edit_is_preserved(catalogue_session: Session) -> None:
    from dev_cli.core.catalogue import load_catalogue, reconcile_catalogue

    session = catalogue_session
    sources = load_catalogue(ROOT)
    reconcile_catalogue(session, sources, apply=True, maintenance=True)
    session.scalars(select(Broker).where(Broker.code == "paper")).one().name = "My local broker"
    session.commit()
    before = _catalogue_snapshot(session)
    with pytest.raises(ValueError, match="conflict: name"):
        reconcile_catalogue(session, sources, apply=True, maintenance=True)
    assert _catalogue_snapshot(session) == before


def test_missing_hierarchy_edge_is_counted_and_audited_once(catalogue_session: Session) -> None:
    from dev_cli.core.catalogue import load_catalogue, reconcile_catalogue

    session = catalogue_session
    sources = load_catalogue(ROOT)
    reconcile_catalogue(session, sources, apply=True, maintenance=True)
    session.delete(session.scalars(select(InstrumentSector)).first())
    session.commit()
    before_audits = len(session.scalars(select(ApiAuditLog)).all())
    result = reconcile_catalogue(session, sources, apply=True, maintenance=True)
    assert result["created"]["instrument_sectors"] == 1
    assert sum(result["created"].values()) == 1
    assert len(session.scalars(select(ApiAuditLog)).all()) == before_audits + 1
    session.commit()
    reconcile_catalogue(session, sources, apply=True, maintenance=True)
    assert len(session.scalars(select(ApiAuditLog)).all()) == before_audits + 1


def test_owner_relative_catalogue_registration_audits_textual_owner(
    catalogue_session: Session,
) -> None:
    from dev_cli.core.catalogue import load_catalogue, reconcile_catalogue

    session = catalogue_session
    session.add(
        User(
            user_id="existing-text-owner",
            email="owner@example.test",
            base_ccy="EUR",
            is_deployment_owner=True,
        )
    )
    session.commit()
    reconcile_catalogue(session, load_catalogue(ROOT), apply=True)
    audits = session.scalars(select(ApiAuditLog)).all()
    assert audits
    assert {row.user_id for row in audits} == {"existing-text-owner"}


def test_loads_all_shipped_releases_including_disabled_sources() -> None:
    from dev_cli.core.catalogue import load_strategy_releases

    releases = load_strategy_releases(ROOT)
    assert {release.strategy_name for release in releases} == {
        "SwingHighLowPMO",
        "USQualityCompounder",
    }
    assert all(release.semver and release.param_schema for release in releases)
    assert all("strategy_version" in release.default_params for release in releases)
    assert all(not hasattr(release, "is_active") for release in releases)


def test_strategy_filter_rejects_unknown_id() -> None:
    from dev_cli.core.catalogue import load_strategy_releases

    with pytest.raises(ValueError, match="Unknown strategy"):
        load_strategy_releases(ROOT, strategy_id="typo_strategy")


def test_broker_references_match_available_adapters_and_have_no_accounts() -> None:
    from dev_cli.core.catalogue import load_broker_references
    from lib_infrastructure.brokers.capabilities import BROKER_SPECS

    references = load_broker_references(ROOT)
    capabilities = {item.code: item.capabilities.catalogue_payload() for item in BROKER_SPECS}
    assert {item["code"] for item in references} == set(capabilities) | {"paper"}
    for item in references:
        if item["code"] in capabilities:
            assert item["capabilities"] == capabilities[item["code"]]
        else:
            assert item["capabilities"]["live_certification_implemented"] is False
            assert item["environments"][0]["base_urls"] == {"transport": "in_process"}
        assert item["environments"]
        assert set(item) == {"code", "name", "capabilities", "environments"}


def test_broker_filter_rejects_unknown_code() -> None:
    from dev_cli.core.catalogue import load_broker_references

    with pytest.raises(ValueError, match="Unknown broker"):
        load_broker_references(ROOT, broker_code="guess_a_broker")
