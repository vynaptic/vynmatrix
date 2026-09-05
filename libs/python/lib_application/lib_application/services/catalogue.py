"""Create missing reference data without overwriting installed configuration.

The caller owns the transaction and retry boundary. Catalogue changes serialize
on PostgreSQL; a complete batch is checked against the locked current state
before any inserts. Existing identifiers, calendar authority and operator edits
are never inferred from a symbol spelling or replaced by a bootstrap rerun.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Broker,
    BrokerEnvironment,
    Instrument,
    InstrumentSector,
    Sector,
    Strategy,
    StrategyVersion,
)
from lib_common.asset_classes import REFERENCE_ONLY_ASSET_CLASSES, normalize_asset_class
from lib_data.market_data import normalize_product_symbol

_IDENTIFIER_LENGTH = 50
_MIN_CURRENCY_LENGTH = 3
_MAX_CURRENCY_LENGTH = 10


def _normalize_asset_class(value: str | None) -> str:
    if not value:
        msg = "Instrument asset_class is required"
        raise ValueError(msg)
    return normalize_asset_class(value, field_name="instrument asset_class")


def _resolve_is_tradable(instrument: dict[str, Any], *, asset_class: str, symbol: str) -> bool:
    value = instrument.get("is_tradable", asset_class not in REFERENCE_ONLY_ASSET_CLASSES)
    if not isinstance(value, bool):
        msg = f"{symbol} is_tradable must be a boolean"
        raise TypeError(msg)
    if asset_class in REFERENCE_ONLY_ASSET_CLASSES and value:
        msg = (
            f"{symbol} is a reference-only {asset_class} instrument; "
            "trade its concrete futures/options contract instead"
        )
        raise ValueError(msg)
    return value


@dataclass(frozen=True)
class InstrumentReference:
    symbol: str
    asset_class: str
    settlement_currency: str
    market_session_policy: str
    is_tradable: bool
    sector: str
    industry: str

    @classmethod
    def parse(cls, item: dict[str, Any]) -> InstrumentReference:
        allowed = {
            "symbol",
            "asset_class",
            "settlement_currency",
            "market_session_policy",
            "is_tradable",
            "sector",
            "industry",
            "index",
            "aliases",
            "broker_symbols",
        }
        if extra := item.keys() - allowed:
            msg = f"Unsupported instrument fields: {', '.join(sorted(extra))}"
            raise ValueError(msg)
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or len(symbol) > _IDENTIFIER_LENGTH:
            msg = "Instrument symbol must contain 1-50 characters"
            raise ValueError(msg)
        asset_class = _normalize_asset_class(item.get("asset_class"))
        currency = str(item.get("settlement_currency") or "").strip().upper()
        if (
            not currency.isalpha()
            or not _MIN_CURRENCY_LENGTH <= len(currency) <= _MAX_CURRENCY_LENGTH
        ):
            msg = f"{symbol} requires an explicit settlement_currency"
            raise ValueError(msg)
        policy = str(item.get("market_session_policy") or "").strip().lower()
        expected = "continuous" if asset_class == "crypto" else "scheduled"
        if policy != expected:
            msg = f"{symbol} market_session_policy must be {expected!r}"
            raise ValueError(msg)
        sector = str(item.get("sector") or "").strip().lower()
        industry = str(item.get("industry") or "").strip().lower()
        if max(len(sector), len(industry)) > _IDENTIFIER_LENGTH:
            msg = f"{symbol} sector/industry codes exceed 50 characters"
            raise ValueError(msg)
        return cls(
            symbol,
            asset_class,
            currency,
            policy,
            _resolve_is_tradable(item, asset_class=asset_class, symbol=symbol),
            sector,
            industry,
        )

    def hierarchy(self) -> list[tuple[str, str | None]]:
        result: list[tuple[str, str | None]] = [(self.sector, None)] if self.sector else []
        if self.industry and self.industry != self.sector:
            result.append((self.industry, self.sector or None))
        return result


def lock_catalogue(session: Session) -> None:
    """Use the same transaction-scoped lock for every catalogue writer."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(18472, 1)"))


def _check_reference_batch(
    session: Session, references: list[InstrumentReference]
) -> tuple[dict[str, Instrument], dict[str, Sector]]:
    current: dict[str, Instrument] = {}
    for row in session.scalars(select(Instrument)):
        key = normalize_product_symbol(row.canonical)
        if key in current:
            msg = f"Ambiguous installed instrument identity: {key}"
            raise ValueError(msg)
        current[key] = row
    sectors = {row.code: row for row in session.scalars(select(Sector))}
    parent_codes = {row.sector_id: row.code for row in sectors.values()}
    desired_sectors: dict[str, tuple[str, str | None]] = {}
    seen: set[str] = set()
    for ref in references:
        key = normalize_product_symbol(ref.symbol)
        if key in seen:
            msg = f"Duplicate instrument definition: {ref.symbol}"
            raise ValueError(msg)
        seen.add(key)
        if installed := current.get(key):
            for field in (
                "asset_class",
                "settlement_currency",
                "market_session_policy",
                "is_tradable",
            ):
                if getattr(installed, field) != getattr(ref, field):
                    msg = f"Instrument {ref.symbol} conflict: {field}; explicit change required"
                    raise ValueError(msg)
        for code, parent in ref.hierarchy():
            desired = (ref.asset_class, parent)
            if code in desired_sectors and desired_sectors[code] != desired:
                msg = f"Conflicting sector definition: {code}"
                raise ValueError(msg)
            desired_sectors[code] = desired
            if sector := sectors.get(code):
                parent_code = (
                    parent_codes[sector.parent_sector_id]
                    if sector.parent_sector_id is not None
                    else None
                )
                actual = (sector.asset_class, parent_code)
                if actual != desired:
                    msg = f"Sector {code} conflict; explicit change required"
                    raise ValueError(msg)
    return current, sectors


def upsert_instruments(session: Session, instruments: list[dict[str, Any]]) -> None:
    """Register missing instruments/hierarchy; never commit or overwrite existing rows."""
    references = [InstrumentReference.parse(item) for item in instruments]
    lock_catalogue(session)
    current, sectors = _check_reference_batch(session, references)
    if session.get_bind().dialect.name == "postgresql":
        for ref in references:
            _create_instrument_reference(session, ref)
        return
    for ref in references:
        key = normalize_product_symbol(ref.symbol)
        instrument = current.get(key)
        if instrument is None:
            instrument = Instrument(
                canonical=ref.symbol,
                asset_class=ref.asset_class,
                settlement_currency=ref.settlement_currency,
                market_session_policy=ref.market_session_policy,
                is_tradable=ref.is_tradable,
            )
            session.add(instrument)
            session.flush()
            current[key] = instrument
        for code, parent in ref.hierarchy():
            if code not in sectors:
                sector = Sector(
                    code=code,
                    name=code,
                    asset_class=ref.asset_class,
                    parent_sector_id=sectors[parent].sector_id if parent else None,
                )
                session.add(sector)
                session.flush()
                sectors[code] = sector
            sector_id = sectors[code].sector_id
            if session.get(InstrumentSector, (instrument.instr_id, sector_id)) is None:
                session.add(
                    InstrumentSector(instr_id=instrument.instr_id, sector_id=sector_id, weight=1)
                )
    session.flush()


def _create_instrument_reference(session: Session, ref: InstrumentReference) -> None:
    instrument_id = session.scalar(
        text(
            "SELECT public.vm_catalogue_create_instrument("
            ":symbol, :asset, :currency, :policy, :tradable)"
        ),
        {
            "symbol": ref.symbol,
            "asset": ref.asset_class,
            "currency": ref.settlement_currency,
            "policy": ref.market_session_policy,
            "tradable": ref.is_tradable,
        },
    )
    for code, parent in ref.hierarchy():
        session.execute(
            text("SELECT public.vm_catalogue_create_sector(:code, :asset, :parent)"),
            {"code": code, "asset": ref.asset_class, "parent": parent},
        )
        session.execute(
            text("SELECT public.vm_catalogue_link_sector(:instrument_id, :code)"),
            {"instrument_id": instrument_id, "code": code},
        )


@dataclass(frozen=True)
class StrategyRelease:
    """Validated source release; operational activation is deliberately absent."""

    strategy_id: str
    strategy_name: str
    asset_class: str
    semver: str
    param_schema: dict[str, Any]
    default_params: dict[str, Any]
    description: str | None = None
    docker_image: str | None = None
    git_repo: str | None = None
    git_commit: str | None = None

    def validate(self) -> None:
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", self.strategy_id)
            or len(self.strategy_id) > _IDENTIFIER_LENGTH
        ):
            msg = "Invalid canonical strategy_id"
            raise ValueError(msg)
        if not self.strategy_name.strip() or self.strategy_name != self.strategy_name.strip():
            msg = "strategy_name must be nonblank and canonical"
            raise ValueError(msg)
        if normalize_asset_class(self.asset_class) != self.asset_class:
            msg = "Strategy asset_class must be canonical"
            raise ValueError(msg)
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", self.semver):
            msg = "Invalid strategy semver"
            raise ValueError(msg)
        limits = {
            "strategy_name": 255,
            "semver": 20,
            "description": 500,
            "docker_image": 500,
            "git_repo": 500,
            "git_commit": 100,
        }
        for field, limit in limits.items():
            value = getattr(self, field)
            if value is not None and len(value) > limit:
                msg = f"Strategy release field {field} exceeds {limit} characters"
                raise ValueError(msg)
        for field in ("param_schema", "default_params"):
            value = getattr(self, field)
            if not isinstance(value, dict):
                msg = f"Strategy release {field} must be an object"
                raise TypeError(msg)
            json.dumps(value, allow_nan=False)


def _check_strategy_release(
    release: StrategyRelease, strategy: Strategy | None, version: StrategyVersion | None
) -> None:
    if strategy is not None:
        for field in ("strategy_name", "asset_class", "description"):
            desired = getattr(release, field)
            if desired is not None and getattr(strategy, field) != desired:
                msg = f"Strategy {release.strategy_id} conflict: {field}; explicit change required"
                raise ValueError(msg)
    if version is not None:
        for field in ("param_schema", "default_params", "docker_image", "git_repo", "git_commit"):
            desired = getattr(release, field)
            if desired is not None and getattr(version, field) != desired:
                msg = (
                    f"Conflicting immutable release {release.strategy_id}/{release.semver}: {field}"
                )
                raise ValueError(msg)


def _create_strategy_release(session: Session, release: StrategyRelease) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT public.vm_catalogue_create_strategy(:id, :name, :asset, :description)"),
            {
                "id": release.strategy_id,
                "name": release.strategy_name,
                "asset": release.asset_class,
                "description": release.description,
            },
        )
        session.execute(
            text(
                "SELECT public.vm_catalogue_create_version("
                ":id, :semver, CAST(:schema AS jsonb), CAST(:params AS jsonb), "
                ":image, :repo, :commit)"
            ),
            {
                "id": release.strategy_id,
                "semver": release.semver,
                "schema": json.dumps(release.param_schema, allow_nan=False),
                "params": json.dumps(release.default_params, allow_nan=False),
                "image": release.docker_image,
                "repo": release.git_repo,
                "commit": release.git_commit,
            },
        )
        return
    if session.get(Strategy, release.strategy_id) is None:
        session.add(
            Strategy(
                strategy_id=release.strategy_id,
                strategy_name=release.strategy_name,
                asset_class=release.asset_class,
                description=release.description,
                is_active=False,
            )
        )
        session.flush()
    existing = session.scalar(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == release.strategy_id,
            StrategyVersion.semver == release.semver,
        )
    )
    if existing is None:
        session.add(
            StrategyVersion(
                strategy_id=release.strategy_id,
                semver=release.semver,
                status="registered",
                param_schema=release.param_schema,
                default_params=release.default_params,
                docker_image=release.docker_image,
                git_repo=release.git_repo,
                git_commit=release.git_commit,
            )
        )
        session.flush()


def _validate_strategy_releases(releases: list[StrategyRelease]) -> None:
    for release in releases:
        release.validate()
    seen: set[tuple[str, str]] = set()
    definitions: dict[str, tuple[str, str, str | None]] = {}
    for release in releases:
        key = (release.strategy_id, release.semver)
        if key in seen:
            msg = f"Duplicate strategy release: {key}"
            raise ValueError(msg)
        seen.add(key)
        definition = (release.strategy_name, release.asset_class, release.description)
        if release.strategy_id in definitions and definitions[release.strategy_id] != definition:
            msg = f"Conflicting strategy definitions: {release.strategy_id}"
            raise ValueError(msg)
        definitions[release.strategy_id] = definition


def _check_strategy_releases(session: Session, releases: list[StrategyRelease]) -> None:
    for release in releases:
        strategy = session.get(Strategy, release.strategy_id)
        version = session.scalar(
            select(StrategyVersion).where(
                StrategyVersion.strategy_id == release.strategy_id,
                StrategyVersion.semver == release.semver,
            )
        )
        _check_strategy_release(release, strategy, version)


def register_strategies(session: Session, releases: list[StrategyRelease]) -> None:
    """Register complete immutable releases with no activation or implicit updates."""
    _validate_strategy_releases(releases)
    lock_catalogue(session)
    _check_strategy_releases(session, releases)
    for release in releases:
        _create_strategy_release(session, release)


def _validate_broker_references(references: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for ref in references:
        if set(ref) != {"code", "name", "capabilities", "environments"}:
            msg = "Broker references require code, name, capabilities and environments"
            raise ValueError(msg)
        for field, limit in (("code", 50), ("name", 255)):
            value = ref[field]
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                msg = f"Invalid broker {field}"
                raise ValueError(msg)
        if ref["code"] in seen:
            msg = f"Duplicate broker: {ref['code']}"
            raise ValueError(msg)
        seen.add(ref["code"])
        if not isinstance(ref["capabilities"], dict) or not isinstance(ref["environments"], list):
            msg = "Broker capabilities must be an object and environments a list"
            raise TypeError(msg)
        json.dumps(ref, allow_nan=False)
        routes = set()
        for route in ref["environments"]:
            if set(route) != {"environment", "region", "base_urls", "rate_limits"}:
                msg = "Invalid broker environment fields"
                raise ValueError(msg)
            region = route["region"]
            if (
                route["environment"] not in {"paper", "live"}
                or not isinstance(region, str)
                or not region.strip()
            ):
                msg = "Broker environment and region must be explicit"
                raise ValueError(msg)
            if not isinstance(route["base_urls"], dict) or not isinstance(
                route["rate_limits"], dict
            ):
                msg = "Broker endpoints and limits must be objects"
                raise TypeError(msg)
            key = (route["environment"], region)
            if key in routes:
                msg = f"Duplicate broker environment: {ref['code']}/{key}"
                raise ValueError(msg)
            routes.add(key)


def _check_broker_references(session: Session, references: list[dict[str, Any]]) -> None:
    for ref in references:
        broker = session.scalar(select(Broker).where(Broker.code == ref["code"]))
        if broker is None:
            continue
        for field in ("name", "capabilities"):
            if getattr(broker, field) != ref[field]:
                msg = f"Broker {ref['code']} conflict: {field}; explicit change required"
                raise ValueError(msg)
        for route in ref["environments"]:
            installed = session.scalar(
                select(BrokerEnvironment).where(
                    BrokerEnvironment.broker_id == broker.broker_id,
                    BrokerEnvironment.environment == route["environment"],
                    BrokerEnvironment.region == route["region"],
                )
            )
            if installed is not None:
                for field in ("base_urls", "rate_limits"):
                    if getattr(installed, field) != route[field]:
                        msg = f"Broker {ref['code']} route conflict: {field}"
                        raise ValueError(msg)


def _create_broker_reference(session: Session, ref: dict[str, Any]) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text(
                "SELECT public.vm_catalogue_create_broker("
                ":code, :name, CAST(:capabilities AS jsonb))"
            ),
            {
                "code": ref["code"],
                "name": ref["name"],
                "capabilities": json.dumps(ref["capabilities"]),
            },
        )
        for route in ref["environments"]:
            session.execute(
                text(
                    "SELECT public.vm_catalogue_create_broker_environment("
                    ":code, :environment, :region, CAST(:urls AS jsonb), CAST(:limits AS jsonb))"
                ),
                {
                    "code": ref["code"],
                    "environment": route["environment"],
                    "region": route["region"],
                    "urls": json.dumps(route["base_urls"]),
                    "limits": json.dumps(route["rate_limits"]),
                },
            )
        return
    broker = session.scalar(select(Broker).where(Broker.code == ref["code"]))
    if broker is None:
        broker = Broker(code=ref["code"], name=ref["name"], capabilities=ref["capabilities"])
        session.add(broker)
        session.flush()
    for route in ref["environments"]:
        installed = session.scalar(
            select(BrokerEnvironment).where(
                BrokerEnvironment.broker_id == broker.broker_id,
                BrokerEnvironment.environment == route["environment"],
                BrokerEnvironment.region == route["region"],
            )
        )
        if installed is None:
            session.add(BrokerEnvironment(broker_id=broker.broker_id, **route))
    session.flush()


def register_brokers(session: Session, references: list[dict[str, Any]]) -> None:
    """Create missing broker routes; account/credential ownership is a separate transaction."""
    _validate_broker_references(references)
    lock_catalogue(session)
    _check_broker_references(session, references)
    for ref in references:
        _create_broker_reference(session, ref)


def check_catalogue(
    session: Session,
    *,
    instruments: list[dict[str, Any]],
    releases: list[StrategyRelease],
    brokers: list[dict[str, Any]],
) -> dict[str, int]:
    """Validate current reference differences without locks, inserts or sequence use."""
    if session.new or session.dirty or session.deleted:
        msg = "Catalogue dry-run requires a clean caller session"
        raise ValueError(msg)
    with session.no_autoflush:
        return _check_catalogue(
            session, instruments=instruments, releases=releases, brokers=brokers
        )


def _check_catalogue(
    session: Session,
    *,
    instruments: list[dict[str, Any]],
    releases: list[StrategyRelease],
    brokers: list[dict[str, Any]],
) -> dict[str, int]:
    refs = [InstrumentReference.parse(item) for item in instruments]
    current, sectors = _check_reference_batch(session, refs)
    _validate_broker_references(brokers)
    _check_broker_references(session, brokers)
    _validate_strategy_releases(releases)
    _check_strategy_releases(session, releases)
    counts = {
        "instruments": sum(normalize_product_symbol(ref.symbol) not in current for ref in refs),
        "sectors": len({code for ref in refs for code, _ in ref.hierarchy()} - sectors.keys()),
        "instrument_sectors": sum(
            (instrument := current.get(normalize_product_symbol(ref.symbol))) is None
            or (sector := sectors.get(code)) is None
            or session.get(InstrumentSector, (instrument.instr_id, sector.sector_id)) is None
            for ref in refs
            for code, _ in ref.hierarchy()
        ),
        "strategies": sum(
            session.get(Strategy, key) is None
            for key in {release.strategy_id for release in releases}
        ),
        "versions": sum(
            session.scalar(
                select(StrategyVersion.strat_ver_id).where(
                    StrategyVersion.strategy_id == release.strategy_id,
                    StrategyVersion.semver == release.semver,
                )
            )
            is None
            for release in releases
        ),
        "brokers": 0,
        "environments": 0,
    }
    for ref in brokers:
        broker_id = session.scalar(select(Broker.broker_id).where(Broker.code == ref["code"]))
        counts["brokers"] += int(broker_id is None)
        counts["environments"] += sum(
            broker_id is None
            or session.scalar(
                select(BrokerEnvironment.broker_env_id).where(
                    BrokerEnvironment.broker_id == broker_id,
                    BrokerEnvironment.environment == route["environment"],
                    BrokerEnvironment.region == route["region"],
                )
            )
            is None
            for route in ref["environments"]
        )
    return counts
