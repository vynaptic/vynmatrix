"""Immutable research datasets; callers own transactions and owner authority."""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import UTC
from typing import Any

from sqlalchemy import and_, insert, or_, select
from sqlalchemy.orm import Session

from lib_application.db.models.instruments import InstrumentPrice
from lib_application.db.models.research_data import ResearchBar, ResearchDataset, ResearchImport
from lib_application.services.control_audit import ERROR, OK, append_audit
from lib_application.services.research_admission import require_research_admission
from lib_application.services.research_data_common import (
    OWNER_BYTE_QUOTA,
    ResearchDataError,
    check_quota,
    decode_cursor,
    encode_cursor,
    lock_owner,
    page_limit,
    reference_observation,
    timing_evidence,
)
from lib_data.research_dataset import (
    CSV_HEADER,
    MAX_IMPORT_BARS,
    MAX_IMPORT_BYTES,
    MAX_VALIDATION_ERRORS,
    canonical_json,
    content_hash,
    iso_timestamp,
    normalize_metadata,
    parse_research_csv,
    require_continuous_cadence,
    utc_timestamp,
    validated_text,
)

_DATASET_COLUMNS = tuple(ResearchDataset.__table__.columns)
_IMPORT_COLUMNS = tuple(ResearchImport.__table__.columns)
MAX_DATASET_NAME = 200
_SUMMARY_METADATA = (
    "instrument_id",
    "source",
    "provenance",
    "currency",
    "timeframe",
    "timestamp_semantics",
    "adjustment_policy",
    "supersedes_id",
    "instrument_terms",
)
_SUMMARY_MANIFEST = (
    "schema",
    "content_hash",
    "original_hash",
    "bar_count",
    "first_ts",
    "last_ts",
    "original_recorded_at",
    "eligibility",
)
_SUMMARY_COLUMNS = (
    ResearchDataset.dataset_id,
    ResearchDataset.import_id,
    ResearchDataset.name,
    ResearchDataset.content_hash,
    ResearchDataset.original_hash,
    ResearchDataset.bar_count,
    ResearchDataset.created_at,
    *(ResearchDataset.metadata_json[key].label(f"metadata_{key}") for key in _SUMMARY_METADATA),
    *(ResearchDataset.manifest_json[key].label(f"manifest_{key}") for key in _SUMMARY_MANIFEST),
)


def _dataset_json(row: Any) -> dict[str, Any]:
    values = dict(row)
    return {
        "dataset_id": values["dataset_id"],
        "import_id": values["import_id"],
        "name": values["name"],
        "content_hash": values["content_hash"],
        "original_hash": values["original_hash"],
        "metadata": values["metadata_json"],
        "manifest": values["manifest_json"],
        "bar_count": values["bar_count"],
        "created_at": iso_timestamp(values["created_at"]),
        "eligibility": values["manifest_json"]["eligibility"],
    }


def _receipt_json(row: Any) -> dict[str, Any]:
    return {
        "import_id": row["import_id"],
        "content_hash": row["content_hash"],
        "original_hash": row["original_hash"],
        "valid": row["valid"],
        "report": row["report_json"],
        "metadata": row["metadata_json"],
        "created_at": iso_timestamp(row["created_at"]),
    }


def _persist_import(
    session: Session,
    owner_id: str,
    metadata: dict[str, Any],
    content: bytes,
    *,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if len(content) > MAX_IMPORT_BYTES:
        msg = f"Import byte limit is {MAX_IMPORT_BYTES}"
        raise ResearchDataError(msg, status_code=413)
    try:
        normalized = normalize_metadata(metadata)
    except (ValueError, OverflowError) as exc:
        raise ResearchDataError(str(exc)) from exc
    lock_owner(session, owner_id)
    request_hash = hashlib.sha256(
        canonical_json(
            {
                "metadata": normalized,
                "original_hash": hashlib.sha256(content).hexdigest(),
                "snapshot": snapshot,
            }
        )
    ).hexdigest()
    existing = (
        session.execute(
            select(*_IMPORT_COLUMNS).where(
                ResearchImport.owner_id == owner_id, ResearchImport.request_hash == request_hash
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        return _receipt_json(existing)
    if normalized.get("supersedes_id"):
        get_dataset(session, owner_id, normalized["supersedes_id"])
    try:
        parsed = parse_research_csv(content, normalized)
    except ValueError as exc:
        raise ResearchDataError(
            str(exc), status_code=413 if "byte limit" in str(exc) else 422
        ) from exc
    terms, evidence, sessions = reference_observation(
        session, normalized["instrument_id"], normalized["currency"], parsed["bars"]
    )
    try:
        if terms["asset_class"] == "crypto":
            require_continuous_cadence(parsed["bars"], normalized["timeframe"])
        evidence, eligibility = timing_evidence(
            normalized, terms, parsed["bars"], evidence=evidence, sessions=sessions
        )
    except (ValueError, OverflowError) as exc:
        parsed.update(valid=False, content_hash=None, bars=[])
        parsed["report"].update(bar_count=0, error_count=parsed["report"]["error_count"] + 1)
        if len(parsed["report"]["errors"]) < MAX_VALIDATION_ERRORS:
            parsed["report"]["errors"].append(
                {
                    "line": None,
                    "error": str(exc)
                    if isinstance(exc, ValueError)
                    else "Bar timing exceeds supported UTC timestamp range",
                }
            )
        evidence, eligibility = timing_evidence(
            normalized, terms, [], evidence=evidence, sessions=sessions
        )
    if not parsed["valid"]:
        eligibility.update(training=False, backtest=False)
        for key in ("training_refusals", "backtest_refusals", "refusals"):
            eligibility[key].append("Import validation failed")
    frozen_metadata = {**normalized, "instrument_terms": terms, "timing_evidence": evidence}
    if snapshot is not None:
        frozen_metadata["snapshot"] = snapshot
        eligibility["provenance_strength"] = "stored_price_values_and_revisions"
    parsed["report"]["eligibility"] = eligibility
    parsed["metadata"] = frozen_metadata
    parsed["content_hash"] = (
        content_hash(frozen_metadata, parsed["bars"]) if parsed["valid"] else None
    )
    size = (
        len(content)
        + len(canonical_json(frozen_metadata))
        + len(canonical_json(parsed["report"]))
        + len(canonical_json(parsed["bars"]))
    )
    check_quota(session, owner_id, size, quota=OWNER_BYTE_QUOTA)
    row = ResearchImport(
        owner_id=owner_id,
        request_hash=request_hash,
        original_hash=parsed["original_hash"],
        content_hash=parsed["content_hash"],
        valid=parsed["valid"],
        metadata_json=frozen_metadata,
        report_json=parsed["report"],
        original_csv=content,
        canonical_bars=parsed["bars"],
        stored_bytes=size,
    )
    session.add(row)
    session.flush()
    append_audit(
        session,
        user_id=owner_id,
        action="research.import",
        request_payload={"fields": sorted(normalized)},
        response_payload={
            "import_id": row.import_id,
            "valid": row.valid,
            "original_hash": row.original_hash,
            "content_hash": row.content_hash,
        },
        status=OK if row.valid else ERROR,
    )
    return _receipt_json({column.name: getattr(row, column.name) for column in _IMPORT_COLUMNS})


def import_dataset(
    session: Session, owner_id: str, metadata: dict[str, Any], content: bytes
) -> dict[str, Any]:
    """Persist successful and failed receipts, deduplicated within the owner lock."""
    require_research_admission(session)
    return _persist_import(session, owner_id, metadata, content)


def freeze_dataset(
    session: Session, owner_id: str, import_id: str, expected_hash: str, name: str
) -> dict[str, Any]:
    """Freeze exactly the expected receipt once; name conflicts need a new receipt."""
    require_research_admission(session)
    try:
        name = validated_text(name, "Dataset name", MAX_DATASET_NAME)
    except ValueError as exc:
        raise ResearchDataError(str(exc)) from exc
    lock_owner(session, owner_id)
    receipt = (
        session.execute(
            select(*_IMPORT_COLUMNS).where(
                ResearchImport.owner_id == owner_id, ResearchImport.import_id == import_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if receipt is None:
        msg = "Import not found"
        raise ResearchDataError(msg, status_code=404)
    if not receipt["valid"]:
        msg = "Cannot freeze an invalid import"
        raise ResearchDataError(msg)
    if receipt["content_hash"] != expected_hash:
        msg = "Import content hash changed or does not match"
        raise ResearchDataError(msg, status_code=409)
    existing = (
        session.execute(
            select(*_DATASET_COLUMNS).where(
                ResearchDataset.owner_id == owner_id, ResearchDataset.import_id == import_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["name"] != name:
            msg = "Import already frozen under another name"
            raise ResearchDataError(msg, status_code=409)
        return _dataset_json(existing)
    bars = receipt["canonical_bars"]
    if content_hash(receipt["metadata_json"], bars) != expected_hash:
        msg = "Stored import content hash verification failed"
        raise ResearchDataError(msg, status_code=409)
    manifest = {
        "schema": "research-dataset-v1",
        "content_hash": expected_hash,
        "original_hash": receipt["original_hash"],
        "metadata": receipt["metadata_json"],
        "validation": receipt["report_json"],
        "eligibility": receipt["report_json"]["eligibility"],
        "bar_count": len(bars),
        "first_ts": bars[0]["ts"],
        "last_ts": bars[-1]["ts"],
        "price_revisions": receipt["metadata_json"].get("snapshot", {}).get("price_revisions", []),
        "original_recorded_at": iso_timestamp(receipt["created_at"]),
    }
    size = (
        len(canonical_json(manifest))
        + len(canonical_json(receipt["metadata_json"]))
        + len(canonical_json(bars))
    )
    check_quota(session, owner_id, size, quota=OWNER_BYTE_QUOTA)
    dataset = ResearchDataset(
        owner_id=owner_id,
        import_id=import_id,
        supersedes_id=receipt["metadata_json"].get("supersedes_id"),
        name=name,
        content_hash=expected_hash,
        original_hash=receipt["original_hash"],
        metadata_json=receipt["metadata_json"],
        manifest_json=manifest,
        bar_count=len(bars),
        stored_bytes=size,
    )
    session.add(dataset)
    session.flush()
    for offset in range(0, len(bars), 1000):
        session.execute(
            insert(ResearchBar),
            [
                {
                    "owner_id": owner_id,
                    "dataset_id": dataset.dataset_id,
                    "ordinal": index,
                    "ts": utc_timestamp(bar["ts"]),
                    "bar_json": bar,
                }
                for index, bar in enumerate(bars[offset : offset + 1000], offset)
            ],
        )
    append_audit(
        session,
        user_id=owner_id,
        action="research.dataset.freeze",
        request_payload={"fields": ["name"]},
        response_payload={
            "dataset_id": dataset.dataset_id,
            "import_id": import_id,
            "content_hash": expected_hash,
        },
    )
    return _dataset_json(
        {column.name: getattr(dataset, column.name) for column in _DATASET_COLUMNS}
    )


def list_datasets(
    session: Session, owner_id: str, after: str | None = None, limit: int = 50
) -> dict[str, Any]:
    limit = page_limit(limit)
    query = select(*_SUMMARY_COLUMNS).where(ResearchDataset.owner_id == owner_id)
    cursor = decode_cursor(after)
    if cursor:
        stamp, identity = cursor
        query = query.where(
            or_(
                ResearchDataset.created_at < stamp,
                and_(ResearchDataset.created_at == stamp, ResearchDataset.dataset_id < identity),
            )
        )
    rows = (
        session.execute(
            query.order_by(
                ResearchDataset.created_at.desc(), ResearchDataset.dataset_id.desc()
            ).limit(limit + 1)
        )
        .mappings()
        .all()
    )
    return {
        "datasets": [
            {
                **{
                    key: row[key]
                    for key in (
                        "dataset_id",
                        "import_id",
                        "name",
                        "content_hash",
                        "original_hash",
                        "bar_count",
                    )
                },
                "created_at": iso_timestamp(row["created_at"]),
                "metadata": {
                    key: row[f"metadata_{key}"]
                    for key in _SUMMARY_METADATA
                    if row[f"metadata_{key}"] is not None
                },
                "manifest": {
                    key: row[f"manifest_{key}"]
                    for key in _SUMMARY_MANIFEST
                    if row[f"manifest_{key}"] is not None
                },
                "eligibility": row["manifest_eligibility"],
            }
            for row in rows[:limit]
        ],
        "next_after": encode_cursor(rows[limit - 1]["created_at"], rows[limit - 1]["dataset_id"])
        if len(rows) > limit
        else None,
    }


def get_dataset(
    session: Session, owner_id: str, dataset_id: str, include_bars: bool = False
) -> dict[str, Any]:
    row = (
        session.execute(
            select(*_DATASET_COLUMNS).where(
                ResearchDataset.owner_id == owner_id, ResearchDataset.dataset_id == dataset_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        msg = "Dataset not found"
        raise ResearchDataError(msg, status_code=404)
    result = _dataset_json(row)
    if include_bars:
        result["bars"] = list(
            session.scalars(
                select(ResearchBar.bar_json)
                .where(ResearchBar.owner_id == owner_id, ResearchBar.dataset_id == dataset_id)
                .order_by(ResearchBar.ordinal)
                .limit(MAX_IMPORT_BARS + 1)
            )
        )
        if (
            len(result["bars"]) != result["bar_count"]
            or content_hash(result["metadata"], result["bars"]) != result["content_hash"]
        ):
            msg = "Frozen dataset content hash verification failed"
            raise ResearchDataError(msg, status_code=409)
    return result


def export_dataset(session: Session, owner_id: str, dataset_id: str) -> dict[str, Any]:
    dataset = get_dataset(session, owner_id, dataset_id, include_bars=True)
    original = session.scalar(
        select(ResearchImport.original_csv).where(
            ResearchImport.owner_id == owner_id, ResearchImport.import_id == dataset["import_id"]
        )
    )
    if original is None or hashlib.sha256(original).hexdigest() != dataset["original_hash"]:
        msg = "Original import content hash verification failed"
        raise ResearchDataError(msg, status_code=409)
    return {"original_csv": original.decode("utf-8"), "manifest": dataset["manifest"]}


def snapshot_dataset(session: Session, owner_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Read price values and revisions in one MVCC statement; never mutate prices."""
    require_research_admission(session)
    values = dict(metadata)
    try:
        name = values.pop("name")
        start, end = utc_timestamp(values.pop("start")), utc_timestamp(values.pop("end"))
        normalized = normalize_metadata(values)
    except (KeyError, ValueError, TypeError) as exc:
        msg = "Snapshot requires name, explicit UTC start/end and import metadata"
        raise ResearchDataError(msg) from exc
    if end <= start:
        msg = "Snapshot end must follow start"
        raise ResearchDataError(msg)
    lock_owner(session, owner_id)
    rows = (
        session.execute(
            select(
                InstrumentPrice.price_id,
                InstrumentPrice.content_revision,
                InstrumentPrice.ts,
                InstrumentPrice.open,
                InstrumentPrice.high,
                InstrumentPrice.low,
                InstrumentPrice.close,
                InstrumentPrice.volume,
            )
            .where(
                InstrumentPrice.instr_id == normalized["instrument_id"],
                InstrumentPrice.source == normalized["source"],
                InstrumentPrice.timeframe == normalized["timeframe"],
                InstrumentPrice.ts >= start.astimezone(UTC).replace(tzinfo=None),
                InstrumentPrice.ts < end.astimezone(UTC).replace(tzinfo=None),
            )
            .order_by(InstrumentPrice.ts)
            .limit(MAX_IMPORT_BARS + 1)
        )
        .mappings()
        .all()
    )
    if not rows or len(rows) > MAX_IMPORT_BARS:
        msg = "Snapshot window must contain 1-100000 recorded bars"
        raise ResearchDataError(msg)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow(
            [
                iso_timestamp(row["ts"]),
                *[str(row[key]) if row[key] is not None else "" for key in CSV_HEADER[1:]],
            ]
        )
    snapshot = {
        "start": iso_timestamp(start),
        "end": iso_timestamp(end),
        "price_revisions": [
            {"price_id": row["price_id"], "content_revision": row["content_revision"]}
            for row in rows
        ],
    }
    receipt = _persist_import(
        session, owner_id, normalized, buffer.getvalue().encode("utf-8"), snapshot=snapshot
    )
    if not receipt["valid"]:
        return {"dataset": None, "import": receipt}
    return freeze_dataset(session, owner_id, receipt["import_id"], receipt["content_hash"], name)


from lib_application.services.research_data_reads import market_data  # noqa: E402

__all__ = [
    "ResearchDataError",
    "export_dataset",
    "freeze_dataset",
    "get_dataset",
    "import_dataset",
    "list_datasets",
    "market_data",
    "snapshot_dataset",
]
