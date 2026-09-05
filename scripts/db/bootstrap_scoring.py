"""Seed the source-controlled instrument hierarchy into an Alembic-managed DB.

Usage:
    python scripts/db/bootstrap_scoring.py \
        --db postgresql://user:pass@localhost:5432/vm_trading \
        --instruments config/instruments.yaml

DATABASE_URL is required (or pass --db explicitly). SQLite is not supported
here. The database must already be at Alembic head; this catalogue loader never
creates schema objects or masks migration drift.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Ensure repo root on path
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(REPO_ROOT))

from lib_application.db.models import (  # noqa: E402
    Instrument,
    InstrumentSector,
    Sector,
)
from lib_common.asset_classes import (  # noqa: E402
    REFERENCE_ONLY_ASSET_CLASSES,
    normalize_asset_class,
)


def load_instruments(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text())
    instruments = data.get("instruments", [])
    return instruments if isinstance(instruments, list) else []


def _normalize_asset_class(value: str | None) -> str:
    if not value:
        msg = "Instrument asset_class is required"
        raise ValueError(msg)
    return normalize_asset_class(value, field_name="instrument asset_class")


def _resolve_is_tradable(
    instrument: dict[str, Any],
    *,
    asset_class: str,
    symbol: str,
) -> bool:
    default = asset_class not in REFERENCE_ONLY_ASSET_CLASSES
    value = instrument.get("is_tradable", default)
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


def _ensure_sector(
    session: Session,
    code: str,
    asset_class: str,
    parent: Sector | None = None,
) -> Sector:
    existing: Sector | None = session.query(Sector).filter_by(code=code).first()
    if existing:
        if existing.asset_class != asset_class:
            msg = (
                f"Sector {code!r} belongs to asset_class={existing.asset_class!r}, "
                f"not {asset_class!r}"
            )
            raise ValueError(msg)
        expected_parent_id = parent.sector_id if parent else None
        if existing.parent_sector_id != expected_parent_id:
            msg = (
                f"Sector {code!r} has parent_sector_id={existing.parent_sector_id!r}, "
                f"not {expected_parent_id!r}"
            )
            raise ValueError(msg)
        return existing
    sector = Sector(
        code=code,
        name=code,
        asset_class=asset_class,
        parent_sector_id=parent.sector_id if parent else None,
    )
    session.add(sector)
    session.flush()
    return sector


def _ensure_instrument_sector(session: Session, instr_id: int, sector_id: int) -> None:
    existing = (
        session.query(InstrumentSector).filter_by(instr_id=instr_id, sector_id=sector_id).first()
    )
    if existing:
        return
    session.add(InstrumentSector(instr_id=instr_id, sector_id=sector_id, weight=1.0))


def upsert_instruments(session: Session, instruments: list[dict[str, Any]]) -> None:
    for inst in instruments:
        symbol = inst["symbol"].upper()
        asset_class = _normalize_asset_class(inst.get("asset_class"))
        is_tradable = _resolve_is_tradable(
            inst,
            asset_class=asset_class,
            symbol=symbol,
        )
        settlement_currency = str(inst.get("settlement_currency") or "").strip().upper()
        if not settlement_currency:
            msg = f"{symbol} is missing settlement_currency"
            raise ValueError(msg)
        market_session_policy = str(inst.get("market_session_policy") or "").strip().lower()
        expected_policy = "continuous" if asset_class == "crypto" else "scheduled"
        if market_session_policy != expected_policy:
            msg = (
                f"{symbol} market_session_policy must be {expected_policy!r} "
                f"for asset_class={asset_class!r}"
            )
            raise ValueError(msg)

        instrument = session.query(Instrument).filter(Instrument.canonical == symbol).first()
        if not instrument:
            instrument = Instrument(
                canonical=symbol,
                asset_class=asset_class,
                settlement_currency=settlement_currency,
                market_session_policy=market_session_policy,
                is_tradable=is_tradable,
            )
            session.add(instrument)
            session.flush()
        else:
            instrument.asset_class = asset_class
            instrument.settlement_currency = settlement_currency
            instrument.market_session_policy = market_session_policy
            instrument.is_tradable = is_tradable
            if market_session_policy == "continuous":
                instrument.market_calendar_id = None

        # This file is the hierarchy source of truth. Remove superseded
        # relationships before applying the current sector/industry mapping.
        session.query(InstrumentSector).filter_by(instr_id=instrument.instr_id).delete(
            synchronize_session=False
        )

        sector = None
        sector_code = (inst.get("sector") or "").strip().lower()
        if sector_code:
            sector = _ensure_sector(session, sector_code, asset_class)
            _ensure_instrument_sector(session, instrument.instr_id, sector.sector_id)

        industry_code = (inst.get("industry") or "").strip().lower()
        if industry_code and industry_code != sector_code:
            industry_sector = _ensure_sector(session, industry_code, asset_class, parent=sector)
            _ensure_instrument_sector(session, instrument.instr_id, industry_sector.sector_id)
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Alembic-managed instrument catalogue.")
    parser.add_argument(
        "--db",
        dest="db_url",
        default=None,
        help="Database URL (required). If omitted, DATABASE_URL must be set.",
    )
    parser.add_argument(
        "--instruments",
        dest="instruments_path",
        default="config/instruments.yaml",
        help="Path to instruments YAML to seed hierarchy.",
    )
    args = parser.parse_args()

    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        print("Error: DATABASE_URL is required (or pass --db).", file=sys.stderr)
        raise SystemExit(2)
    engine = create_engine(db_url, future=True)

    # Seed instruments if file exists
    inst_path = Path(args.instruments_path)
    if inst_path.exists():
        instruments = load_instruments(inst_path)
        with Session(engine, expire_on_commit=False) as session:
            upsert_instruments(session, instruments)
        print(f"Seeded {len(instruments)} instruments from {inst_path}")
    else:
        print(f"No instruments file found at {inst_path}, skipping seeding.")

    # Print a quick summary for verification
    with Session(engine, expire_on_commit=False) as session:
        rows = session.query(Instrument).all()
        print(f"Instruments in DB ({len(rows)}):")
        for r in rows:
            print(
                json.dumps(
                    {
                        "symbol": r.canonical,
                        "asset_class": r.asset_class,
                    }
                )
            )


if __name__ == "__main__":
    main()
