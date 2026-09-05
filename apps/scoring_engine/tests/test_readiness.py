"""/ready must reflect DB connectivity for the scoring ingest entrypoint (G14)
and inline outbox-relay liveness (OB-1)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from lib_common.config_validation import (
    DatabaseConfig,
    RunMode,
    ScoringEngineConfig,
    ScoringRuntimeConfig,
    load_scoring_engine_config,
)
from lib_common.hashing import canonical_json_hash
from lib_common.paper_promotion import (
    PaperPromotionScope,
)
from scoring_engine import main as scoring_main
from scoring_engine.api import _store_db_ready
from scoring_engine.storage import InMemoryScoreStore


def test_in_memory_store_is_not_production_ready() -> None:
    assert _store_db_ready(InMemoryScoreStore()) is False


def test_scoring_runtime_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL environment variable required"):
        load_scoring_engine_config()


def test_production_paper_policy_fails_closed_without_manifest() -> None:
    config = ScoringEngineConfig(
        mode=RunMode.PAPER,
        database=DatabaseConfig(url="sqlite:///:memory:"),
        runtime=ScoringRuntimeConfig(environment="production"),
    )

    scope, required = scoring_main._load_paper_promotion_policy(config)

    assert required is True
    assert scope is None


def test_development_paper_policy_does_not_require_promotion() -> None:
    config = ScoringEngineConfig(
        mode=RunMode.PAPER,
        database=DatabaseConfig(url="sqlite:///:memory:"),
        runtime=ScoringRuntimeConfig(environment="dev"),
    )

    scope, required = scoring_main._load_paper_promotion_policy(config)

    assert required is False
    assert scope is None


def test_production_paper_policy_loads_exact_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = PaperPromotionScope(
        user_id="user-1",
        broker_account_id=101,
        strategy_binding_id=201,
        strategy_id="swing_high_low_pmo_v1",
        strategy_version="1.0.1",
        strategy_universe="BTCUSDC",
        model_scope="single_instrument",
        canonical_instrument="BTC-USDC",
        asset_class="crypto",
        broker_code="paper",
        data_use_scope=None,
        model_configuration_sha256=None,
        instrument_set_sha256=canonical_json_hash(
            {
                "schema": "paper-promotion-single-instrument-v1",
                "canonical_instrument": "BTC-USDC",
            }
        ),
        instruments=(),
        scoring_semantics="calibrated_forecast",
        order_evidence_profile="bracket_oco",
    )
    manifest_path = Path("/app/.artifacts/paper_strategy_promotion.json")
    config = ScoringEngineConfig(
        mode=RunMode.PAPER,
        database=DatabaseConfig(url="sqlite:///:memory:"),
        runtime=ScoringRuntimeConfig(
            environment="production",
            paper_promotion_manifest=manifest_path,
            deploy_image_tag="1.2.3",
        ),
    )

    monkeypatch.setattr(
        scoring_main,
        "load_paper_promotion_scope",
        lambda **kwargs: (expected, ()),
    )

    scope, required = scoring_main._load_paper_promotion_policy(config)

    assert required is True
    assert scope == expected


class _DeadSession:
    def execute(self, *_args, **_kwargs):
        raise SQLAlchemyError("connection pool exhausted")

    def close(self) -> None:
        pass


class _DeadStore:
    def get_session(self):
        return _DeadSession()


def test_lost_db_pool_is_not_ready() -> None:
    # A scoring instance with a dead pool must report not-ready so the
    # orchestrator stops routing signal traffic to it.
    assert _store_db_ready(_DeadStore()) is False


def test_relay_ready_when_inline_relay_not_running() -> None:
    # No inline relay task in this process -> readiness is not gated on it.
    scoring_main._runtime.relay_task = None
    try:
        assert scoring_main._relay_ready() is True
    finally:
        scoring_main._runtime.relay_task = None


def test_relay_not_ready_when_task_has_stopped() -> None:
    # A dead inline relay halts all execution-command delivery; /ready must fail
    # even though /health stays green.
    async def _completed_task() -> asyncio.Task[None]:
        async def _noop() -> None:
            return None

        task = asyncio.create_task(_noop())
        await task
        return task

    task = asyncio.run(_completed_task())
    scoring_main._runtime.relay_task = task
    try:
        assert task.done()
        assert scoring_main._relay_ready() is False
    finally:
        scoring_main._runtime.relay_task = None


def test_relay_ready_while_task_running() -> None:
    async def _check() -> bool:
        async def _forever() -> None:
            await asyncio.sleep(10)

        task = asyncio.create_task(_forever())
        scoring_main._runtime.relay_task = task
        try:
            return scoring_main._relay_ready()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            scoring_main._runtime.relay_task = None

    assert asyncio.run(_check()) is True


def test_relay_progress_ready_with_fresh_execution_command() -> None:
    store = InMemoryScoreStore()
    store.enqueue_event(
        topic="execution.commands",
        event_type="execution.command.requested",
        payload={"command_id": "command-1"},
    )
    relay_config = scoring_main.ScoringRelayConfig(max_backlog_age_seconds=60)

    assert scoring_main._relay_ready(store, relay_config) is True


def test_relay_progress_not_ready_when_execution_command_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryScoreStore()
    store.enqueue_event(
        topic="execution.commands",
        event_type="execution.command.requested",
        payload={"command_id": "command-1"},
    )
    stale_created_at = datetime.now(tz=UTC) - timedelta(seconds=61)
    monkeypatch.setattr(
        store,
        "oldest_outbox_undelivered_created_at",
        lambda *, topics=None: stale_created_at,
    )
    relay_config = scoring_main.ScoringRelayConfig(max_backlog_age_seconds=60)

    assert scoring_main._relay_ready(store, relay_config) is False


def test_relay_progress_not_ready_with_execution_command_dead_letter() -> None:
    store = InMemoryScoreStore()
    store.enqueue_event(
        topic="execution.commands",
        event_type="execution.command.requested",
        payload={"command_id": "command-1"},
    )
    claimed = store.claim_outbox_batch(
        topics=["execution.commands"],
        consumer="relay-1",
    )
    assert len(claimed) == 1
    assert store.mark_outbox_failed(
        claimed[0].event_id,
        expected_claim_owner="relay-1",
        expected_attempts=claimed[0].attempts,
        error_message="permanent contract rejection",
        failure_class="permanent",
    )
    relay_config = scoring_main.ScoringRelayConfig(max_backlog_age_seconds=60)

    assert scoring_main._relay_ready(store, relay_config) is False
