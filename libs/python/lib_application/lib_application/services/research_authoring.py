"""Frozen research source revisions; submitted Python is never imported here."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from lib_application.db.models._base import generate_uuid
from lib_application.db.models.research_authoring import ResearchDraft, ResearchDraftRevision
from lib_application.services.control_audit import append_audit
from lib_application.services.research_admission import (
    ResearchWorkflowError,
    require_research_admission,
    transaction_lock,
)
from lib_application.services.research_data_common import (
    ResearchDataError,
    decode_cursor,
    encode_cursor,
)

SOURCE_LIMIT = 256 * 1024
PROTOCOL_LIMIT = 64 * 1024
MAX_PAGE_SIZE = 100
FIRST_PRINTABLE = 32
HASH_INPUT_LIMIT = 32 * 1024 * 1024


def canonical_json(value: Any, *, limit: int = PROTOCOL_LIMIT) -> str:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        encoded_bytes = encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        msg = "payload must be finite UTF-8 JSON"
        raise ResearchWorkflowError(msg) from exc
    if len(encoded_bytes) > limit:
        msg = f"JSON payload exceeds {limit} bytes"
        raise ResearchWorkflowError(msg)
    return encoded


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value, limit=HASH_INPUT_LIMIT).encode()).hexdigest()


def identity(value: str, field: str, *, maximum: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
        or any(ord(char) < FIRST_PRINTABLE for char in value)
    ):
        msg = f"{field} must be nonempty and at most {maximum} characters"
        raise ResearchWorkflowError(msg)
    return value


def serialize_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()


def page_cursor(value: str) -> tuple[datetime, str]:
    try:
        decoded = decode_cursor(value)
    except ResearchDataError as exc:
        raise ResearchWorkflowError(exc.detail, status_code=exc.status_code) from exc
    if decoded is None:
        msg = "invalid cursor"
        raise ResearchWorkflowError(msg)
    return decoded


def audit(session: Session, owner_id: str, action: str, object_id: str) -> None:
    append_audit(
        session,
        user_id=owner_id,
        action="research." + action,
        request_payload={"object_id": object_id},
    )


def _content(source: str, config: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(source, str) or not source.strip() or "\x00" in source:
        msg = "source must be nonempty Python text"
        raise ResearchWorkflowError(msg)
    try:
        encoded = source.encode("utf-8")
    except UnicodeError as exc:
        msg = "source must be valid UTF-8"
        raise ResearchWorkflowError(msg) from exc
    if len(encoded) > SOURCE_LIMIT:
        msg = f"source exceeds {SOURCE_LIMIT} bytes"
        raise ResearchWorkflowError(msg)
    if not isinstance(config, dict) or set(config) != {"class_name", "strategy_id", "parameters"}:
        msg = "config requires exactly class_name, strategy_id and parameters"
        raise ResearchWorkflowError(msg)
    if (
        config["class_name"] != "UserStrategy"
        or not isinstance(config["strategy_id"], str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", config["strategy_id"]) is None
        or not isinstance(config["parameters"], dict)
    ):
        msg = "invalid research strategy config"
        raise ResearchWorkflowError(msg)
    if "research_model" in config["parameters"]:
        msg = "parameters.research_model is reserved for the selected immutable model artifact"
        raise ResearchWorkflowError(msg)
    canonical_json(config)
    return hashlib.sha256(encoded).hexdigest(), content_hash({"source": source, "config": config})


def _draft(session: Session, owner_id: str, draft_id: str, *, lock: bool = False) -> ResearchDraft:
    query = (
        select(ResearchDraft)
        .where(ResearchDraft.owner_id == owner_id, ResearchDraft.draft_id == draft_id)
        .execution_options(populate_existing=True)
    )
    row = session.scalar(query.with_for_update() if lock else query)
    if row is None:
        msg = "draft not found"
        raise ResearchWorkflowError(msg, status_code=404)
    return row


def get_revision(session: Session, owner_id: str, revision_id: str) -> ResearchDraftRevision:
    row = session.scalar(
        select(ResearchDraftRevision).where(
            ResearchDraftRevision.owner_id == owner_id,
            ResearchDraftRevision.revision_id == revision_id,
        )
    )
    if row is None:
        msg = "revision not found"
        raise ResearchWorkflowError(msg, status_code=404)
    return row


def _view(session: Session, row: ResearchDraft, revision_id: str | None = None) -> dict[str, Any]:
    revision = get_revision(session, row.owner_id, revision_id or row.latest_revision_id or "")
    if revision.draft_id != row.draft_id:
        msg = "revision not found"
        raise ResearchWorkflowError(msg, status_code=404)
    return {
        "draft_id": row.draft_id,
        "revision_id": revision.revision_id,
        "revision": revision.revision,
        "name": row.name,
        "source": revision.source,
        "config": json.loads(canonical_json(revision.config_json)),
        "source_hash": revision.source_hash,
        "content_hash": revision.content_hash,
        "created_at": serialize_time(revision.created_at),
    }


def create_draft(
    session: Session,
    owner_id: str,
    *,
    request_id: str,
    name: str,
    source: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    require_research_admission(session)
    identity(request_id, "request_id")
    identity(name, "name")
    source_hash, revision_hash = _content(source, config)
    request_hash = content_hash({"name": name, "content_hash": revision_hash})
    transaction_lock(session, "owner:" + owner_id)
    previous = session.scalar(
        select(ResearchDraft).where(
            ResearchDraft.owner_id == owner_id, ResearchDraft.request_id == request_id
        )
    )
    if previous is not None:
        if previous.request_hash != request_hash:
            msg = "request_id already used with different content"
            raise ResearchWorkflowError(msg, status_code=409)
        return _view(session, previous)
    draft = ResearchDraft(
        draft_id=generate_uuid(),
        owner_id=owner_id,
        request_id=request_id,
        request_hash=request_hash,
        name=name,
        revision=0,
    )
    session.add(draft)
    session.flush()
    revision = ResearchDraftRevision(
        revision_id=generate_uuid(),
        owner_id=owner_id,
        draft_id=draft.draft_id,
        revision=1,
        source=source,
        config_json=json.loads(canonical_json(config)),
        source_hash=source_hash,
        content_hash=revision_hash,
    )
    session.add(revision)
    session.flush()
    draft.revision, draft.latest_revision_id = 1, revision.revision_id
    audit(session, owner_id, "draft.create", draft.draft_id)
    session.flush()
    return _view(session, draft)


def update_draft(
    session: Session,
    owner_id: str,
    draft_id: str,
    *,
    expected_revision: int,
    name: str,
    source: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    require_research_admission(session)
    identity(name, "name")
    source_hash, revision_hash = _content(source, config)
    transaction_lock(session, "owner:" + owner_id)
    draft = _draft(session, owner_id, draft_id, lock=True)
    if type(expected_revision) is not int or draft.revision != expected_revision:
        msg = "draft revision changed; reread before saving"
        raise ResearchWorkflowError(msg, status_code=409)
    revision = ResearchDraftRevision(
        revision_id=generate_uuid(),
        owner_id=owner_id,
        draft_id=draft_id,
        revision=draft.revision + 1,
        source=source,
        config_json=json.loads(canonical_json(config)),
        source_hash=source_hash,
        content_hash=revision_hash,
    )
    session.add(revision)
    session.flush()
    draft.name, draft.revision, draft.latest_revision_id = (
        name,
        revision.revision,
        revision.revision_id,
    )
    audit(session, owner_id, "draft.revise", revision.revision_id)
    session.flush()
    return _view(session, draft)


def get_draft(session: Session, owner_id: str, draft_id: str) -> dict[str, Any]:
    return _view(session, _draft(session, owner_id, draft_id))


def export_draft(
    session: Session, owner_id: str, draft_id: str, *, revision_id: str | None = None
) -> dict[str, Any]:
    return _view(session, _draft(session, owner_id, draft_id), revision_id)


def list_drafts(
    session: Session, owner_id: str, *, after: str | None = None, limit: int = 50
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        msg = "limit must be between 1 and 100"
        raise ResearchWorkflowError(msg)
    query = (
        select(
            ResearchDraft.draft_id,
            ResearchDraft.name,
            ResearchDraft.revision,
            ResearchDraft.latest_revision_id.label("revision_id"),
            ResearchDraftRevision.source_hash,
            ResearchDraftRevision.content_hash,
            ResearchDraft.created_at,
        )
        .join(
            ResearchDraftRevision,
            (ResearchDraftRevision.owner_id == ResearchDraft.owner_id)
            & (ResearchDraftRevision.revision_id == ResearchDraft.latest_revision_id),
        )
        .where(ResearchDraft.owner_id == owner_id)
    )
    if after:
        timestamp, key = page_cursor(after)
        query = query.where(
            or_(
                ResearchDraft.created_at < timestamp,
                and_(ResearchDraft.created_at == timestamp, ResearchDraft.draft_id < key),
            )
        )
    rows = list(
        session.execute(
            query.order_by(ResearchDraft.created_at.desc(), ResearchDraft.draft_id.desc()).limit(
                limit + 1
            )
        ).mappings()
    )
    return {
        "drafts": [
            {**dict(row), "created_at": serialize_time(row["created_at"])} for row in rows[:limit]
        ],
        "next_after": encode_cursor(rows[limit - 1]["created_at"], rows[limit - 1]["draft_id"])
        if len(rows) > limit
        else None,
    }
