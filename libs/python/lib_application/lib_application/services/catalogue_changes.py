"""Reviewed static metadata and explicit instrument links; callers own transactions.

Instrument financial/session terms and release authority have no patch surface.
Adapter capabilities are supplied by the composition root, never inferred here.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lib_application.db.models import (
    ApiAuditLog,
    Broker,
    BrokerEnvironment,
    Instrument,
    InstrumentAlias,
    InstrumentBrokerSymbol,
    Sector,
)
from lib_application.db.session import tenant_scope
from lib_application.services.catalogue import InstrumentReference, lock_catalogue
from lib_application.services.database_authority import require_maintenance_database_role
from lib_application.services.deployment_owner import (
    DeploymentOwnerError,
    require_deployment_owner_id,
)
from lib_data.market_data import normalize_product_symbol

_NAME_LENGTH = 255
_DESCRIPTION_LENGTH = 10000
_KEYS = {
    "broker": {"code"},
    "broker_environment": {"broker_code", "environment", "region"},
    "sector": {"code"},
}
_FIELDS = {
    "broker": {"name", "capabilities"},
    "broker_environment": {"base_urls", "rate_limits"},
    "sector": {"name", "description"},
}


@dataclass(frozen=True)
class CatalogueChangeResult:
    kind: str
    key: dict[str, str]
    record_id: int | None
    changed_fields: tuple[str, ...]


@dataclass(frozen=True)
class CataloguePatch:
    kind: str
    key: dict[str, str]
    expected: dict[str, Any]
    changes: dict[str, Any]


def _nonblank(value: Any, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
    ):
        msg = f"Invalid catalogue {label}"
        raise ValueError(msg)
    return value


def validate_environment_metadata(*, broker_code: str, field: str, value: Any) -> None:
    """Reject credential-bearing endpoints and nonpositive/nonfinite rate limits."""
    if not isinstance(value, dict):
        msg = f"{field} must be an object"
        raise TypeError(msg)
    if field == "rate_limits":
        for key, rate in value.items():
            _nonblank(key, "rate limit key", 100)
            try:
                finite = math.isfinite(rate) if isinstance(rate, (int, float)) else False
            except OverflowError:
                finite = False
            if (
                isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or not finite
                or rate <= 0
            ):
                msg = "Rate limits must be finite positive numbers"
                raise ValueError(msg)
        return
    if set(value) - {"rest", "ws", "gateway", "transport"}:
        msg = "Unsupported broker endpoint fields"
        raise ValueError(msg)
    for key, endpoint in value.items():
        if key == "transport":
            if broker_code != "paper" or endpoint != "in_process":
                msg = "Only local paper supports in_process transport"
                raise ValueError(msg)
            continue
        if endpoint is None:
            continue
        endpoint_text = _nonblank(endpoint, "endpoint", 2048)
        try:
            parsed = urlsplit(endpoint_text)
            valid = parsed.scheme in {"https", "wss"} and parsed.hostname and parsed.port != 0
        except ValueError as exc:
            msg = "Invalid broker endpoint"
            raise ValueError(msg) from exc
        if (
            not valid
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "%" in parsed.netloc
            or any(char.isspace() for char in endpoint_text)
        ):
            msg = "Broker endpoints require https/wss without credentials, query or fragment"
            raise ValueError(msg)


def validate_catalogue_changes(
    patches: list[dict[str, Any]],
    *,
    broker_capabilities: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[CataloguePatch]:
    """Validate the entire envelope before database reads or mutations."""
    parsed: list[CataloguePatch] = []
    seen: set[tuple[str, str]] = set()
    for item in patches:
        if not isinstance(item, dict) or set(item) != {"kind", "key", "expected", "changes"}:
            msg = "Catalogue changes require kind, key, expected and changes only"
            raise ValueError(msg)
        kind = item["kind"]
        if kind == "instrument":
            msg = "Instrument authority and financial terms require explicit recataloguing"
            raise ValueError(msg)
        if not isinstance(kind, str) or kind not in _FIELDS:
            msg = "Unsupported catalogue patch kind; release activation is not a metadata patch"
            raise ValueError(msg)
        key, expected, changes = (item[name] for name in ("key", "expected", "changes"))
        if not isinstance(key, dict) or set(key) != _KEYS[kind]:
            msg = f"Invalid {kind} stable key"
            raise ValueError(msg)
        key = {name: _nonblank(value, name, 50) for name, value in key.items()}
        if kind == "broker_environment" and key["environment"] not in {"paper", "live"}:
            msg = "Broker environment must be paper or live"
            raise ValueError(msg)
        identity = (kind, json.dumps(key, sort_keys=True))
        if identity in seen:
            msg = "Duplicate catalogue patch target"
            raise ValueError(msg)
        seen.add(identity)
        if (
            not isinstance(expected, dict)
            or not isinstance(changes, dict)
            or not changes
            or set(expected) != set(changes)
            or set(changes) - _FIELDS[kind]
        ):
            msg = "Every permitted changed field requires its expected current value"
            raise ValueError(msg)
        json.dumps(expected, allow_nan=False)
        json.dumps(changes, allow_nan=False)
        for field, value in changes.items():
            if field == "name":
                _nonblank(value, "name", _NAME_LENGTH)
            elif field == "description":
                if value is not None and (
                    not isinstance(value, str) or len(value) > _DESCRIPTION_LENGTH
                ):
                    msg = "Invalid sector description"
                    raise ValueError(msg)
            elif field == "capabilities":
                if (
                    not isinstance(value, dict)
                    or broker_capabilities is None
                    or key["code"] not in broker_capabilities
                    or value != broker_capabilities[key["code"]]
                ):
                    msg = "Broker capabilities must match the authoritative adapter reference"
                    raise ValueError(msg)
            else:
                validate_environment_metadata(
                    broker_code=key["broker_code"], field=field, value=value
                )
        parsed.append(CataloguePatch(kind, dict(key), dict(expected), dict(changes)))
    return parsed


def _resolve_patch(session: Session, patch: CataloguePatch) -> tuple[Any, int]:
    query: Any
    if patch.kind == "broker":
        query = select(Broker).where(Broker.code == patch.key["code"])
    elif patch.kind == "sector":
        query = select(Sector).where(Sector.code == patch.key["code"])
    else:
        query = (
            select(BrokerEnvironment)
            .join(Broker)
            .where(
                Broker.code == patch.key["broker_code"],
                BrokerEnvironment.environment == patch.key["environment"],
                BrokerEnvironment.region == patch.key["region"],
            )
        )
    row = session.scalar(query.execution_options(populate_existing=True))
    if row is None:
        msg = f"Missing catalogue {patch.kind} target"
        raise ValueError(msg)
    identity = getattr(
        row,
        {"broker": "broker_id", "sector": "sector_id", "broker_environment": "broker_env_id"}[
            patch.kind
        ],
    )
    for field, old in patch.expected.items():
        current = getattr(row, field)
        if current != old and current != patch.changes[field]:
            msg = f"Catalogue {patch.kind} {field} does not match expected value"
            raise ValueError(msg)
    return row, int(identity)


def _audit(session: Session, owner_id: str | None, result: CatalogueChangeResult) -> None:
    # Field names and stable keys are sufficient audit metadata; never copy old
    # endpoint strings or arbitrary capability payloads into request logs.
    session.add(
        ApiAuditLog(
            user_id=owner_id,
            action=f"catalogue.{result.kind}",
            status="ok",
            req={"key": result.key, "fields": list(result.changed_fields)},
            resp={"record_id": result.record_id},
        )
    )


def _apply_patch(session: Session, patch: CataloguePatch, row: Any) -> None:
    if session.get_bind().dialect.name == "postgresql":
        args = {
            "expected": json.dumps(patch.expected, allow_nan=False),
            "changes": json.dumps(patch.changes, allow_nan=False),
            **patch.key,
        }
        if patch.kind == "broker_environment":
            sql = (
                "SELECT public.vm_catalogue_patch_broker_environment(:broker_code, "
                ":environment, :region, CAST(:expected AS jsonb), CAST(:changes AS jsonb))"
            )
        elif patch.kind == "broker":
            sql = (
                "SELECT public.vm_catalogue_patch_broker(:code, "
                "CAST(:expected AS jsonb), CAST(:changes AS jsonb))"
            )
        else:
            sql = (
                "SELECT public.vm_catalogue_patch_sector(:code, "
                "CAST(:expected AS jsonb), CAST(:changes AS jsonb))"
            )
        session.execute(text(sql), args)
        session.expire(row)
    else:
        for field, value in patch.changes.items():
            setattr(row, field, value)


def apply_catalogue_changes(
    session: Session,
    patches: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    broker_capabilities: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[CatalogueChangeResult]:
    parsed = validate_catalogue_changes(patches, broker_capabilities=broker_capabilities)
    if dry_run and (session.new or session.dirty or session.deleted):
        msg = "Catalogue dry-run requires a clean caller session"
        raise ValueError(msg)
    with session.no_autoflush:
        owner_id = require_deployment_owner_id(session)
        if not dry_run:
            lock_catalogue(session)
        resolved = [(patch, *_resolve_patch(session, patch)) for patch in parsed]
        results = [
            CatalogueChangeResult(
                patch.kind,
                patch.key,
                identity,
                tuple(
                    sorted(
                        field
                        for field, value in patch.changes.items()
                        if getattr(row, field) != value
                    )
                ),
            )
            for patch, row, identity in resolved
        ]
        if dry_run:
            return results
        with tenant_scope(session, user_id=owner_id):
            for (patch, row, _identity), result in zip(resolved, results, strict=True):
                if result.changed_fields:
                    _apply_patch(session, patch, row)
                    _audit(session, owner_id, result)
            session.flush()
        return results


@dataclass(frozen=True)
class InstrumentLink:
    canonical: str
    asset_class: str
    kind: str
    values: dict[str, str | None]


def validate_instrument_links(
    definitions: list[dict[str, Any]],
    *,
    broker_references: list[dict[str, Any]] | None = None,
) -> list[InstrumentLink]:
    """Use supplied exact identities only; never derive a venue contract from spelling."""
    known_brokers = (
        {ref["code"] for ref in broker_references} if broker_references is not None else None
    )
    links: list[InstrumentLink] = []
    seen: set[tuple[str, str]] = set()
    instruments: set[str] = set()
    for definition in definitions:
        ref = InstrumentReference.parse(definition)
        canonical = normalize_product_symbol(ref.symbol)
        if canonical in instruments:
            msg = "Duplicate instrument definition"
            raise ValueError(msg)
        instruments.add(canonical)
        for field in ("aliases", "broker_symbols"):
            records = definition.get(field, [])
            if not isinstance(records, list):
                msg = f"Instrument {field} must be a list"
                raise TypeError(msg)
            for item in records:
                if not isinstance(item, dict):
                    msg = "Instrument links must be objects"
                    raise TypeError(msg)
                values: dict[str, str | None]
                if field == "aliases":
                    if set(item) != {"alias", "source"}:
                        msg = "Explicit aliases require alias and source"
                        raise ValueError(msg)
                    values = {
                        "alias": _nonblank(item["alias"], "alias", 100),
                        "source": _nonblank(item["source"], "alias source", 50),
                    }
                    key = ("alias", str(values["alias"]))
                else:
                    required = {"broker_code", "broker_symbol"}
                    optional = {"broker_instrument_id", "broker_instrument_type"}
                    if not required <= item.keys() or set(item) - required - optional:
                        msg = "Invalid explicit broker symbol fields"
                        raise ValueError(msg)
                    values = {
                        "broker_code": _nonblank(item["broker_code"], "broker code", 50),
                        "broker_symbol": _nonblank(item["broker_symbol"], "broker symbol", 100),
                    }
                    for name, maximum in (
                        ("broker_instrument_id", 255),
                        ("broker_instrument_type", 100),
                    ):
                        values[name] = (
                            _nonblank(item[name], name, maximum)
                            if item.get(name) is not None
                            else None
                        )
                    if known_brokers is not None and values["broker_code"] not in known_brokers:
                        msg = "Instrument mapping references an unknown broker source"
                        raise ValueError(msg)
                    if ref.asset_class == "crypto" and (
                        values["broker_code"] in {"deribit", "delta"}
                        or "PERPETUAL" in str(values["broker_symbol"]).upper()
                    ):
                        msg = (
                            "Derivative mappings require an explicit derivative instrument "
                            "and contract terms"
                        )
                        raise ValueError(msg)
                    key = (canonical, str(values["broker_code"]))
                if key in seen:
                    msg = "Duplicate instrument link"
                    raise ValueError(msg)
                seen.add(key)
                links.append(InstrumentLink(ref.symbol, ref.asset_class, field, values))
    _check_link_batch_identities(links, instruments)
    return links


def _check_link_batch_identities(links: list[InstrumentLink], instruments: set[str]) -> None:
    aliases = {canonical: canonical for canonical in instruments}
    venue_ids: dict[tuple[str | None, str | None, str | None], str] = {}
    for link in links:
        canonical = normalize_product_symbol(link.canonical)
        if link.kind == "aliases":
            token = normalize_product_symbol(str(link.values["alias"]))
            if not token or (token in aliases and aliases[token] != canonical):
                msg = "Conflicting normalized alias in source batch"
                raise ValueError(msg)
            aliases[token] = canonical
        elif link.values.get("broker_instrument_id") is not None:
            venue_key = (
                link.values["broker_code"],
                link.values["broker_instrument_id"],
                link.values["broker_instrument_type"],
            )
            if venue_key in venue_ids and venue_ids[venue_key] != canonical:
                msg = "Duplicate broker exact identity in source batch"
                raise ValueError(msg)
            venue_ids[venue_key] = canonical


def _resolve_link(
    session: Session,
    link: InstrumentLink,
    *,
    dry_run: bool,
    planned_brokers: set[str],
) -> tuple[Instrument | None, Broker | None, Any]:
    instruments = list(session.scalars(select(Instrument)))
    matches = [
        row
        for row in instruments
        if normalize_product_symbol(row.canonical) == normalize_product_symbol(link.canonical)
    ]
    if len(matches) > 1 or (not matches and not dry_run):
        msg = "Missing or ambiguous instrument link identity"
        raise ValueError(msg)
    instrument = matches[0] if matches else None
    if instrument is not None and instrument.asset_class != link.asset_class:
        msg = "Instrument link asset class differs from the installed identity"
        raise ValueError(msg)
    existing: Any
    if link.kind == "aliases":
        return instrument, None, _installed_alias(session, link, instrument, instruments)
    broker = session.scalar(select(Broker).where(Broker.code == link.values["broker_code"]))
    if broker is None and not (dry_run and link.values["broker_code"] in planned_brokers):
        msg = "Missing broker for instrument mapping"
        raise ValueError(msg)
    existing = None
    if instrument is not None and broker is not None:
        existing = session.get(
            InstrumentBrokerSymbol, (instrument.instr_id, broker.broker_id), populate_existing=True
        )
        if existing is not None:
            for field in ("broker_symbol", "broker_instrument_id", "broker_instrument_type"):
                value = link.values.get(field)
                if value is not None and getattr(existing, field) != value:
                    msg = "Immutable broker instrument mapping conflict"
                    raise ValueError(msg)
    if broker is not None and link.values.get("broker_instrument_id") is not None:
        collision_query = select(InstrumentBrokerSymbol).where(
            InstrumentBrokerSymbol.broker_id == broker.broker_id,
            InstrumentBrokerSymbol.broker_instrument_id == link.values["broker_instrument_id"],
            InstrumentBrokerSymbol.broker_instrument_type
            == link.values.get("broker_instrument_type"),
        )
        if instrument is not None:
            collision_query = collision_query.where(
                InstrumentBrokerSymbol.instr_id != instrument.instr_id
            )
        if session.scalar(collision_query) is not None:
            msg = "Broker exact identity already belongs to another instrument"
            raise ValueError(msg)
    return instrument, broker, existing


def _installed_alias(
    session: Session,
    link: InstrumentLink,
    instrument: Instrument | None,
    instruments: list[Instrument],
) -> InstrumentAlias | None:
    alias = str(link.values["alias"])
    candidates = session.scalars(select(InstrumentAlias)).all()
    for row in candidates:
        if normalize_product_symbol(row.alias) == normalize_product_symbol(alias) and (
            instrument is None or row.instr_id != instrument.instr_id
        ):
            msg = "Alias conflicts with another instrument identity"
            raise ValueError(msg)
    for catalogue_row in instruments:
        if normalize_product_symbol(catalogue_row.canonical) == normalize_product_symbol(
            alias
        ) and (instrument is None or catalogue_row.instr_id != instrument.instr_id):
            msg = "Alias conflicts with another canonical instrument"
            raise ValueError(msg)
    existing = session.scalar(
        select(InstrumentAlias)
        .where(InstrumentAlias.alias == alias)
        .execution_options(populate_existing=True)
    )
    if existing is not None and existing.source != link.values["source"]:
        msg = "Alias source conflict"
        raise ValueError(msg)
    return existing


def register_instrument_links(
    session: Session,
    definitions: list[dict[str, Any]],
    *,
    broker_references: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
    maintenance: bool = False,
) -> list[CatalogueChangeResult]:
    links = validate_instrument_links(definitions, broker_references=broker_references)
    if dry_run and (session.new or session.dirty or session.deleted):
        msg = "Catalogue dry-run requires a clean caller session"
        raise ValueError(msg)
    planned_brokers = {ref["code"] for ref in broker_references or []}
    with session.no_autoflush:
        if maintenance:
            require_maintenance_database_role(session)
            try:
                owner_id: str | None = require_deployment_owner_id(session)
            except DeploymentOwnerError:
                owner_id = None
        else:
            owner_id = require_deployment_owner_id(session)
        if not dry_run:
            lock_catalogue(session)
        resolved = [
            (link, *_resolve_link(session, link, dry_run=dry_run, planned_brokers=planned_brokers))
            for link in links
        ]
        results = []
        with (
            tenant_scope(session, user_id=owner_id)
            if owner_id is not None and not dry_run
            else nullcontext()
        ):
            for link, instrument, broker, existing in resolved:
                identity = int(instrument.instr_id) if instrument else None
                key = {
                    "canonical": link.canonical,
                    **{
                        name: str(value)
                        for name, value in link.values.items()
                        if name in {"alias", "broker_code"}
                    },
                }
                result = CatalogueChangeResult(
                    link.kind, key, identity, tuple(sorted(link.values)) if existing is None else ()
                )
                results.append(result)
                if dry_run or existing is not None:
                    continue
                assert instrument is not None
                if session.get_bind().dialect.name == "postgresql":
                    if link.kind == "aliases":
                        session.execute(
                            text(
                                "SELECT public.vm_catalogue_create_alias("
                                ":canonical, :alias, :source)"
                            ),
                            {"canonical": link.canonical, **link.values},
                        )
                    else:
                        session.execute(
                            text(
                                "SELECT public.vm_catalogue_create_broker_symbol(:canonical, "
                                ":broker_code, :broker_symbol, :broker_instrument_id, "
                                ":broker_instrument_type)"
                            ),
                            {"canonical": link.canonical, **link.values},
                        )
                elif link.kind == "aliases":
                    session.add(InstrumentAlias(instr_id=instrument.instr_id, **link.values))
                else:
                    assert broker is not None
                    session.add(
                        InstrumentBrokerSymbol(
                            instr_id=instrument.instr_id,
                            broker_id=broker.broker_id,
                            **{
                                name: value
                                for name, value in link.values.items()
                                if name != "broker_code"
                            },
                        )
                    )
                _audit(session, owner_id, result)
            if not dry_run:
                session.flush()
        return results
