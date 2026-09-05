"""Reference registration preserves installed authority and caller transactions."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lib_application.db.models import Base, Instrument, InstrumentSector, Sector
from lib_application.services.catalogue import check_catalogue, upsert_instruments


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _definition(**changes: object) -> dict[str, object]:
    return {
        "symbol": "BTCUSDC",
        "asset_class": "crypto",
        "settlement_currency": "USDC",
        "market_session_policy": "continuous",
        **changes,
    }


def test_registration_leaves_commit_to_caller(session: Session) -> None:
    upsert_instruments(session, [_definition()])
    assert len(session.scalars(select(Instrument)).all()) == 1
    session.rollback()
    assert session.scalars(select(Instrument)).all() == []


def test_preview_does_not_flush_caller_pending_work(session: Session) -> None:
    pending = Sector(code="pending", name="Pending", asset_class="crypto")
    session.add(pending)
    with pytest.raises(ValueError, match="clean caller session"):
        check_catalogue(session, instruments=[], releases=[], brokers=[])
    assert pending in session.new
    assert pending.sector_id is None


def test_preview_rejects_inconsistent_strategy_identity_across_versions(session: Session) -> None:
    with pytest.raises(ValueError, match="Conflicting strategy definitions"):
        check_catalogue(
            session,
            instruments=[],
            brokers=[],
            releases=[_release(), _release(semver="2.0.0", strategy_name="Different name")],
        )


def test_preview_counts_missing_instrument_sector_edges(session: Session) -> None:
    definition = _definition(sector="crypto")
    upsert_instruments(session, [definition])
    session.commit()
    edge = session.scalars(select(InstrumentSector)).one()
    session.delete(edge)
    session.commit()
    counts = check_catalogue(session, instruments=[definition], releases=[], brokers=[])
    assert counts["instrument_sectors"] == 1
    assert counts["instruments"] == counts["sectors"] == 0


def test_reference_batch_validates_before_any_write(session: Session) -> None:
    with pytest.raises(ValueError, match="settlement_currency"):
        upsert_instruments(
            session, [_definition(), _definition(symbol="ETHUSDC", settlement_currency="")]
        )
    assert session.scalars(select(Instrument)).all() == []


def test_existing_reference_conflict_does_not_overwrite_owner_setting(session: Session) -> None:
    installed = Instrument(
        canonical="BTCUSDC",
        asset_class="crypto",
        settlement_currency="USDC",
        market_session_policy="continuous",
        is_tradable=False,
    )
    session.add(installed)
    session.commit()
    with pytest.raises(ValueError, match="is_tradable"):
        upsert_instruments(session, [_definition()])
    session.refresh(installed)
    assert installed.is_tradable is False


def test_existing_canonical_spelling_and_hierarchy_are_preserved(session: Session) -> None:
    installed = Instrument(
        canonical="BTC/USDC",
        asset_class="crypto",
        settlement_currency="USDC",
        market_session_policy="continuous",
        is_tradable=True,
    )
    sector = Sector(code="owner_sector", name="Owner sector", asset_class="crypto")
    session.add_all([installed, sector])
    session.flush()
    session.add(InstrumentSector(instr_id=installed.instr_id, sector_id=sector.sector_id, weight=1))
    session.commit()
    original_id = installed.instr_id
    upsert_instruments(session, [_definition()])
    session.commit()
    assert [(i.instr_id, i.canonical) for i in session.scalars(select(Instrument))] == [
        (original_id, "BTC/USDC")
    ]
    assert len(session.scalars(select(InstrumentSector)).all()) == 1


def test_conflict_in_later_reference_prevents_earlier_insert(session: Session) -> None:
    session.add(
        Instrument(
            canonical="ETHUSDC",
            asset_class="crypto",
            settlement_currency="EUR",
            market_session_policy="continuous",
            is_tradable=True,
        )
    )
    session.commit()
    with pytest.raises(ValueError, match="settlement_currency"):
        upsert_instruments(session, [_definition(), _definition(symbol="ETHUSDC")])
    assert [row.canonical for row in session.scalars(select(Instrument))] == ["ETHUSDC"]


def test_repeat_reference_registration_preserves_identifiers(session: Session) -> None:
    definition = _definition(sector="crypto", industry="layer1")
    upsert_instruments(session, [definition])
    session.commit()
    instrument_id = session.scalars(select(Instrument.instr_id)).one()
    sector_ids = list(session.scalars(select(Sector.sector_id).order_by(Sector.sector_id)))
    upsert_instruments(session, [definition])
    session.commit()
    assert session.scalars(select(Instrument.instr_id)).one() == instrument_id
    assert list(session.scalars(select(Sector.sector_id).order_by(Sector.sector_id))) == sector_ids
    assert len(session.scalars(select(InstrumentSector)).all()) == 2


def _release(**changes: object):
    from lib_application.services.catalogue import StrategyRelease

    return StrategyRelease(
        **{
            "strategy_id": "unit_strategy",
            "strategy_name": "UnitStrategy",
            "asset_class": "crypto",
            "semver": "1.0.0",
            "param_schema": {"type": "object"},
            "default_params": {"window": 5},
            **changes,
        }
    )


def test_strategy_registration_is_non_executable_and_repeatable(session: Session) -> None:
    from lib_application.db.models import Strategy, StrategyVersion
    from lib_application.services.catalogue import register_strategies

    register_strategies(session, [_release()])
    session.commit()
    strategy = session.get(Strategy, "unit_strategy")
    version = session.scalars(select(StrategyVersion)).one()
    assert strategy.is_active is False
    assert version.status == "registered"
    original_id = version.strat_ver_id
    strategy.is_active = True
    version.status = "pulled"
    session.commit()
    register_strategies(session, [_release()])
    session.commit()
    assert session.get(Strategy, "unit_strategy").is_active is True
    assert session.scalars(select(StrategyVersion)).one().strat_ver_id == original_id
    assert session.scalars(select(StrategyVersion)).one().status == "pulled"
    register_strategies(session, [_release(semver="1.1.0", default_params={"window": 10})])
    session.commit()
    assert sorted(session.scalars(select(StrategyVersion.status))) == ["pulled", "registered"]


@pytest.mark.parametrize(
    "changes", [{"default_params": {"window": 8}}, {"param_schema": {"type": "array"}}]
)
def test_release_conflict_prevents_entire_batch(session: Session, changes: dict) -> None:
    from lib_application.db.models import StrategyVersion
    from lib_application.services.catalogue import register_strategies

    register_strategies(session, [_release()])
    session.commit()
    with pytest.raises(ValueError, match="immutable release"):
        register_strategies(session, [_release(semver="2.0.0"), _release(**changes)])
    assert session.scalars(select(StrategyVersion.semver)).all() == ["1.0.0"]


def test_release_registration_does_not_commit(session: Session) -> None:
    from lib_application.db.models import Strategy, StrategyVersion
    from lib_application.services.catalogue import register_strategies

    register_strategies(session, [_release()])
    session.rollback()
    assert session.scalars(select(StrategyVersion)).all() == []
    assert session.scalars(select(Strategy)).all() == []


def _broker_reference() -> dict[str, object]:
    return {
        "code": "paper",
        "name": "Local paper",
        "capabilities": {"asset_classes": ["crypto"], "live_certification_implemented": False},
        "environments": [
            {
                "environment": "paper",
                "region": "global",
                "base_urls": {"transport": "in_process"},
                "rate_limits": {},
            }
        ],
    }


def test_broker_registration_preserves_stable_route_ids(session: Session) -> None:
    from lib_application.db.models import Broker, BrokerEnvironment
    from lib_application.services.catalogue import register_brokers

    register_brokers(session, [_broker_reference()])
    session.commit()
    broker_id = session.scalars(select(Broker.broker_id)).one()
    route_id = session.scalars(select(BrokerEnvironment.broker_env_id)).one()
    register_brokers(session, [_broker_reference()])
    session.commit()
    assert session.scalars(select(Broker.broker_id)).all() == [broker_id]
    assert session.scalars(select(BrokerEnvironment.broker_env_id)).all() == [route_id]


def test_broker_conflict_preserves_edited_settings(session: Session) -> None:
    from lib_application.db.models import BrokerEnvironment
    from lib_application.services.catalogue import register_brokers

    register_brokers(session, [_broker_reference()])
    session.commit()
    row = session.scalars(select(BrokerEnvironment)).one()
    row.rate_limits = {"requests_per_second": 3}
    session.commit()
    with pytest.raises(ValueError, match="rate_limits"):
        register_brokers(session, [_broker_reference()])
    session.refresh(row)
    assert row.rate_limits == {"requests_per_second": 3}


def test_broker_registration_leaves_commit_to_caller(session: Session) -> None:
    from lib_application.db.models import Broker, BrokerEnvironment
    from lib_application.services.catalogue import register_brokers

    register_brokers(session, [_broker_reference()])
    session.rollback()
    assert session.scalars(select(Broker)).all() == []
    assert session.scalars(select(BrokerEnvironment)).all() == []
