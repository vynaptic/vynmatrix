"""Register strict finite JSON from a successful frozen training attempt."""

from __future__ import annotations

import json
from typing import Any, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from lib_application.db.models.research_data import ResearchDataset
from lib_application.db.models.research_jobs import (
    ResearchJob,
    ResearchJobAttempt,
    ResearchModelVersion,
)
from lib_application.services.research_admission import require_research_admission
from lib_application.services.research_authoring import (
    MAX_PAGE_SIZE,
    ResearchWorkflowError,
    audit,
    canonical_json,
    content_hash,
    encode_cursor,
    identity,
    page_cursor,
    serialize_time,
    transaction_lock,
)


def _view(row: ResearchModelVersion) -> dict[str, Any]:
    return {
        "model_id": row.model_id,
        "name": row.name,
        "job_id": row.job_id,
        "attempt_id": row.attempt_id,
        "dataset_id": row.dataset_id,
        "created_at": serialize_time(row.created_at),
        "training_cutoff": row.training_cutoff,
        "metrics": row.metrics_json,
        "artifact": row.artifact_json,
        "artifact_hash": row.artifact_hash,
    }


def get_model(session: Session, owner_id: str, model_id: str) -> dict[str, Any]:
    row = session.scalar(
        select(ResearchModelVersion).where(
            ResearchModelVersion.owner_id == owner_id, ResearchModelVersion.model_id == model_id
        )
    )
    if row is None:
        msg = "model not found"
        raise ResearchWorkflowError(msg, status_code=404)
    return _view(row)


def export_model(session: Session, owner_id: str, model_id: str) -> dict[str, Any]:
    return cast(dict[str, Any], get_model(session, owner_id, model_id)["artifact"])


def list_models(
    session: Session, owner_id: str, *, after: str | None = None, limit: int = 50
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        msg = "limit must be between 1 and 100"
        raise ResearchWorkflowError(msg)
    query = select(ResearchModelVersion).where(ResearchModelVersion.owner_id == owner_id)
    if after:
        timestamp, key = page_cursor(after)
        query = query.where(
            or_(
                ResearchModelVersion.created_at < timestamp,
                and_(
                    ResearchModelVersion.created_at == timestamp,
                    ResearchModelVersion.model_id < key,
                ),
            )
        )
    rows = list(
        session.scalars(
            query.order_by(
                ResearchModelVersion.created_at.desc(), ResearchModelVersion.model_id.desc()
            ).limit(limit + 1)
        )
    )
    return {
        "models": [_view(row) for row in rows[:limit]],
        "next_after": encode_cursor(rows[limit - 1].created_at, rows[limit - 1].model_id)
        if len(rows) > limit
        else None,
    }


def register_model(
    session: Session, owner_id: str, *, request_id: str, job_id: str, name: str
) -> dict[str, Any]:
    from lib_strategy.research import validate_model  # noqa: PLC0415

    require_research_admission(session)
    identity(request_id, "request_id")
    identity(name, "name")
    transaction_lock(session, "control")
    transaction_lock(session, "owner:" + owner_id)
    request_hash = content_hash({"job_id": job_id, "name": name})
    previous = session.scalar(
        select(ResearchModelVersion).where(
            ResearchModelVersion.owner_id == owner_id, ResearchModelVersion.request_id == request_id
        )
    )
    if previous is not None:
        if previous.request_hash != request_hash:
            msg = "request_id already used with different model inputs"
            raise ResearchWorkflowError(msg, status_code=409)
        return _view(previous)
    job = session.scalar(
        select(ResearchJob).where(ResearchJob.owner_id == owner_id, ResearchJob.job_id == job_id)
    )
    if job is None:
        msg = "job not found"
        raise ResearchWorkflowError(msg, status_code=404)
    if job.kind != "train" or job.status != "completed" or job.dataset_id is None:
        msg = "registration requires a successful train job"
        raise ResearchWorkflowError(msg, status_code=409)
    attempt = session.scalar(
        select(ResearchJobAttempt).where(
            ResearchJobAttempt.owner_id == owner_id,
            ResearchJobAttempt.job_id == job_id,
            ResearchJobAttempt.attempt == job.current_attempt,
            ResearchJobAttempt.status == "completed",
        )
    )
    if attempt is None or not isinstance(attempt.result_json, dict):
        msg = "successful training attempt is missing"
        raise ResearchWorkflowError(msg, status_code=409)
    existing = session.scalar(
        select(ResearchModelVersion.model_id).where(
            ResearchModelVersion.owner_id == owner_id,
            ResearchModelVersion.attempt_id == attempt.attempt_id,
        )
    )
    if existing is not None:
        msg = "training attempt is already registered"
        raise ResearchWorkflowError(msg, status_code=409)
    try:
        artifact = validate_model(attempt.result_json["model"])
        metrics = json.loads(canonical_json(attempt.result_json["metrics"]))
    except (KeyError, TypeError, ValueError) as exc:
        msg = "training result is not a valid finite model artifact"
        raise ResearchWorkflowError(msg) from exc
    if not isinstance(metrics, dict) or set(metrics) != {"model", "zero_return", "training_mean"}:
        msg = "training metrics are missing required model and baseline results"
        raise ResearchWorkflowError(msg)
    dataset = session.scalar(
        select(ResearchDataset).where(
            ResearchDataset.owner_id == owner_id, ResearchDataset.dataset_id == job.dataset_id
        )
    )
    if dataset is None:
        msg = "training dataset not found"
        raise ResearchWorkflowError(msg, status_code=409)
    model_identity = artifact["identity"]
    if (
        model_identity["dataset_hash"] != dataset.content_hash
        or any(
            model_identity[key] != dataset.metadata_json[key]
            for key in ("instrument_id", "source", "timeframe")
        )
        or model_identity["image_hash"].removeprefix("sha256:")
        != (attempt.image_hash or "").removeprefix("sha256:")
    ):
        msg = "model identity does not match its frozen dataset and training image"
        raise ResearchWorkflowError(msg)
    if (
        artifact["horizon"] != job.inputs_json["horizon"]
        or job.lineage_json["dataset"]["content_hash"] != dataset.content_hash
    ):
        msg = "model lineage does not match its frozen training inputs"
        raise ResearchWorkflowError(msg)
    row = ResearchModelVersion(
        owner_id=owner_id,
        job_id=job_id,
        attempt_id=attempt.attempt_id,
        dataset_id=dataset.dataset_id,
        request_id=request_id,
        request_hash=request_hash,
        name=name,
        training_cutoff=artifact["training_cutoff"],
        metrics_json=metrics,
        artifact_json=artifact,
        artifact_hash=artifact["artifact_hash"],
    )
    session.add(row)
    session.flush()
    audit(session, owner_id, "model.register", row.model_id)
    session.flush()
    return _view(row)
