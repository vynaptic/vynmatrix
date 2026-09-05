"""Explicit static metadata changes preserve authority and caller transactions."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from sqlalchemy.orm import Session

from lib_application.db.models import (
    ApiAuditLog,
    Base,
    Broker,
    BrokerEnvironment,
    Instrument,
    InstrumentAlias,
    InstrumentBrokerSymbol,
    Sector,
    User,
)
from lib_application.services.catalogue_changes import (
    apply_catalogue_changes,
    register_instrument_links,
    validate_catalogue_changes,
    validate_instrument_links,
)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(
            User(
                user_id="catalogue-owner:text",
                email="owner@test.invalid",
                is_deployment_owner=True,
                base_ccy="EUR",
            )
        )
        session.add(Broker(broker_id=1, code="coinbase", name="Installed", capabilities={}))
        session.add(
            BrokerEnvironment(
                broker_id=1,
                environment="live",
                region="US",
                base_urls={"rest": "https://api.coinbase.com"},
                rate_limits={"requests_per_second": 10},
            )
        )
        session.add(Sector(code="crypto", name="Installed sector", asset_class="crypto"))
        session.add(
            Instrument(
                instr_id=1,
                canonical="BTC/USD",
                asset_class="crypto",
                settlement_currency="USD",
                market_session_policy="continuous",
            )
        )
        session.commit()
        yield session
    engine.dispose()


def _patch(**extra: Any) -> dict[str, Any]:
    return {
        "kind": "broker",
        "key": {"code": "coinbase"},
        "expected": {"name": "Installed"},
        "changes": {"name": "Reviewed"},
        **extra,
    }


def test_patch_preserves_identity_and_omitted_metadata_with_atomic_audit(session: Session) -> None:
    results = apply_catalogue_changes(session, [_patch()])
    assert results[0].record_id == 1
    assert results[0].changed_fields == ("name",)
    assert session.get(Broker, 1).capabilities == {}
    assert session.query(ApiAuditLog).one().user_id == "catalogue-owner:text"
    session.rollback()
    assert session.get(Broker, 1).name == "Installed"
    assert session.query(ApiAuditLog).count() == 0


def test_dry_run_does_not_mutate_rows_or_audit(session: Session) -> None:
    results = apply_catalogue_changes(session, [_patch()], dry_run=True)
    assert results[0].changed_fields == ("name",)
    assert session.get(Broker, 1).name == "Installed"
    assert session.query(ApiAuditLog).count() == 0
    assert not session.dirty


def test_later_stale_patch_prevents_earlier_update(session: Session) -> None:
    stale = {
        "kind": "sector",
        "key": {"code": "crypto"},
        "expected": {"name": "stale"},
        "changes": {"name": "Changed"},
    }
    with pytest.raises(ValueError, match="expected"):
        apply_catalogue_changes(session, [_patch(), stale])
    assert session.get(Broker, 1).name == "Installed"
    assert session.query(ApiAuditLog).count() == 0


def test_repeated_acknowledged_patch_is_noop_without_duplicate_audit(session: Session) -> None:
    apply_catalogue_changes(session, [_patch()])
    session.commit()
    result = apply_catalogue_changes(session, [_patch()])
    assert result[0].changed_fields == ()
    assert session.query(ApiAuditLog).count() == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"user_id": "other"},
        {"kind": "strategy"},
        {"kind": "instrument"},
        {"expected": {}},
        {"changes": {"code": "renamed"}},
    ],
)
def test_invalid_or_authority_changing_envelopes_fail_before_writes(
    session: Session, extra: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match=r"require|Unsupported|recataloguing"):
        apply_catalogue_changes(session, [_patch(**extra)])
    assert session.get(Broker, 1).name == "Installed"
    assert session.query(ApiAuditLog).count() == 0


def test_duplicate_targets_fail_before_any_update(session: Session) -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        apply_catalogue_changes(session, [_patch(), _patch()])
    assert session.get(Broker, 1).name == "Installed"


def test_capability_changes_require_exact_adapter_reference(session: Session) -> None:
    patch = _patch(
        expected={"capabilities": {}}, changes={"capabilities": {"features": ["invented"]}}
    )
    with pytest.raises(ValueError, match="authoritative adapter"):
        apply_catalogue_changes(
            session, [patch], broker_capabilities={"coinbase": {"features": ["spot"]}}
        )
    with pytest.raises(ValueError, match="authoritative adapter"):
        apply_catalogue_changes(session, [patch])
    desired = {"features": ["spot"]}
    result = apply_catalogue_changes(
        session,
        [_patch(expected={"capabilities": {}}, changes={"capabilities": desired})],
        broker_capabilities={"coinbase": desired},
    )
    assert result[0].record_id == 1
    assert session.get(Broker, 1).capabilities == desired


def _environment_patch(field: str, value: Any) -> dict[str, Any]:
    return {
        "kind": "broker_environment",
        "key": {"broker_code": "coinbase", "environment": "live", "region": "US"},
        "expected": {field: {}},
        "changes": {field: value},
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret@host.invalid",
        "https://host.invalid?api_key=secret",
        "https://host.invalid/#secret",
        "http://host.invalid",
        "ftp://host.invalid",
        "https://host.invalid/\nsecret",
        "https://host.invalid:0",
        "https://host.invalid:65536",
        "https://host.invalid:invalid",
        "https://host.invalid:-1",
        "https://[::1]:0",
        "https://[::1]:invalid",
        "https://[invalid",
    ],
)
def test_endpoint_patch_rejects_credentials_and_unsupported_urls(endpoint: str) -> None:
    with pytest.raises(ValueError, match=r"(?i)endpoint"):
        validate_catalogue_changes([_environment_patch("base_urls", {"rest": endpoint})])


@pytest.mark.parametrize(
    "endpoint",
    ["https://host.invalid:1", "https://host.invalid:65535", "wss://[::1]:9443/ws"],
)
def test_endpoint_patch_accepts_valid_explicit_ports(endpoint: str) -> None:
    patch = validate_catalogue_changes([_environment_patch("base_urls", {"rest": endpoint})])
    assert patch[0].changes["base_urls"] == {"rest": endpoint}


@pytest.mark.parametrize("rate", [0, -1, True, "10", float("inf"), float("nan")])
def test_rate_limits_must_be_positive_finite_numbers(rate: Any) -> None:
    with pytest.raises(ValueError, match=r"finite positive|Out of range"):
        validate_catalogue_changes(
            [_environment_patch("rate_limits", {"requests_per_second": rate})]
        )


def test_environment_and_sector_metadata_preserve_unmentioned_authority(session: Session) -> None:
    environment = _environment_patch("rate_limits", {"requests_per_second": 5})
    environment["expected"] = {"rate_limits": {"requests_per_second": 10}}
    sector = {
        "kind": "sector",
        "key": {"code": "crypto"},
        "expected": {"description": None},
        "changes": {"description": "Reviewed classification"},
    }
    results = apply_catalogue_changes(session, [environment, sector])
    assert len(results) == 2
    assert session.query(BrokerEnvironment).one().base_urls == {"rest": "https://api.coinbase.com"}
    assert session.query(Sector).one().asset_class == "crypto"
    assert session.query(Sector).one().parent_sector_id is None


def _instrument(**extra: Any) -> dict[str, Any]:
    return {
        "symbol": "BTCUSD",
        "asset_class": "crypto",
        "settlement_currency": "USD",
        "market_session_policy": "continuous",
        **extra,
    }


def test_explicit_links_preserve_ids_and_omitted_venue_identifiers(session: Session) -> None:
    definition = _instrument(
        aliases=[{"alias": "BTCUSD", "source": "seed"}],
        broker_symbols=[
            {
                "broker_code": "coinbase",
                "broker_symbol": "BTC-USD",
                "broker_instrument_id": "exact-product",
            }
        ],
    )
    first = register_instrument_links(session, [definition])
    session.commit()
    alias_id = session.query(InstrumentAlias).one().alias_id
    definition["broker_symbols"][0].pop("broker_instrument_id")
    repeated = register_instrument_links(session, [definition])
    assert all(result.changed_fields == () for result in repeated)
    assert session.query(InstrumentAlias).one().alias_id == alias_id
    assert session.query(InstrumentBrokerSymbol).one().broker_instrument_id == "exact-product"
    assert all(result.record_id == 1 for result in first)
    assert session.query(ApiAuditLog).count() == 2


def test_later_link_conflict_prevents_earlier_alias_insert(session: Session) -> None:
    session.add(InstrumentBrokerSymbol(instr_id=1, broker_id=1, broker_symbol="ReviewedProduct"))
    session.commit()
    definition = _instrument(
        aliases=[{"alias": "BTCUSD", "source": "seed"}],
        broker_symbols=[{"broker_code": "coinbase", "broker_symbol": "BTC-USD"}],
    )
    with pytest.raises(ValueError, match="Immutable"):
        register_instrument_links(session, [definition])
    assert session.query(InstrumentAlias).count() == 0


def test_link_dry_run_checks_planned_new_sources_without_allocating_ids(session: Session) -> None:
    definition = _instrument(
        symbol="ETHUSD",
        aliases=[{"alias": "ETH-USD", "source": "seed"}],
        broker_symbols=[{"broker_code": "planned", "broker_symbol": "ETHUSD"}],
    )
    results = register_instrument_links(
        session, [definition], broker_references=[{"code": "planned"}], dry_run=True
    )
    assert all(result.record_id is None for result in results)
    assert session.query(Instrument).count() == 1
    assert session.query(Broker).count() == 1
    assert session.query(InstrumentAlias).count() == 0
    assert session.query(ApiAuditLog).count() == 0


def test_source_batch_rejects_normalized_alias_and_exact_id_collisions() -> None:
    first = _instrument(aliases=[{"alias": "SHARED", "source": "seed"}])
    second = _instrument(symbol="ETHUSD", aliases=[{"alias": "SH-ARED", "source": "seed"}])
    with pytest.raises(ValueError, match="normalized alias"):
        validate_instrument_links([first, second])
    first = _instrument(
        broker_symbols=[
            {
                "broker_code": "coinbase",
                "broker_symbol": "BTC-USD",
                "broker_instrument_id": "shared",
            }
        ]
    )
    second = _instrument(
        symbol="ETHUSD",
        broker_symbols=[
            {
                "broker_code": "coinbase",
                "broker_symbol": "ETH-USD",
                "broker_instrument_id": "shared",
            }
        ],
    )
    with pytest.raises(ValueError, match="exact identity"):
        validate_instrument_links([first, second])


def test_spot_source_cannot_authorize_perpetual_mapping() -> None:
    with pytest.raises(ValueError, match="Derivative"):
        validate_instrument_links(
            [
                _instrument(
                    broker_symbols=[{"broker_code": "deribit", "broker_symbol": "BTC-PERPETUAL"}]
                )
            ]
        )


def test_repository_links_are_explicit_compatible_seed_references() -> None:
    root = Path(__file__).resolve().parents[1]
    definitions = yaml.safe_load((root / "config/instruments.yaml").read_text())["instruments"]
    links = validate_instrument_links(definitions)
    assert len([link for link in links if link.kind == "aliases"]) == 6
    mappings = [link for link in links if link.kind == "broker_symbols"]
    assert len(mappings) == 12
    assert {link.values["broker_code"] for link in mappings} == {"coinbase", "ibkr", "saxo"}
    aapl = next(link for link in mappings if link.canonical == "AAPL")
    assert aapl.values["broker_instrument_id"] == "265598"


def test_link_validation_checks_installed_asset_class(session: Session) -> None:
    session.add(Broker(code="deribit", name="Deribit", capabilities={}))
    session.commit()
    definition = _instrument(
        asset_class="futures",
        market_session_policy="scheduled",
        broker_symbols=[{"broker_code": "deribit", "broker_symbol": "BTC-PERPETUAL"}],
    )
    with pytest.raises(ValueError, match="asset class"):
        register_instrument_links(session, [definition])


def test_dry_run_detects_existing_exact_identity_collision_for_planned_instrument(
    session: Session,
) -> None:
    session.add(
        InstrumentBrokerSymbol(
            instr_id=1, broker_id=1, broker_symbol="BTC-USD", broker_instrument_id="claimed"
        )
    )
    session.commit()
    definition = _instrument(
        symbol="ETHUSD",
        broker_symbols=[
            {
                "broker_code": "coinbase",
                "broker_symbol": "ETH-USD",
                "broker_instrument_id": "claimed",
            }
        ],
    )
    with pytest.raises(ValueError, match="already belongs"):
        register_instrument_links(session, [definition], dry_run=True)


def test_dry_run_does_not_flush_or_discard_caller_pending_changes(session: Session) -> None:
    broker = session.get(Broker, 1)
    broker.name = "Caller draft"
    with pytest.raises(ValueError, match="clean caller session"):
        apply_catalogue_changes(session, [_patch()], dry_run=True)
    assert broker.name == "Caller draft"
    assert broker in session.dirty


def test_maintenance_links_require_the_existing_role_authority_port(session: Session) -> None:
    from lib_application.services.database_authority import DatabaseAuthorityError

    with pytest.raises(DatabaseAuthorityError):
        register_instrument_links(session, [_instrument()], maintenance=True)
    assert session.query(ApiAuditLog).count() == 0
