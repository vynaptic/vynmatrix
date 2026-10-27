"""Holdout reuse is produced from retained attempts and actual evaluation times."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from lib_application.db.models.research_jobs import ResearchJob, ResearchJobAttempt
from libs.python.lib_application.tests import test_research_workflows

store = test_research_workflows.store
training_job = test_research_workflows.training_job


def attempt(
    session: Session,
    key: str,
    *,
    owner: str = "owner",
    status: str = "completed",
    dataset_hash: str = "a" * 64,
    horizon: int = 1,
    kind: str = "train",
) -> ResearchJob:
    job = ResearchJob(
        job_id=key,
        owner_id=owner,
        kind=kind,
        inputs_json={"horizon": horizon},
        lineage_json={"dataset": {"content_hash": dataset_hash}},
        inputs_hash="a" * 64,
        status=status,
    )
    session.add(job)
    session.flush()
    session.add(
        ResearchJobAttempt(
            attempt_id=key,
            owner_id=owner,
            job_id=key,
            attempt=1,
            request_id=key,
            request_hash="a" * 64,
            status=status,
            finished_at=datetime(2020, 1, 1, tzinfo=UTC) if status == "completed" else None,
            result_json={"large": "retained payload must not be selected"},
        )
    )
    session.flush()
    return job


def test_training_counts_only_retained_completed_matching_owner_data_and_horizon(
    store: Session,
) -> None:
    from lib_application.services.research_holdout import training_holdout_evidence

    attempt(store, "prior")
    attempt(store, "other-owner", owner="other")
    attempt(store, "other-data", dataset_hash="b" * 64)
    attempt(store, "other-horizon", horizon=2)
    attempt(store, "other-kind", kind="predict")
    attempt(store, "failed", status="failed")
    current = attempt(store, "current", status="running")
    statements: list[str] = []

    def record(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(store.get_bind(), "before_cursor_execute", record)
    try:
        evidence = training_holdout_evidence(store, current, attempt_id="current")
    finally:
        event.remove(store.get_bind(), "before_cursor_execute", record)
    assert evidence["holdout_reused"] is True
    assert evidence["prior_completed_attempts"] == 1
    assert evidence["basis"] == "same_frozen_dataset_and_horizon"
    assert all("result_json" not in statement for statement in statements)


def test_first_training_records_zero_prior_exposures(store: Session) -> None:
    from lib_application.services.research_holdout import training_holdout_evidence

    current = attempt(store, "current", status="running")
    assert (
        training_holdout_evidence(store, current, attempt_id="current")["holdout_reused"] is False
    )


def test_finish_training_retains_real_reuse_evidence_without_changing_model_hash(
    store: Session,
) -> None:
    from lib_application.services.research_jobs import get_job

    first, first_result = training_job(store, "first")
    second, second_result = training_job(store, "second")
    old = get_job(store, "owner", first["job_id"])["result"]
    new = get_job(store, "owner", second["job_id"])["result"]
    assert old["validation"]["holdout_reuse"]["prior_completed_attempts"] == 0
    assert new["validation"]["holdout_reuse"]["prior_completed_attempts"] == 1
    assert old["model"]["artifact_hash"] == first_result["model"]["artifact_hash"]
    assert new["model"]["artifact_hash"] == second_result["model"]["artifact_hash"]


def test_finish_training_refuses_malformed_validation_without_completing(store: Session) -> None:
    from lib_application.services import research_jobs as jobs

    previous, _ = training_job(store, "previous")
    current = jobs.enqueue_job(
        store,
        "owner",
        request_id="malformed",
        kind="train",
        inputs=previous["inputs"],
    )
    claimed = jobs.claim_job(
        store,
        worker_id="fit-worker",
        generation=store.info["generation"],
        image_hash="b" * 64,
        bundle_hash="c" * 64,
    )
    assert claimed is not None
    with pytest.raises(jobs.ResearchWorkflowError, match="validation"):
        jobs.finish_job(
            store,
            "owner",
            current["job_id"],
            attempt_id=claimed["attempt_id"],
            token=claimed["claim_token"],
            generation=store.info["generation"],
            result={"validation": []},
        )
    assert jobs.get_job(store, "owner", current["job_id"])["status"] == "running"


def model() -> dict[str, Any]:
    return {
        "identity": {"instrument_id": 1, "source": "fixture", "timeframe": "1m"},
        "artifact_hash": "a" * 64,
        "split": {
            "test": {
                "first_ts": "2020-01-01T12:00:00+00:00",
                "last_ts": "2020-01-01T13:00:00+00:00",
                "last_label_ts": "2020-01-01T13:01:00+00:00",
            }
        },
    }


@pytest.mark.parametrize(
    ("observations", "count"),
    [
        (["2020-01-01T11:59:00+00:00"], 0),
        (["2020-01-01T12:00:00+00:00", "2020-01-01T13:00:00+00:00"], 2),
        (["2020-01-02T12:00:00+00:00"], 0),
        ([], 0),
    ],
)
def test_evaluation_marks_actual_original_holdout_observation_overlap(
    observations: list[str], count: int
) -> None:
    from lib_application.services.research_holdout import evaluation_holdout_evidence

    artifact = model()
    evidence = evaluation_holdout_evidence(artifact, artifact["identity"], observations)
    assert evidence["overlap_observations"] == count
    assert evidence["holdout_reused"] is (count > 0)
    assert evidence["model_artifact_hash"] == artifact["artifact_hash"]
    assert evidence["holdout_start"] == artifact["split"]["test"]["first_ts"]
    assert evidence["evaluation_start"] == (min(observations) if observations else None)


@pytest.mark.parametrize(
    ("observation", "reused"),
    [
        ("2020-01-01T13:01:00+00:00", True),
        ("2020-01-01T13:03:00+00:00", True),
        ("2020-01-01T13:04:00+00:00", False),
    ],
)
def test_horizon_three_holdout_includes_prices_consumed_by_final_labels(
    observation: str, reused: bool
) -> None:
    from lib_application.services.research_holdout import evaluation_holdout_evidence

    artifact = model()
    artifact["horizon"] = 3
    artifact["split"]["test"]["last_label_ts"] = "2020-01-01T13:03:00+00:00"
    evidence = evaluation_holdout_evidence(artifact, artifact["identity"], [observation])
    assert evidence["holdout_reused"] is reused
    assert evidence["holdout_end"] == "2020-01-01T13:03:00+00:00"
    assert evidence["holdout_forecast_end"] == "2020-01-01T13:00:00+00:00"


@pytest.mark.parametrize("key", ["instrument_id", "source", "timeframe"])
def test_evaluation_does_not_guess_cross_instrument_or_feed_identity(key: str) -> None:
    from lib_application.services.research_holdout import evaluation_holdout_evidence

    artifact = model()
    metadata = {**artifact["identity"], key: "different"}
    with pytest.raises(ValueError, match="identity"):
        evaluation_holdout_evidence(artifact, metadata, ["2020-01-01T12:00:00+00:00"])


def test_evaluation_requires_utc_times() -> None:
    from lib_application.services.research_holdout import evaluation_holdout_evidence

    artifact = model()
    with pytest.raises(ValueError, match="UTC"):
        evaluation_holdout_evidence(artifact, artifact["identity"], ["2020-01-01T12:00:00"])
