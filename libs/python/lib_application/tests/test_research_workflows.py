"""Research drafts and durable attempts preserve frozen owner-scoped evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lib_application.db.models import Base, User

SOURCE = "class UserStrategy:\n    pass\n"
CONFIG = {"class_name": "UserStrategy", "strategy_id": "research_only", "parameters": {}}


@pytest.fixture
def store() -> Any:
    import lib_application.db.models.research_authoring
    import lib_application.db.models.research_data
    import lib_application.db.models.research_jobs  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                User(
                    user_id="owner",
                    email="owner@local.test",
                    base_ccy="USD",
                    is_deployment_owner=True,
                ),
                User(user_id="other", email="other@local.test", base_ccy="USD"),
            ]
        )
        session.commit()
        from lib_application.services.research_jobs import supervisor_status

        session.info["generation"] = supervisor_status(session)["generation"]
        yield session
    engine.dispose()


def draft(store: Session) -> dict[str, Any]:
    from lib_application.services import research_authoring as service

    return service.create_draft(
        store, "owner", request_id="draft-1", name="Draft", source=SOURCE, config=CONFIG
    )


def queued(store: Session) -> dict[str, Any]:
    from lib_application.services import research_jobs as service

    revision = draft(store)
    return service.enqueue_job(
        store,
        "owner",
        request_id="job-1",
        kind="validate_strategy",
        inputs={"revision_id": revision["revision_id"]},
    )


def claim(store: Session, **kwargs: Any) -> dict[str, Any]:
    from lib_application.services import research_jobs as service

    service.supervisor_heartbeat(
        store,
        worker_id="worker",
        generation=store.info["generation"],
        image_hash="sha256:image",
        bundle_hash="bundle",
    )
    result = service.claim_job(
        store,
        worker_id="worker",
        generation=store.info["generation"],
        image_hash="sha256:image",
        bundle_hash="bundle",
        **kwargs,
    )
    assert result is not None
    return result


def test_revision_hash_immutable_export_and_expected_update(store: Session) -> None:
    from lib_application.db.models.research_authoring import ResearchDraftRevision
    from lib_application.services import research_authoring as service

    first = draft(store)
    assert (
        service.create_draft(
            store, "owner", request_id="draft-1", name="Draft", source=SOURCE, config=CONFIG
        )
        == first
    )
    second = service.update_draft(
        store,
        "owner",
        first["draft_id"],
        expected_revision=1,
        name="Renamed",
        source=SOURCE + "# edit\n",
        config=CONFIG,
    )
    assert second["revision"] == 2
    assert second["source_hash"] != first["source_hash"]
    assert (
        service.export_draft(store, "owner", first["draft_id"], revision_id=first["revision_id"])[
            "source"
        ]
        == SOURCE
    )
    with pytest.raises(service.ResearchWorkflowError, match="revision"):
        service.update_draft(
            store,
            "owner",
            first["draft_id"],
            expected_revision=1,
            name="Lost",
            source=SOURCE,
            config=CONFIG,
        )
    row = store.get(ResearchDraftRevision, first["revision_id"])
    assert row is not None
    row.source = "mutated"
    with pytest.raises(ValueError, match="immutable"):
        store.flush()


def test_authoring_scope_bounded_source_and_idempotency_conflict(store: Session) -> None:
    from lib_application.services import research_authoring as service

    first = draft(store)
    with pytest.raises(service.ResearchWorkflowError, match="not found"):
        service.get_draft(store, "other", first["draft_id"])
    with pytest.raises(service.ResearchWorkflowError, match="request_id"):
        service.create_draft(
            store, "owner", request_id="draft-1", name="Changed", source=SOURCE, config=CONFIG
        )
    with pytest.raises(service.ResearchWorkflowError, match="source"):
        service.create_draft(
            store,
            "owner",
            request_id="draft-2",
            name="Large",
            source="x" * (256 * 1024 + 1),
            config=CONFIG,
        )
    assert service.list_drafts(store, "other")["drafts"] == []


def test_job_enqueue_retains_exact_revision_and_rejects_cross_owner(store: Session) -> None:
    from lib_application.services import research_jobs as service

    job = queued(store)
    assert job["status"] == "queued"
    assert len(job["attempts"]) == 1
    assert (
        service.enqueue_job(
            store, "owner", request_id="job-1", kind=job["kind"], inputs=job["inputs"]
        )["job_id"]
        == job["job_id"]
    )
    with pytest.raises(service.ResearchWorkflowError, match="request_id"):
        service.enqueue_job(
            store,
            "owner",
            request_id="job-1",
            kind="validate_strategy",
            inputs={"revision_id": "another"},
        )
    with pytest.raises(service.ResearchWorkflowError, match="not found"):
        service.enqueue_job(
            store, "other", request_id="job-1", kind=job["kind"], inputs=job["inputs"]
        )
    with pytest.raises(service.ResearchWorkflowError, match="not found"):
        service.get_job(store, "other", job["job_id"])


def test_worker_claim_and_finish_are_fenced_by_attempt_token(store: Session) -> None:
    from lib_application.services import research_jobs as service

    job = queued(store)
    claimed = claim(store)
    assert claimed["job_id"] == job["job_id"]
    assert claimed["status"] == "running"
    token = claimed["claim_token"]
    attempt_id = claimed["attempt_id"]
    with pytest.raises(service.ResearchWorkflowError, match="fenc"):
        service.finish_job(
            store,
            "owner",
            job["job_id"],
            attempt_id=attempt_id,
            token="wrong",
            generation=store.info["generation"],
            result={"valid": True},
        )
    result = service.finish_job(
        store,
        "owner",
        job["job_id"],
        attempt_id=attempt_id,
        token=token,
        generation=store.info["generation"],
        result={"valid": True},
    )
    assert result["status"] == "completed"
    assert result["result"] == {"valid": True}
    with pytest.raises(service.ResearchWorkflowError, match="retry"):
        service.retry_job(store, "owner", job["job_id"], request_id="retry-complete")


def test_cancel_fences_late_results_and_retry_retains_attempt(store: Session) -> None:
    from lib_application.services import research_jobs as service

    job = queued(store)
    claimed = claim(store)
    cancelled = service.cancel_job(store, "owner", job["job_id"])
    assert cancelled["status"] == "cancelled"
    with pytest.raises(service.ResearchWorkflowError, match="fenc"):
        service.finish_job(
            store,
            "owner",
            job["job_id"],
            attempt_id=claimed["attempt_id"],
            token=claimed["claim_token"],
            generation=store.info["generation"],
            result={"valid": True},
        )
    retried = service.retry_job(store, "owner", job["job_id"], request_id="retry-1")
    assert retried["status"] == "queued"
    assert len(retried["attempts"]) == 2
    assert retried["attempts"][0]["status"] == "cancelled"
    assert service.retry_job(store, "owner", job["job_id"], request_id="retry-1") == retried


def test_expired_attempt_is_interrupted_and_not_automatically_rerun(store: Session) -> None:
    from lib_application.db.models.research_jobs import ResearchJobAttempt
    from lib_application.services import research_jobs as service

    job = queued(store)
    claimed = claim(store)
    attempt = store.get(ResearchJobAttempt, claimed["attempt_id"])
    assert attempt is not None
    attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    store.flush()
    assert (
        service.claim_job(
            store,
            worker_id="worker",
            generation=store.info["generation"],
            image_hash="sha256:image",
            bundle_hash="bundle",
        )
        is None
    )
    assert service.get_job(store, "owner", job["job_id"])["status"] == "interrupted"
    with pytest.raises(service.ResearchWorkflowError, match="fenc"):
        service.heartbeat_job(
            store,
            "owner",
            job["job_id"],
            attempt_id=claimed["attempt_id"],
            token=claimed["claim_token"],
            generation=store.info["generation"],
        )


def test_maintenance_blocks_admission_and_rotates_generation(store: Session) -> None:
    from lib_application.services import research_jobs as service

    first = draft(store)
    queued(store)
    result = service.set_maintenance(
        store, enabled=True, expected_generation=store.info["generation"]
    )
    assert result["maintenance"] is True
    with pytest.raises(service.ResearchWorkflowError, match="maintenance"):
        service.enqueue_job(
            store,
            "owner",
            request_id="job-new",
            kind="validate_strategy",
            inputs={"revision_id": first["revision_id"]},
        )
    assert (
        service.claim_job(
            store,
            worker_id="worker",
            generation=store.info["generation"],
            image_hash="sha256:image",
            bundle_hash="bundle",
        )
        is None
    )
    resumed = service.set_maintenance(
        store, enabled=False, expected_generation=store.info["generation"]
    )
    assert resumed["generation"] != store.info["generation"]
    assert resumed["available"] is False
    with pytest.raises(service.ResearchWorkflowError, match="generation"):
        service.supervisor_heartbeat(
            store,
            worker_id="old",
            generation=store.info["generation"],
            image_hash="sha256:image",
            bundle_hash="bundle",
        )


def test_job_input_and_finished_attempt_are_immutable(store: Session) -> None:
    from lib_application.db.models.research_jobs import ResearchJob

    job = queued(store)
    row = store.scalar(select(ResearchJob).where(ResearchJob.job_id == job["job_id"]))
    assert row is not None
    row.inputs_json = {"revision_id": "replace"}
    with pytest.raises(ValueError, match="immutable"):
        store.flush()


def test_job_lineage_pins_source_content_and_hides_private_token(store: Session) -> None:
    from lib_application.services import research_authoring as authoring
    from lib_application.services import research_jobs as jobs

    job = queued(store)
    original = dict(job["lineage"])
    source = authoring.get_revision(store, "owner", job["inputs"]["revision_id"])
    authoring.update_draft(
        store,
        "owner",
        source.draft_id,
        expected_revision=1,
        name="Changed",
        source=SOURCE + "# next\n",
        config=CONFIG,
    )
    claimed = claim(store)
    public = jobs.get_job(store, "owner", claimed["job_id"])
    assert public["lineage"] == original
    assert "claim_token" not in str(public)


def training_job(
    store: Session, request_id: str = "train"
) -> tuple[dict[str, Any], dict[str, Any]]:
    import math

    from lib_application.db.models import Instrument
    from lib_application.services import research_data
    from lib_application.services import research_jobs as jobs
    from lib_strategy.research import train_return_model

    if store.get(Instrument, 1) is None:
        store.add(
            Instrument(
                instr_id=1, canonical="BTCUSD", asset_class="crypto", settlement_currency="USD"
            )
        )
    store.flush()
    bars = []
    for index in range(1300):
        price = 100 + math.sin(index / 7)
        bars.append(
            {
                "ts": (datetime(2020, 1, 1, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
                "open": str(price),
                "high": str(price + 1),
                "low": str(price - 1),
                "close": str(price),
                "volume": "100",
            }
        )
    csv = "ts,open,high,low,close,volume\n" + "".join(
        ",".join(row[key] for key in ("ts", "open", "high", "low", "close", "volume")) + "\n"
        for row in bars
    )
    metadata = {
        "instrument_id": 1,
        "source": "fixture",
        "provenance": "deterministic unit fixture",
        "currency": "USD",
        "timeframe": "1m",
        "timestamp_semantics": "bar_open",
        "adjustment_policy": "raw",
    }
    receipt = research_data.import_dataset(store, "owner", metadata, csv.encode())
    dataset = research_data.freeze_dataset(
        store, "owner", receipt["import_id"], receipt["content_hash"], "Training"
    )
    job = jobs.enqueue_job(
        store,
        "owner",
        request_id=request_id,
        kind="train",
        inputs={"dataset_id": dataset["dataset_id"], "horizon": 1},
    )
    jobs.supervisor_heartbeat(
        store,
        worker_id="fit-worker",
        generation=store.info["generation"],
        image_hash="b" * 64,
        bundle_hash="c" * 64,
    )
    claimed = jobs.claim_job(
        store,
        worker_id="fit-worker",
        generation=store.info["generation"],
        image_hash="b" * 64,
        bundle_hash="c" * 64,
    )
    assert claimed is not None
    result = train_return_model(
        bars,
        identity={
            "instrument_id": 1,
            "source": "fixture",
            "timeframe": "1m",
            "dataset_hash": dataset["content_hash"],
            "image_hash": "b" * 64,
        },
    )
    jobs.finish_job(
        store,
        "owner",
        job["job_id"],
        attempt_id=claimed["attempt_id"],
        token=claimed["claim_token"],
        generation=store.info["generation"],
        result=result,
    )
    return job, result


def test_model_registration_strict_lineage_idempotency_and_immutability(store: Session) -> None:
    from lib_application.db.models.research_jobs import ResearchModelVersion
    from lib_application.services import research_registry as registry

    job, result = training_job(store)
    model = registry.register_model(
        store, "owner", request_id="register", job_id=job["job_id"], name="Return model"
    )
    assert model["artifact"] == result["model"]
    assert model["training_cutoff"] == result["model"]["training_cutoff"]
    assert (
        registry.register_model(
            store, "owner", request_id="register", job_id=job["job_id"], name="Return model"
        )
        == model
    )
    with pytest.raises(registry.ResearchWorkflowError, match="not found"):
        registry.get_model(store, "other", model["model_id"])
    with pytest.raises(registry.ResearchWorkflowError, match="request_id"):
        registry.register_model(
            store, "owner", request_id="register", job_id=job["job_id"], name="Different"
        )
    row = store.get(ResearchModelVersion, model["model_id"])
    assert row is not None
    row.name = "mutate"
    with pytest.raises(ValueError, match="immutable"):
        store.flush()


def test_model_registration_rejects_nontraining_job(store: Session) -> None:
    from lib_application.services import research_registry as registry

    job = queued(store)
    with pytest.raises(registry.ResearchWorkflowError, match="successful train"):
        registry.register_model(
            store, "owner", request_id="bad-model", job_id=job["job_id"], name="Bad"
        )


def test_new_supervisor_fences_previous_supervisor_results(store: Session) -> None:
    from lib_application.db.models.research_jobs import ResearchControl
    from lib_application.services import research_jobs as service

    job = queued(store)
    claimed = claim(store)
    control = store.get(ResearchControl, 1)
    assert control is not None
    control.heartbeat_at = datetime.now(UTC) - timedelta(seconds=31)
    store.flush()
    service.supervisor_heartbeat(
        store,
        worker_id="replacement",
        generation=store.info["generation"],
        image_hash="sha256:image",
        bundle_hash="bundle",
    )
    with pytest.raises(service.ResearchWorkflowError, match="fenc"):
        service.finish_job(
            store,
            "owner",
            job["job_id"],
            attempt_id=claimed["attempt_id"],
            token=claimed["claim_token"],
            generation=store.info["generation"],
            result={"valid": True},
        )


def test_workspace_lists_use_metadata_without_source_or_result_payloads(store: Session) -> None:
    from sqlalchemy import event

    from lib_application.services import research_authoring as authoring
    from lib_application.services import research_jobs as jobs

    job = queued(store)
    claimed = claim(store)
    jobs.finish_job(
        store,
        "owner",
        job["job_id"],
        attempt_id=claimed["attempt_id"],
        token=claimed["claim_token"],
        generation=store.info["generation"],
        result={"valid": True, "diagnostics": "large-result" * 1000},
    )
    statements: list[str] = []

    def capture(
        _connection: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: bool
    ) -> None:
        statements.append(statement)

    event.listen(store.get_bind(), "before_cursor_execute", capture)
    try:
        listed_jobs = jobs.list_jobs(store, "owner")["jobs"]
        listed_drafts = authoring.list_drafts(store, "owner")["drafts"]
    finally:
        event.remove(store.get_bind(), "before_cursor_execute", capture)
    assert listed_jobs[0]["result"] is None
    assert listed_jobs[0]["result_available"] is True
    assert "source" not in listed_drafts[0]
    assert "config" not in listed_drafts[0]
    assert len(statements) == 2
    assert not any(
        "research_draft_revisions.source," in query or "research_job_attempts.result_json," in query
        for query in statements
    )
    selected = jobs.get_job(store, "owner", job["job_id"])
    assert selected["result"]["diagnostics"].startswith("large-result")
    assert "result" not in selected["attempts"][0]


def test_retry_limit_preserves_existing_attempts(
    store: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lib_application.services import research_jobs as jobs

    job = queued(store)
    jobs.cancel_job(store, "owner", job["job_id"])
    monkeypatch.setattr(jobs, "MAX_ATTEMPTS", 1)
    with pytest.raises(jobs.ResearchWorkflowError, match="attempt limit"):
        jobs.retry_job(store, "owner", job["job_id"], request_id="excess-retry")
    assert len(jobs.get_job(store, "owner", job["job_id"])["attempts"]) == 1


def test_reconciliation_retains_exact_interrupted_identity(store: Session) -> None:
    from lib_application.db.models.research_jobs import ResearchJobAttempt
    from lib_application.services import research_jobs as jobs

    job = queued(store)
    claimed = claim(store)
    attempt = store.get(ResearchJobAttempt, claimed["attempt_id"])
    assert attempt is not None
    attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    store.flush()
    stale = jobs.reconcile_expired_attempts(store)
    assert stale[0]["attempt_id"] == claimed["attempt_id"]
    assert "claim_token" not in stale[0]
    assert jobs.get_job(store, "owner", job["job_id"])["status"] == "interrupted"
    assert jobs.reconcile_expired_attempts(store) == []


def test_controlled_interrupt_is_fenced_and_allowed_during_maintenance(store: Session) -> None:
    from lib_application.services import research_jobs as jobs

    job = queued(store)
    claimed = claim(store)
    jobs.set_maintenance(store, enabled=True, expected_generation=claimed["generation"])
    with pytest.raises(jobs.ResearchWorkflowError, match="fenc"):
        jobs.interrupt_job(
            store,
            "owner",
            job["job_id"],
            attempt_id=claimed["attempt_id"],
            token="wrong",
            generation=claimed["generation"],
            detail="Host stopped",
        )
    result = jobs.interrupt_job(
        store,
        "owner",
        job["job_id"],
        attempt_id=claimed["attempt_id"],
        token=claimed["claim_token"],
        generation=claimed["generation"],
        detail="Host stopped",
    )
    assert result["status"] == "interrupted"
    assert result["error"]["detail"] == "Host stopped"


def test_retiring_supervisor_requires_exact_identity(store: Session) -> None:
    from lib_application.services import research_jobs as jobs

    queued(store)
    claim(store)
    assert (
        jobs.retire_supervisor(store, worker_id="other", generation=store.info["generation"])[
            "available"
        ]
        is True
    )
    assert (
        jobs.retire_supervisor(store, worker_id="worker", generation=store.info["generation"])[
            "available"
        ]
        is False
    )


def test_newest_draft_and_job_pages_use_stable_opaque_cursor(
    store: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lib_application.services import research_authoring as authoring
    from lib_application.services import research_jobs as jobs

    identifiers = iter(
        [
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000011",
            "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "ffffffff-ffff-ffff-ffff-fffffffffffe",
        ]
    )
    monkeypatch.setattr(authoring, "generate_uuid", lambda: next(identifiers))
    old = authoring.create_draft(
        store, "owner", request_id="old", name="Old", source=SOURCE, config=CONFIG
    )
    new = authoring.create_draft(
        store, "owner", request_id="new", name="New", source=SOURCE, config=CONFIG
    )
    first = authoring.list_drafts(store, "owner", limit=1)
    assert first["drafts"][0]["draft_id"] == new["draft_id"]
    assert first["next_after"] not in {old["draft_id"], new["draft_id"]}
    second = authoring.list_drafts(store, "owner", after=first["next_after"], limit=1)
    assert second["drafts"][0]["draft_id"] == old["draft_id"]
    assert second["next_after"] is None
    first_job = jobs.enqueue_job(
        store,
        "owner",
        request_id="old-job",
        kind="validate_strategy",
        inputs={"revision_id": old["revision_id"]},
    )
    second_job = jobs.enqueue_job(
        store,
        "owner",
        request_id="new-job",
        kind="validate_strategy",
        inputs={"revision_id": new["revision_id"]},
    )
    job_page = jobs.list_jobs(store, "owner", limit=1)
    assert job_page["jobs"][0]["job_id"] == second_job["job_id"]
    assert (
        jobs.list_jobs(store, "owner", limit=1, after=job_page["next_after"])["jobs"][0]["job_id"]
        == first_job["job_id"]
    )
    with pytest.raises(authoring.ResearchWorkflowError, match="cursor"):
        authoring.list_drafts(store, "owner", after="not-a-valid-cursor")


def test_model_pages_show_new_registration_first(store: Session) -> None:
    from lib_application.services import research_registry as registry

    old_job, _ = training_job(store, request_id="old-train")
    old_model = registry.register_model(
        store, "owner", request_id="old-model", job_id=old_job["job_id"], name="Old model"
    )
    new_job, _ = training_job(store, request_id="new-train")
    new_model = registry.register_model(
        store, "owner", request_id="new-model", job_id=new_job["job_id"], name="New model"
    )
    first = registry.list_models(store, "owner", limit=1)
    assert first["models"][0]["model_id"] == new_model["model_id"]
    second = registry.list_models(store, "owner", limit=1, after=first["next_after"])
    assert second["models"][0]["model_id"] == old_model["model_id"]
    assert second["next_after"] is None


def test_maintenance_blocks_draft_and_model_mutations(store: Session) -> None:
    from lib_application.services import research_authoring as authoring
    from lib_application.services import research_jobs as jobs
    from lib_application.services import research_registry as registry

    first = draft(store)
    jobs.set_maintenance(store, enabled=True, expected_generation=store.info["generation"])
    with pytest.raises(authoring.ResearchWorkflowError, match="maintenance"):
        authoring.create_draft(
            store, "owner", request_id="fenced-new", name="New", source=SOURCE, config=CONFIG
        )
    with pytest.raises(authoring.ResearchWorkflowError, match="maintenance"):
        authoring.update_draft(
            store,
            "owner",
            first["draft_id"],
            expected_revision=1,
            name="Changed",
            source=SOURCE,
            config=CONFIG,
        )
    with pytest.raises(registry.ResearchWorkflowError, match="maintenance"):
        registry.register_model(
            store, "owner", request_id="fenced-model", job_id="missing", name="Model"
        )


def test_authoring_reserves_research_model_parameter_for_selected_registry_artifact(
    store: Session,
) -> None:
    from lib_application.services import research_authoring as authoring

    config = {**CONFIG, "parameters": {"research_model": {"injected": True}}}
    with pytest.raises(authoring.ResearchWorkflowError, match="research_model"):
        authoring.create_draft(
            store, "owner", request_id="reserved", name="Reserved", source=SOURCE, config=config
        )
    current = draft(store)
    with pytest.raises(authoring.ResearchWorkflowError, match="research_model"):
        authoring.update_draft(
            store,
            "owner",
            current["draft_id"],
            expected_revision=1,
            name="Reserved",
            source=SOURCE,
            config=config,
        )
