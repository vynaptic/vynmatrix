"""Small retained evidence of repeated training and original-holdout exposure."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lib_application.db.models.research_jobs import ResearchJob, ResearchJobAttempt


def training_holdout_evidence(
    session: Session, job: ResearchJob, *, attempt_id: str
) -> dict[str, Any]:
    """Count prior completed exposure without reading retained result artifacts.

    The fixed training recipe uses the same chronological split for the same
    frozen dataset and horizon. Count across images and bundle versions so
    changing implementation does not hide a previously inspected holdout.
    """
    dataset_hash = job.lineage_json["dataset"]["content_hash"]
    horizon = job.inputs_json["horizon"]
    count = session.scalar(
        select(func.count())
        .select_from(ResearchJobAttempt)
        .join(ResearchJob, ResearchJob.job_id == ResearchJobAttempt.job_id)
        .where(
            ResearchJob.owner_id == job.owner_id,
            ResearchJobAttempt.owner_id == job.owner_id,
            ResearchJob.kind == "train",
            ResearchJobAttempt.status == "completed",
            ResearchJobAttempt.attempt_id != attempt_id,
            ResearchJob.lineage_json["dataset"]["content_hash"].as_string() == dataset_hash,
            ResearchJob.inputs_json["horizon"].as_integer() == horizon,
        )
    )
    return {
        "holdout_reused": bool(count),
        "prior_completed_attempts": int(count or 0),
        "basis": "same_frozen_dataset_and_horizon",
        "dataset_hash": dataset_hash,
        "horizon": horizon,
    }


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        msg = "holdout evidence requires UTC ISO timestamps"
        raise ValueError(msg) from exc
    if parsed.utcoffset() != timedelta(0):
        msg = "holdout evidence requires UTC ISO timestamps"
        raise ValueError(msg)
    return parsed.astimezone(UTC)


def evaluation_holdout_evidence(
    model: Mapping[str, Any],
    metadata: Mapping[str, Any],
    observations: Sequence[str],
    *,
    evaluation_start: str | None = None,
) -> dict[str, Any]:
    """Mark evaluation observations inside the original holdout's consumed data.

    The coordinator already validates the complete model and compatible dataset.
    Recheck feed identity here because timestamp overlap alone is insufficient.
    A changed upload/hash does not make overlapping observations independent.
    Final holdout labels consume prices through last_label_ts, after the final
    forecast timestamp; those observations remain part of the inspected data.
    """
    if any(
        model["identity"][key] != metadata.get(key)
        for key in ("instrument_id", "source", "timeframe")
    ):
        msg = "holdout evidence requires matching instrument/source/timeframe identity"
        raise ValueError(msg)
    window = model["split"]["test"]
    first, last = _utc(window["first_ts"]), _utc(window["last_label_ts"])
    actual = [_utc(value) for value in observations]
    if evaluation_start is not None:
        start = _utc(evaluation_start)
        actual = [timestamp for timestamp in actual if timestamp >= start]
    count = sum(first <= timestamp <= last for timestamp in actual)
    return {
        "holdout_reused": count > 0,
        "basis": "original_model_holdout_timestamp_overlap",
        "overlap_observations": count,
        "holdout_start": first.isoformat(),
        "holdout_end": last.isoformat(),
        "holdout_forecast_end": _utc(window["last_ts"]).isoformat(),
        "evaluation_start": min(actual).isoformat() if actual else None,
        "evaluation_end": max(actual).isoformat() if actual else None,
        "model_artifact_hash": model["artifact_hash"],
    }
