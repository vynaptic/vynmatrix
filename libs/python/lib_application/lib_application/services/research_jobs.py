"""Research admission and lease-fenced execution; no submitted code executes here."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from lib_application.db.models._base import generate_uuid
from lib_application.db.models.research_data import ResearchDataset
from lib_application.db.models.research_jobs import (
    ResearchControl,
    ResearchJob,
    ResearchJobAttempt,
    ResearchModelVersion,
)
from lib_application.services.research_admission import (
    get_research_control,
    require_research_admission,
)
from lib_application.services.research_authoring import (
    MAX_PAGE_SIZE,
    ResearchWorkflowError,
    audit,
    canonical_json,
    content_hash,
    encode_cursor,
    get_revision,
    identity,
    page_cursor,
    serialize_time,
    transaction_lock,
)
from lib_application.services.research_holdout import training_holdout_evidence

RESULT_LIMIT = 16 * 1024 * 1024
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
RETRYABLE = frozenset({"failed", "cancelled", "interrupted"})
MAX_ATTEMPTS = 100
SUPERVISOR_TTL = 30
MAX_HORIZON = 30
MIN_LEASE_SECONDS = 5
MAX_LEASE_SECONDS = 300


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _control(session: Session) -> ResearchControl:
    return get_research_control(session)


def _status(control: ResearchControl) -> dict[str, Any]:
    available = (
        not control.maintenance
        and control.heartbeat_at is not None
        and _utc(control.heartbeat_at) > _now() - timedelta(seconds=SUPERVISOR_TTL)
    )
    return {
        "available": available,
        "as_of": serialize_time(control.heartbeat_at),
        "maintenance": control.maintenance,
        "generation": control.generation,
    }


def supervisor_status(session: Session) -> dict[str, Any]:
    return _status(get_research_control(session, lock=False))


def supervisor_heartbeat(
    session: Session, *, worker_id: str, generation: str, image_hash: str, bundle_hash: str
) -> dict[str, Any]:
    identity(worker_id, "worker_id")
    identity(image_hash, "image_hash", maximum=255)
    identity(bundle_hash, "bundle_hash", maximum=64)
    control = _control(session)
    if control.maintenance or generation != control.generation:
        msg = "research generation changed or maintenance is active"
        raise ResearchWorkflowError(msg, status_code=409)
    if control.worker_id and control.worker_id != worker_id and _status(control)["available"]:
        msg = "another research supervisor owns the current lease"
        raise ResearchWorkflowError(msg, status_code=409)
    if control.worker_id == worker_id and (
        control.image_hash != image_hash or control.bundle_hash != bundle_hash
    ):
        msg = "supervisor identity changed within its generation"
        raise ResearchWorkflowError(msg, status_code=409)
    control.worker_id, control.image_hash, control.bundle_hash = worker_id, image_hash, bundle_hash
    control.heartbeat_at = _now()
    session.flush()
    return _status(control)


def set_maintenance(session: Session, *, enabled: bool, expected_generation: str) -> dict[str, Any]:
    """Maintenance authority fences admission; drain must precede resuming a generation."""
    if session.get_bind().dialect.name == "postgresql":
        from lib_application.services.database_authority import (  # noqa: PLC0415
            DatabaseAuthorityError,
            require_maintenance_database_role,
        )

        try:
            require_maintenance_database_role(session)
        except DatabaseAuthorityError as exc:
            msg = "maintenance database authority is required"
            raise ResearchWorkflowError(msg, status_code=403) from exc
        allowed = session.scalar(
            text(
                "SELECT pg_has_role(current_user, c.relowner, 'USAGE') "
                "FROM pg_class AS c JOIN pg_namespace AS n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relname='research_control'"
            )
        )
        if allowed is not True:
            msg = "maintenance database authority is required"
            raise ResearchWorkflowError(msg, status_code=403)
    control = _control(session)
    if (
        type(enabled) is not bool
        or not isinstance(expected_generation, str)
        or expected_generation != control.generation
    ):
        msg = "maintenance generation conflict"
        raise ResearchWorkflowError(msg, status_code=409)
    if enabled:
        control.maintenance = True
    elif control.maintenance:
        active = session.scalar(
            select(ResearchJob.job_id).where(ResearchJob.status == "running").limit(1)
        )
        if active is not None:
            msg = "research attempts must be drained before resuming"
            raise ResearchWorkflowError(msg, status_code=409)
        control.maintenance = False
        control.generation = generate_uuid()
        control.worker_id = control.image_hash = control.bundle_hash = None
        control.heartbeat_at = None
    session.flush()
    return _status(control)


def _job(session: Session, owner_id: str, job_id: str) -> ResearchJob:
    row = session.scalar(
        select(ResearchJob)
        .where(ResearchJob.owner_id == owner_id, ResearchJob.job_id == job_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        msg = "job not found"
        raise ResearchWorkflowError(msg, status_code=404)
    return row


def _attempt(session: Session, job: ResearchJob) -> ResearchJobAttempt:
    row = session.scalar(
        select(ResearchJobAttempt)
        .where(
            ResearchJobAttempt.owner_id == job.owner_id,
            ResearchJobAttempt.job_id == job.job_id,
            ResearchJobAttempt.attempt == job.current_attempt,
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        msg = "job attempt is missing"
        raise ResearchWorkflowError(msg, status_code=409)
    return row


ATTEMPT_METADATA = (
    ResearchJobAttempt.attempt_id,
    ResearchJobAttempt.attempt,
    ResearchJobAttempt.status,
    ResearchJobAttempt.started_at,
    ResearchJobAttempt.finished_at,
    ResearchJobAttempt.heartbeat_at,
    ResearchJobAttempt.lease_expires_at,
    ResearchJobAttempt.image_hash,
    ResearchJobAttempt.bundle_hash,
    ResearchJobAttempt.generation,
)


def _attempt_view(row: RowMapping) -> dict[str, Any]:
    return {
        key: serialize_time(value) if key.endswith("_at") else value for key, value in row.items()
    }


def get_job(session: Session, owner_id: str, job_id: str) -> dict[str, Any]:
    job = _job(session, owner_id, job_id)
    attempts = list(
        session.execute(
            select(*ATTEMPT_METADATA)
            .where(ResearchJobAttempt.owner_id == owner_id, ResearchJobAttempt.job_id == job_id)
            .order_by(ResearchJobAttempt.attempt.desc())
            .limit(MAX_ATTEMPTS)
        ).mappings()
    )
    active = (
        session.execute(
            select(
                ResearchJobAttempt.started_at,
                ResearchJobAttempt.finished_at,
                ResearchJobAttempt.error_json,
                ResearchJobAttempt.result_json,
            ).where(
                ResearchJobAttempt.owner_id == owner_id,
                ResearchJobAttempt.job_id == job_id,
                ResearchJobAttempt.attempt == job.current_attempt,
            )
        )
        .mappings()
        .one_or_none()
    )
    return {
        "job_id": job.job_id,
        "kind": job.kind,
        "status": job.status,
        "inputs": json.loads(canonical_json(job.inputs_json)),
        "inputs_hash": job.inputs_hash,
        "lineage": job.lineage_json,
        "created_at": serialize_time(job.created_at),
        "started_at": serialize_time(active["started_at"]) if active else None,
        "finished_at": serialize_time(active["finished_at"]) if active else None,
        "error": active["error_json"] if active else None,
        "result": active["result_json"] if active else None,
        "result_available": job.status == "completed",
        "attempt_count": job.current_attempt,
        "attempts": [_attempt_view(row) for row in reversed(attempts)],
    }


def list_jobs(
    session: Session, owner_id: str, *, after: str | None = None, limit: int = 50
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        msg = "limit must be between 1 and 100"
        raise ResearchWorkflowError(msg)
    query = (
        select(
            ResearchJob.job_id,
            ResearchJob.kind,
            ResearchJob.status,
            ResearchJob.created_at,
            ResearchJob.current_attempt.label("attempt_count"),
            ResearchJobAttempt.started_at,
            ResearchJobAttempt.finished_at,
        )
        .join(
            ResearchJobAttempt,
            (ResearchJobAttempt.owner_id == ResearchJob.owner_id)
            & (ResearchJobAttempt.job_id == ResearchJob.job_id)
            & (ResearchJobAttempt.attempt == ResearchJob.current_attempt),
        )
        .where(ResearchJob.owner_id == owner_id)
    )
    if after:
        timestamp, key = page_cursor(after)
        query = query.where(
            or_(
                ResearchJob.created_at < timestamp,
                and_(ResearchJob.created_at == timestamp, ResearchJob.job_id < key),
            )
        )
    rows = list(
        session.execute(
            query.order_by(ResearchJob.created_at.desc(), ResearchJob.job_id.desc()).limit(
                limit + 1
            )
        ).mappings()
    )
    return {
        "jobs": [
            {
                **_attempt_view(row),
                "inputs": {},
                "result": None,
                "error": None,
                "attempts": [],
                "result_available": row["status"] == "completed",
            }
            for row in rows[:limit]
        ],
        "next_after": encode_cursor(rows[limit - 1]["created_at"], rows[limit - 1]["job_id"])
        if len(rows) > limit
        else None,
    }


def _dataset(session: Session, owner_id: str, dataset_id: str) -> ResearchDataset:
    row = session.scalar(
        select(ResearchDataset).where(
            ResearchDataset.owner_id == owner_id, ResearchDataset.dataset_id == dataset_id
        )
    )
    if row is None:
        msg = "dataset not found"
        raise ResearchWorkflowError(msg, status_code=404)
    return row


def _model(session: Session, owner_id: str, model_id: str) -> ResearchModelVersion:
    row = session.scalar(
        select(ResearchModelVersion).where(
            ResearchModelVersion.owner_id == owner_id, ResearchModelVersion.model_id == model_id
        )
    )
    if row is None:
        msg = "model not found"
        raise ResearchWorkflowError(msg, status_code=404)
    return row


def _inputs(session: Session, owner_id: str, kind: str, inputs: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "validate_strategy": ({"revision_id"}, set()),
        "train": ({"dataset_id"}, {"horizon"}),
        "predict": ({"dataset_id", "model_id"}, {"evaluation_start"}),
        "backtest": (
            {"dataset_id", "revision_id", "assumptions"},
            {"model_id", "evaluation_start"},
        ),
    }
    if kind not in fields or not isinstance(inputs, dict):
        msg = "unsupported research job kind or inputs"
        raise ResearchWorkflowError(msg)
    required, optional = fields[kind]
    if not required <= set(inputs) or set(inputs) - required - optional:
        msg = "inputs have missing or unknown fields"
        raise ResearchWorkflowError(msg)
    canonical_json(inputs)
    frozen: dict[str, Any] = json.loads(canonical_json(inputs))
    if "revision_id" in frozen:
        get_revision(session, owner_id, identity(frozen["revision_id"], "revision_id", maximum=36))
    dataset = (
        _dataset(session, owner_id, identity(frozen["dataset_id"], "dataset_id", maximum=36))
        if "dataset_id" in frozen
        else None
    )
    if kind == "train":
        horizon = frozen.setdefault("horizon", 1)
        if type(horizon) is not int or not 1 <= horizon <= MAX_HORIZON:
            msg = "horizon must be from 1 to 30 observed bars"
            raise ResearchWorkflowError(msg)
    if dataset is not None:
        eligible = dataset.manifest_json.get("eligibility", {})
        key = "backtest" if kind == "backtest" else "training"
        if eligible.get(key) is not True:
            raise ResearchWorkflowError(
                "dataset is ineligible for "
                + kind
                + ": "
                + "; ".join(map(str, eligible.get("refusals", [])))
            )
    _validate_evaluation(session, owner_id, frozen, dataset)
    if kind == "backtest":
        _validate_assumptions(frozen, dataset)
    return frozen


def _validate_evaluation(
    session: Session, owner_id: str, inputs: dict[str, Any], dataset: ResearchDataset | None
) -> None:
    if inputs.get("model_id") is not None:
        model = _model(session, owner_id, identity(inputs["model_id"], "model_id", maximum=36))
        assert dataset is not None
        model_identity = model.artifact_json["identity"]
        metadata = dataset.metadata_json
        if any(
            model_identity[key] != metadata[key] for key in ("instrument_id", "source", "timeframe")
        ):
            msg = "model and dataset identity do not match"
            raise ResearchWorkflowError(msg)
        if inputs.get("evaluation_start") is not None:
            from lib_strategy.research.features import utc_timestamp  # noqa: PLC0415

            try:
                if utc_timestamp(inputs["evaluation_start"]) < utc_timestamp(model.training_cutoff):
                    msg = "evaluation_start precedes model training cutoff and embargo"
                    raise ResearchWorkflowError(msg)
            except (TypeError, ValueError) as exc:
                raise ResearchWorkflowError(str(exc)) from exc
    elif "evaluation_start" in inputs:
        from lib_strategy.research.features import utc_timestamp  # noqa: PLC0415

        try:
            utc_timestamp(inputs["evaluation_start"])
        except (TypeError, ValueError) as exc:
            raise ResearchWorkflowError(str(exc)) from exc


def _validate_assumptions(inputs: dict[str, Any], dataset: ResearchDataset | None) -> None:
    assumptions = inputs["assumptions"]
    required_assumptions = {
        "initial_capital",
        "currency",
        "size_pct",
        "fee_bps",
        "slippage_bps",
        "annualization_factor",
    }
    if not isinstance(assumptions, dict) or set(assumptions) != required_assumptions:
        msg = "backtest assumptions require explicit capital/currency/sizing/costs/annualization"
        raise ResearchWorkflowError(msg)
    for key in required_assumptions - {"currency"}:
        value = assumptions[key]
        try:
            number = Decimal(value) if isinstance(value, str) else Decimal("NaN")
        except InvalidOperation:
            number = Decimal("NaN")
        if (
            not number.is_finite()
            or number < 0
            or (key in {"initial_capital", "size_pct", "annualization_factor"} and number == 0)
            or (key == "size_pct" and number > 1)
        ):
            raise ResearchWorkflowError("invalid decimal-string backtest assumption: " + key)
    assert dataset is not None
    if assumptions["currency"] != dataset.metadata_json["currency"]:
        msg = "backtest currency must match the recorded dataset"
        raise ResearchWorkflowError(msg)


def _lineage(session: Session, owner_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
    lineage: dict[str, Any] = {}
    if inputs.get("revision_id"):
        revision = get_revision(session, owner_id, inputs["revision_id"])
        lineage["revision"] = {
            "revision_id": revision.revision_id,
            "source_hash": revision.source_hash,
            "content_hash": revision.content_hash,
        }
    if inputs.get("dataset_id"):
        dataset = _dataset(session, owner_id, inputs["dataset_id"])
        lineage["dataset"] = {
            "dataset_id": dataset.dataset_id,
            "content_hash": dataset.content_hash,
            "original_hash": dataset.original_hash,
            "manifest_hash": content_hash(dataset.manifest_json),
        }
    if inputs.get("model_id"):
        model = _model(session, owner_id, inputs["model_id"])
        lineage["model"] = {
            "model_id": model.model_id,
            "artifact_hash": model.artifact_hash,
            "training_cutoff": model.training_cutoff,
        }
    return lineage


def enqueue_job(
    session: Session, owner_id: str, *, request_id: str, kind: str, inputs: dict[str, Any]
) -> dict[str, Any]:
    identity(request_id, "request_id")
    require_research_admission(session)
    transaction_lock(session, "owner:" + owner_id)
    requested_hash = content_hash({"kind": kind, "inputs": inputs})
    previous = session.scalar(
        select(ResearchJobAttempt).where(
            ResearchJobAttempt.owner_id == owner_id, ResearchJobAttempt.request_id == request_id
        )
    )
    if previous is not None:
        if previous.request_hash != requested_hash:
            msg = "request_id already used with different job inputs"
            raise ResearchWorkflowError(msg, status_code=409)
        return get_job(session, owner_id, previous.job_id)
    frozen = _inputs(session, owner_id, kind, inputs)
    job = ResearchJob(
        job_id=generate_uuid(),
        owner_id=owner_id,
        kind=kind,
        inputs_json=frozen,
        lineage_json=_lineage(session, owner_id, frozen),
        inputs_hash=content_hash(frozen),
        revision_id=frozen.get("revision_id"),
        dataset_id=frozen.get("dataset_id"),
        model_id=frozen.get("model_id"),
        status="queued",
        current_attempt=1,
    )
    session.add(job)
    session.flush()
    session.add(
        ResearchJobAttempt(
            owner_id=owner_id,
            job_id=job.job_id,
            attempt=1,
            request_id=request_id,
            request_hash=requested_hash,
            status="queued",
        )
    )
    audit(session, owner_id, "job.enqueue", job.job_id)
    session.flush()
    return get_job(session, owner_id, job.job_id)


def _end(
    job: ResearchJob,
    attempt: ResearchJobAttempt,
    status: str,
    *,
    error: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    job.status = attempt.status = status
    attempt.finished_at = _now()
    attempt.error_json, attempt.result_json = error, result
    attempt.claim_token = None


def cancel_job(session: Session, owner_id: str, job_id: str) -> dict[str, Any]:
    _control(session)
    job = _job(session, owner_id, job_id)
    if job.status not in TERMINAL:
        _end(
            job,
            _attempt(session, job),
            "cancelled",
            error={"code": "cancelled", "detail": "Owner cancelled this attempt"},
        )
        audit(session, owner_id, "job.cancel", job_id)
        session.flush()
    return get_job(session, owner_id, job_id)


def retry_job(session: Session, owner_id: str, job_id: str, *, request_id: str) -> dict[str, Any]:
    identity(request_id, "request_id")
    require_research_admission(session)
    transaction_lock(session, "owner:" + owner_id)
    request_hash = content_hash({"retry": job_id})
    previous = session.scalar(
        select(ResearchJobAttempt).where(
            ResearchJobAttempt.owner_id == owner_id, ResearchJobAttempt.request_id == request_id
        )
    )
    if previous is not None:
        if previous.job_id != job_id or previous.request_hash != request_hash:
            msg = "request_id already used with different retry inputs"
            raise ResearchWorkflowError(msg, status_code=409)
        return get_job(session, owner_id, job_id)
    job = _job(session, owner_id, job_id)
    if job.status not in RETRYABLE:
        msg = "only failed, cancelled or interrupted jobs can retry"
        raise ResearchWorkflowError(msg, status_code=409)
    if job.current_attempt >= MAX_ATTEMPTS:
        msg = "research job attempt limit reached; create a new job to retain further attempts"
        raise ResearchWorkflowError(msg, status_code=409)
    job.current_attempt += 1
    job.status = "queued"
    session.add(
        ResearchJobAttempt(
            owner_id=owner_id,
            job_id=job_id,
            attempt=job.current_attempt,
            request_id=request_id,
            request_hash=request_hash,
            status="queued",
        )
    )
    audit(session, owner_id, "job.retry", job_id)
    session.flush()
    return get_job(session, owner_id, job_id)


def _lease_seconds(value: int) -> int:
    if type(value) is not int or not MIN_LEASE_SECONDS <= value <= MAX_LEASE_SECONDS:
        msg = "lease_seconds must be from 5 to 300"
        raise ResearchWorkflowError(msg)
    return value


def claim_job(
    session: Session,
    *,
    worker_id: str,
    generation: str,
    image_hash: str,
    bundle_hash: str,
    lease_seconds: int = 30,
) -> dict[str, Any] | None:
    lease_seconds = _lease_seconds(lease_seconds)
    control = _control(session)
    if (
        control.maintenance
        or generation != control.generation
        or not _status(control)["available"]
        or (control.worker_id, control.image_hash, control.bundle_hash)
        != (worker_id, image_hash, bundle_hash)
    ):
        return None
    running = list(session.scalars(select(ResearchJob).where(ResearchJob.status == "running")))
    for running_job in running:
        attempt = _attempt(session, running_job)
        if (
            attempt.lease_expires_at is None
            or _utc(attempt.lease_expires_at) <= _now()
            or attempt.generation != control.generation
        ):
            _end(
                running_job,
                attempt,
                "interrupted",
                error={
                    "code": "lease_expired",
                    "detail": "Worker lease expired; explicit retry is required",
                },
            )
            session.flush()
        else:
            return None
    # An interrupted execution must first be reconciled by the host controller.
    if running:
        return None
    job = session.scalar(
        select(ResearchJob)
        .where(ResearchJob.status == "queued")
        .order_by(ResearchJob.created_at, ResearchJob.job_id)
        .limit(1)
    )
    if job is None:
        return None
    attempt = _attempt(session, job)
    job.status = attempt.status = "running"
    attempt.claim_token, attempt.worker_id, attempt.generation = (
        secrets.token_hex(32),
        worker_id,
        generation,
    )
    attempt.image_hash, attempt.bundle_hash = image_hash, bundle_hash
    attempt.started_at = attempt.heartbeat_at = _now()
    attempt.lease_expires_at = _now() + timedelta(seconds=lease_seconds)
    session.flush()
    return {
        **get_job(session, job.owner_id, job.job_id),
        "owner_id": job.owner_id,
        "attempt_id": attempt.attempt_id,
        "claim_token": attempt.claim_token,
        "generation": generation,
        "image_hash": image_hash,
        "bundle_hash": bundle_hash,
    }


def _fenced(
    session: Session, owner_id: str, job_id: str, *, attempt_id: str, token: str, generation: str
) -> tuple[ResearchJob, ResearchJobAttempt]:
    control = _control(session)
    job = _job(session, owner_id, job_id)
    attempt = _attempt(session, job)
    if (
        control.maintenance
        or control.generation != generation
        or (attempt.worker_id, attempt.image_hash, attempt.bundle_hash)
        != (control.worker_id, control.image_hash, control.bundle_hash)
        or job.status != "running"
        or attempt.status != "running"
        or attempt.attempt_id != attempt_id
        or attempt.generation != generation
        or not attempt.claim_token
        or not secrets.compare_digest(attempt.claim_token, token)
        or attempt.lease_expires_at is None
        or _utc(attempt.lease_expires_at) <= _now()
    ):
        msg = "research attempt was fenced; result refused"
        raise ResearchWorkflowError(msg, status_code=409)
    return job, attempt


def heartbeat_job(
    session: Session,
    owner_id: str,
    job_id: str,
    *,
    attempt_id: str,
    token: str,
    generation: str,
    lease_seconds: int = 30,
) -> dict[str, Any]:
    duration = _lease_seconds(lease_seconds)
    job, attempt = _fenced(
        session, owner_id, job_id, attempt_id=attempt_id, token=token, generation=generation
    )
    attempt.heartbeat_at, attempt.lease_expires_at = _now(), _now() + timedelta(seconds=duration)
    session.flush()
    return get_job(session, owner_id, job.job_id)


def finish_job(
    session: Session,
    owner_id: str,
    job_id: str,
    *,
    attempt_id: str,
    token: str,
    generation: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (
        (result is None) == (error is None)
        or (result is not None and not isinstance(result, dict))
        or (error is not None and not isinstance(error, dict))
    ):
        msg = "finish requires exactly one result or error object"
        raise ResearchWorkflowError(msg)
    payload = json.loads(
        canonical_json(
            result if result is not None else error,
            limit=RESULT_LIMIT if result is not None else 1024 * 1024,
        )
    )
    job, attempt = _fenced(
        session, owner_id, job_id, attempt_id=attempt_id, token=token, generation=generation
    )
    if result is not None and job.kind == "train":
        validation = payload.setdefault("validation", {})
        if not isinstance(validation, dict):
            msg = "training result validation must be an object"
            raise ResearchWorkflowError(msg)
        validation["holdout_reuse"] = training_holdout_evidence(
            session, job, attempt_id=attempt.attempt_id
        )
        payload = json.loads(canonical_json(payload, limit=RESULT_LIMIT))
    _end(
        job,
        attempt,
        "completed" if result is not None else "failed",
        result=payload if result is not None else None,
        error=payload if error is not None else None,
    )
    session.flush()
    return get_job(session, owner_id, job_id)


def reconcile_expired_attempts(session: Session) -> list[dict[str, Any]]:
    """Retain interrupted attempts and return exact identities for host cleanup.

    The supervisor must stop and verify these recorded sandboxes before claiming
    another job. This function changes database evidence only.
    """
    control = _control(session)
    stale: list[dict[str, Any]] = []
    running = list(session.scalars(select(ResearchJob).where(ResearchJob.status == "running")))
    for job in running:
        attempt = _attempt(session, job)
        if (
            attempt.lease_expires_at is None
            or _utc(attempt.lease_expires_at) <= _now()
            or attempt.generation != control.generation
            or (attempt.worker_id, attempt.image_hash, attempt.bundle_hash)
            != (control.worker_id, control.image_hash, control.bundle_hash)
        ):
            stale.append(
                {
                    "owner_id": job.owner_id,
                    "job_id": job.job_id,
                    "attempt_id": attempt.attempt_id,
                    "worker_id": attempt.worker_id,
                    "generation": attempt.generation,
                    "image_hash": attempt.image_hash,
                    "bundle_hash": attempt.bundle_hash,
                }
            )
            _end(
                job,
                attempt,
                "interrupted",
                error={
                    "code": "lease_expired",
                    "detail": "Worker identity or lease expired; explicit retry is required",
                },
            )
    session.flush()
    return stale


def interrupt_job(
    session: Session,
    owner_id: str,
    job_id: str,
    *,
    attempt_id: str,
    token: str,
    generation: str,
    detail: str,
) -> dict[str, Any]:
    """Record an exact stopped host attempt, including while maintenance drains."""
    identity(detail, "detail", maximum=1000)
    control = _control(session)
    job = _job(session, owner_id, job_id)
    attempt = _attempt(session, job)
    if (
        control.generation != generation
        or attempt.generation != generation
        or job.status != "running"
        or attempt.status != "running"
        or attempt.attempt_id != attempt_id
        or not attempt.claim_token
        or not secrets.compare_digest(attempt.claim_token, token)
        or (attempt.worker_id, attempt.image_hash, attempt.bundle_hash)
        != (control.worker_id, control.image_hash, control.bundle_hash)
    ):
        msg = "research attempt was fenced; interruption refused"
        raise ResearchWorkflowError(msg, status_code=409)
    _end(job, attempt, "interrupted", error={"code": "interrupted", "detail": detail})
    session.flush()
    return get_job(session, owner_id, job_id)


def retire_supervisor(session: Session, *, worker_id: str, generation: str) -> dict[str, Any]:
    """Retire only this exact supervisor after its sandbox has been stopped."""
    control = _control(session)
    if not control.maintenance and (control.worker_id, control.generation) == (
        worker_id,
        generation,
    ):
        control.worker_id = control.image_hash = control.bundle_hash = None
        control.heartbeat_at = None
        session.flush()
    return _status(control)
