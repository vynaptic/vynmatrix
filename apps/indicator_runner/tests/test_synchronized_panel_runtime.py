"""Transactional orchestration tests for the synchronized-panel runtime."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateSchema, DropSchema

from indicator_runner.panel_binding import (
    PanelCorrectionError,
    PanelRuntimeBinding,
    scoped_panel_worker_id,
)
from indicator_runner.panel_runtime import SynchronizedPanelRuntime
from indicator_runner.runtime_journal import (
    MODEL_REBALANCE_SUBMISSION_TOPIC,
    BufferedSignalEmitter,
    StrategyRuntimeIdentity,
    StrategyRuntimeStore,
)
from lib_application.db.models import (
    Base,
    EquityObservation,
    EquityRankSnapshot,
    EquityRankSnapshotRow,
    EquitySourceLineage,
    Instrument,
    MarketCalendar,
    MarketSession,
    OutboxEvent,
    Strategy,
    StrategyPanelDecision,
    StrategyPanelInputRevision,
    StrategyRuntimeState,
    StrategyVersion,
    User,
)
from lib_application.services.strategy_panel_sessions import (
    market_session_content_sha256,
    market_session_source_identity,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.cross_sectional import (
    CrossSectionalSnapshot,
    FactorSpec,
    MissingFactorDecision,
    MissingFactorReason,
    PeerFallbackPolicy,
)
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.panels import (
    EffectivePanelMember,
    OfficialSessionCutoff,
    PanelAuditDecision,
    PanelEvaluationAudit,
    PanelEvaluationRow,
    PanelExclusion,
    PanelNotReadyError,
    PanelObservationRef,
    PanelReadyInput,
    SessionAuthority,
    evaluate_panel_readiness,
    panel_ready_input_from_payload,
    panel_ready_input_to_payload,
)
from lib_strategy.signals.pure_strategy import (
    MarketState,
    ModelStateContractError,
    PureSignalStrategy,
)

_STRATEGY_ID = "panel_runtime_contract"
_STRATEGY_VERSION = "1.0.0"
_WORKER_ID = "panel-runtime-contract-worker"
_INSTRUMENT_ID = 910_001
_SECURITY_ID = "US0378331005"
_ISSUER_ID = "APPLE-INC"
_SYMBOL = "AAPL"
_JANUARY_SESSION = date(2024, 1, 31)
_FEBRUARY_SESSION = date(2024, 2, 29)
_CLOCK = datetime(2024, 1, 31, 21, 30, tzinfo=UTC)
_FEBRUARY_CLOCK = datetime(2024, 2, 29, 21, 30, tzinfo=UTC)
_OWNER_USER_ID = "panel-runtime-owner"
_OTHER_OWNER_USER_ID = "panel-runtime-other-owner"
_CALENDAR_ID = 910_002
_CALENDAR_REFERENCE = "XNYS:regular-session-contract"
_CONFIGURATION_SHA256 = canonical_json_hash(
    {"strategy": _STRATEGY_ID, "factor_contract": "quality-v1"}
)
_POLICY = ProviderAuthorityPolicy(
    policy_version="panel-runtime-paper-v1",
    data_use_scope=DataUseScope.PAPER_FORWARD,
    rules=(
        ProviderAuthorityRule(
            provider="eodhd",
            decision=ProviderAuthorityDecision.ALLOW,
            entitlement_scopes=("personal_all_in_one",),
            entitlement_owner_user_id=_OWNER_USER_ID,
        ),
        ProviderAuthorityRule(
            provider="nyse",
            decision=ProviderAuthorityDecision.ALLOW,
            entitlement_scopes=("public_calendar",),
        ),
    ),
)


def _paper_policy(owner_user_id: str) -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version=f"panel-runtime-paper-{owner_user_id}-v1",
        data_use_scope=DataUseScope.PAPER_FORWARD,
        rules=(
            ProviderAuthorityRule(
                provider="eodhd",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("personal_all_in_one",),
                entitlement_owner_user_id=owner_user_id,
            ),
            ProviderAuthorityRule(
                provider="nyse",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("public_calendar",),
            ),
        ),
    )


_HISTORICAL_POLICY = ProviderAuthorityPolicy(
    policy_version="panel-runtime-historical-v1",
    data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
    rules=(
        ProviderAuthorityRule(
            provider="sec",
            decision=ProviderAuthorityDecision.ALLOW,
            entitlement_scopes=("public_filings",),
        ),
    ),
)

SessionMaker = sessionmaker[Session]


@dataclass(frozen=True, slots=True)
class _StrategyPanelInput:
    panel: PanelReadyInput
    revision_label: str


class _PanelStrategy(PureSignalStrategy):
    """Minimal stateful panel core used to exercise the production runtime."""

    def __init__(self, emitter: BufferedSignalEmitter) -> None:
        super().__init__(
            strategy_id=_STRATEGY_ID,
            strategy_type="indicator",
            config={
                "asset_class": "equity",
                "signal_source": "backtest",
                "strategy_version": _STRATEGY_VERSION,
            },
            emitter=emitter,
        )
        self.evaluation_calls = 0

    def initialize(self) -> None:
        self.warmup_bars_needed = 0

    def on_data(self, state: MarketState) -> None:
        del state

    def panel_ready_input(self, panel_input: object) -> PanelReadyInput:
        if not isinstance(panel_input, _StrategyPanelInput):
            raise ModelStateContractError("test strategy received an incompatible panel input")
        return panel_input.panel

    def serialize_panel_input(self, panel_input: object) -> Mapping[str, Any]:
        if not isinstance(panel_input, _StrategyPanelInput):
            raise ModelStateContractError("test strategy received an incompatible panel input")
        return {
            "schema": "panel-runtime-contract-input-v1",
            "revision_label": panel_input.revision_label,
            "panel": panel_ready_input_to_payload(panel_input.panel),
        }

    def deserialize_panel_input(self, payload: Mapping[str, Any]) -> object:
        if set(payload) != {"schema", "revision_label", "panel"}:
            raise ModelStateContractError("test panel payload fields changed")
        if payload.get("schema") != "panel-runtime-contract-input-v1":
            raise ModelStateContractError("test panel payload schema changed")
        raw_panel = payload.get("panel")
        revision_label = payload.get("revision_label")
        if not isinstance(raw_panel, Mapping) or not isinstance(revision_label, str):
            raise ModelStateContractError("test panel payload is malformed")
        return _StrategyPanelInput(
            panel=panel_ready_input_from_payload(raw_panel),
            revision_label=revision_label,
        )

    def evaluate_panel(self, panel_input: object) -> PanelEvaluationAudit:
        panel = self.panel_ready_input(panel_input)
        self.evaluation_calls += 1
        state = self.state_for(_SYMBOL)
        state.custom["completed_panels"] = int(state.custom.get("completed_panels", 0)) + 1
        return _audit(panel)


@dataclass(slots=True)
class _RuntimeHarness:
    strategy: _PanelStrategy
    signal_buffer: BufferedSignalEmitter
    store: StrategyRuntimeStore
    runtime: SynchronizedPanelRuntime
    failures: list[tuple[BaseException, str]]
    relay_calls: list[None]


def _calendar_lineage(*, retrieved_at: datetime) -> EquitySourceLineage:
    revision = retrieved_at.date().isoformat()
    return EquitySourceLineage(
        lineage_id=canonical_json_hash({"calendar_lineage": revision}),
        provider="nyse",
        product="regular-session-calendar",
        endpoint="https://www.nyse.com/markets/hours-calendars",
        dataset_version=revision,
        tool_version="panel-runtime-contract-v1",
        source_identity=_CALENDAR_REFERENCE,
        source_revision=revision,
        retrieved_at=retrieved_at,
        timestamp_semantics={"opens_at": "UTC", "closes_at": "UTC"},
        adjustment_policy="not-applicable",
        entitlement_scope="public_calendar",
        missing_data_policy="fail-closed",
        content_sha256=canonical_json_hash({"calendar_content": revision}),
    )


def _official_session(session_date: date) -> OfficialSessionCutoff:
    # 2024-01-31 and 2024-02-29 were ordinary NYSE sessions. During EST the
    # published 09:30-16:00 regular interval is 14:30-21:00 UTC.
    opens_at = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC).replace(
        hour=14,
        minute=30,
    )
    closes_at = opens_at.replace(hour=21, minute=0)
    calendar = MarketCalendar(
        calendar_id=_CALENDAR_ID,
        code="XNYS",
        source_kind="exchange",
        provider="nyse",
        source_reference=_CALENDAR_REFERENCE,
    )
    window = MarketSession(
        calendar_id=_CALENDAR_ID,
        opens_at=opens_at,
        closes_at=closes_at,
    )
    lineage = _calendar_lineage(retrieved_at=closes_at)
    return OfficialSessionCutoff(
        mic="XNYS",
        session_date=session_date,
        opens_at=opens_at,
        closes_at=closes_at,
        authority=SessionAuthority.OFFICIAL_EXCHANGE,
        source_identity=market_session_source_identity(calendar),
        content_sha256=market_session_content_sha256(calendar, window, lineage),
    )


def _panel_input(
    *,
    session_date: date = _JANUARY_SESSION,
    revision_label: str = "original",
    complete: bool = True,
    explicitly_excluded: bool = False,
    policy: ProviderAuthorityPolicy = _POLICY,
    knowledge_delay: timedelta = timedelta(0),
) -> _StrategyPanelInput:
    decision_session = _official_session(session_date)
    execution_session = _official_session(session_date + timedelta(days=1))
    cutoff = decision_session.closes_at + knowledge_delay
    observation = PanelObservationRef(
        security_id=_SECURITY_ID,
        observation_id=f"sec-panel:{session_date.isoformat()}:{revision_label}",
        observed_at=decision_session.closes_at - timedelta(minutes=5),
        available_at=cutoff,
        content_revision=1 if revision_label == "original" else 2,
        content_sha256=canonical_json_hash(
            {
                "security_id": _SECURITY_ID,
                "session_date": session_date.isoformat(),
                "revision_label": revision_label,
            }
        ),
    )
    panel = PanelReadyInput(
        cutoff=cutoff,
        session=decision_session,
        execution_session=execution_session,
        data_use_scope=policy.data_use_scope,
        provider_authority_policy=policy,
        provider_authority_sha256=policy.digest,
        membership_sha256=canonical_json_hash(
            {
                "universe": "SP500",
                "effective_session": session_date.isoformat(),
                "security_id": _SECURITY_ID,
            }
        ),
        factor_snapshot_sha256=canonical_json_hash(
            {
                "effective_session": session_date.isoformat(),
                "factor_contract": "quality-v1",
                "revision_label": revision_label,
            }
        ),
        members=(
            EffectivePanelMember(
                security_id=_SECURITY_ID,
                issuer_id=_ISSUER_ID,
                instrument_id=_INSTRUMENT_ID,
                canonical_symbol=_SYMBOL,
            ),
        ),
        observations=(observation,) if complete and not explicitly_excluded else (),
        exclusions=(
            (
                PanelExclusion(
                    security_id=_SECURITY_ID,
                    reason_code="point_in_time_exclusion",
                    disposition_identity=(
                        f"panel-exclusion:{session_date.isoformat()}:{revision_label}"
                    ),
                    content_sha256=observation.content_sha256,
                ),
            )
            if explicitly_excluded
            else ()
        ),
    )
    return _StrategyPanelInput(panel=panel, revision_label=revision_label)


def _audit(panel: PanelReadyInput) -> PanelEvaluationAudit:
    factor_spec = FactorSpec(name="quality", weight=1.0)
    source_identity = (
        panel.observations[0].observation_id
        if panel.observations
        else panel.exclusions[0].disposition_identity
    )
    exclusion_reason = "factor_evidence_incomplete" if panel.observations else "panel_excluded"
    missing = MissingFactorDecision(
        entity_id=_SECURITY_ID,
        symbol=_SYMBOL,
        factor_name="quality",
        reason=MissingFactorReason.SOURCE_MISSING,
        detail="cutoff evidence unavailable",
        source_observation_ids=(source_identity,),
    )
    rank_snapshot = CrossSectionalSnapshot(
        cutoff=panel.cutoff,
        calculation_version="panel-runtime-v1",
        minimum_peer_count=2,
        winsorize_limit=3.0,
        fallback_policy=PeerFallbackPolicy.INELIGIBLE,
        factor_specs=(factor_spec,),
        contributions=(),
        ranks=(),
        missing_decisions=(missing,),
    )
    return PanelEvaluationAudit(
        rank_snapshot=rank_snapshot,
        rows=(
            PanelEvaluationRow(
                entity_id=_SECURITY_ID,
                symbol=_SYMBOL,
                instrument_id=_INSTRUMENT_ID,
                factor_snapshot_id=None,
                rank_complete=False,
                strategy_eligible=False,
                decision=PanelAuditDecision.EXCLUDED,
                incumbent=False,
                rank=None,
                composite_score=None,
                target_allocation_hint=None,
                exclusion_reason=exclusion_reason,
            ),
        ),
        configuration_sha256=_CONFIGURATION_SHA256,
        peer_taxonomy_version="gics-2023",
        replayed=False,
        intentional_cash_slots=1,
        required_factor_versions=(("quality", "quality-v1"),),
    )


def _seed_catalog(session_factory: SessionMaker) -> None:
    observed_at = _official_session(_JANUARY_SESSION).closes_at
    lineage = _calendar_lineage(retrieved_at=observed_at)
    observation_id = canonical_json_hash({"calendar_observation": observed_at.isoformat()})
    with session_factory() as session:
        session.add(
            User(
                user_id=_OWNER_USER_ID,
                email="panel-runtime-owner@example.test",
                is_deployment_owner=True,
                base_ccy="USD",
            )
        )
        session.add(
            User(
                user_id=_OTHER_OWNER_USER_ID,
                email="panel-runtime-other-owner@example.test",
                base_ccy="USD",
            )
        )
        session.add(
            Strategy(
                strategy_id=_STRATEGY_ID,
                strategy_name="Synchronized panel runtime contract",
                asset_class="equity",
            )
        )
        session.add(
            StrategyVersion(
                strategy_id=_STRATEGY_ID,
                semver=_STRATEGY_VERSION,
                param_schema={},
                default_params={},
            )
        )
        session.add(lineage)
        session.flush()
        session.add(
            EquityObservation(
                observation_id=observation_id,
                lineage_id=lineage.lineage_id,
                instr_id=None,
                observation_kind="calendar",
                source_record_identity=_CALENDAR_REFERENCE,
                event_at=observed_at,
                available_at=observed_at,
                revision=1,
                disposition="observed",
                content_sha256=canonical_json_hash(
                    {"calendar_observation_content": observed_at.isoformat()}
                ),
            )
        )
        session.add(
            MarketCalendar(
                calendar_id=_CALENDAR_ID,
                code="XNYS",
                source_kind="exchange",
                provider="nyse",
                source_reference=_CALENDAR_REFERENCE,
                observation_id=observation_id,
                coverage_start=datetime(2024, 1, 1, tzinfo=UTC),
                coverage_end=datetime(2024, 3, 2, tzinfo=UTC),
                observed_at=observed_at,
            )
        )
        for session_date in (
            _JANUARY_SESSION,
            date(2024, 2, 1),
            _FEBRUARY_SESSION,
            date(2024, 3, 1),
        ):
            official = _official_session(session_date)
            session.add(
                MarketSession(
                    calendar_id=_CALENDAR_ID,
                    opens_at=official.opens_at,
                    closes_at=official.closes_at,
                )
            )
        session.add(
            Instrument(
                instr_id=_INSTRUMENT_ID,
                asset_class="equity",
                canonical=_SYMBOL,
                exchange="XNYS",
                settlement_currency="USD",
                is_tradable=True,
                market_session_policy="scheduled",
                market_calendar_id=_CALENDAR_ID,
            )
        )
        session.commit()


def _refresh_calendar_evidence(
    session_factory: SessionMaker,
    *,
    observed_at: datetime,
) -> None:
    lineage = _calendar_lineage(retrieved_at=observed_at)
    observation_id = canonical_json_hash({"calendar_observation": observed_at.isoformat()})
    with session_factory() as session:
        calendar = session.get(MarketCalendar, _CALENDAR_ID)
        assert calendar is not None
        prior_observation_id = str(calendar.observation_id)
        session.add(lineage)
        session.flush()
        session.add(
            EquityObservation(
                observation_id=observation_id,
                lineage_id=lineage.lineage_id,
                instr_id=None,
                observation_kind="calendar",
                source_record_identity=_CALENDAR_REFERENCE,
                event_at=observed_at,
                available_at=observed_at,
                revision=2,
                supersedes_observation_id=prior_observation_id,
                disposition="observed",
                content_sha256=canonical_json_hash(
                    {"calendar_observation_content": observed_at.isoformat()}
                ),
            )
        )
        session.flush()
        calendar.observation_id = observation_id
        calendar.observed_at = observed_at
        session.commit()


@pytest.fixture
def session_factory() -> Iterator[SessionMaker]:
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _seed_catalog(factory)
    try:
        yield factory
    finally:
        engine.dispose()


def _build_harness(
    session_factory: SessionMaker,
    *,
    worker_id: str = _WORKER_ID,
    binding: PanelRuntimeBinding | None = None,
    clock: Callable[[], datetime] | None = None,
) -> _RuntimeHarness:
    signal_buffer = BufferedSignalEmitter()
    strategy = _PanelStrategy(signal_buffer)
    runtime_binding = binding or PanelRuntimeBinding(
        environment="test",
        data_use_scope="paper_forward",
        entitlement_owner_user_id=_OWNER_USER_ID,
        activation_cutoff=datetime(2020, 1, 1, tzinfo=UTC),
    )
    identity = StrategyRuntimeIdentity.from_strategy(
        worker_id=worker_id,
        strategy=strategy,
        symbols=(),
        source="historical_validation",
        timeframe="1d",
        consolidation_minutes=0,
        panel_capable=True,
        panel_binding=runtime_binding,
    )
    store = StrategyRuntimeStore(session_factory, identity)
    failures: list[tuple[BaseException, str]] = []
    relay_calls: list[None] = []

    def relay_once() -> tuple[int, int]:
        relay_calls.append(None)
        return (0, 0)

    runtime = SynchronizedPanelRuntime(
        strategy=strategy,
        capability=strategy,
        session_factory=session_factory,
        signal_buffer=signal_buffer,
        runtime_store=store,
        processing_lock=threading.RLock(),
        relay_once=relay_once,
        mark_transition_failure=lambda exc, key: failures.append((exc, key)),
        clock=clock or (lambda: _CLOCK),
    )
    return _RuntimeHarness(
        strategy=strategy,
        signal_buffer=signal_buffer,
        store=store,
        runtime=runtime,
        failures=failures,
        relay_calls=relay_calls,
    )


def _register_input(
    session_factory: SessionMaker,
    strategy: _PanelStrategy,
    panel_input: _StrategyPanelInput,
) -> str:
    strategy_payload = dict(strategy.serialize_panel_input(panel_input))
    input_sha256 = canonical_json_hash(strategy_payload)
    panel = panel_input.panel
    readiness = evaluate_panel_readiness(panel)
    authority_payload = {
        "schema": "panel-runtime-validator-proof-v1",
        "input_sha256": input_sha256,
        "panel_sha256": readiness.panel_sha256,
        "validator": "panel-runtime-contract-validator",
    }
    with session_factory() as session:
        session.add(
            StrategyPanelInputRevision(
                input_sha256=input_sha256,
                strategy_id=_STRATEGY_ID,
                strategy_version=_STRATEGY_VERSION,
                universe_code="SP500",
                cutoff_at=panel.cutoff,
                official_session_date=panel.session.session_date,
                execute_not_before=panel.execution_session.opens_at,
                data_use_scope=panel.data_use_scope.value,
                entitlement_owner_user_id=(
                    panel.provider_authority_policy.effective_entitlement_owner_user_id
                ),
                provider_authority_sha256=panel.provider_authority_sha256,
                membership_sha256=panel.membership_sha256,
                factor_snapshot_sha256=panel.factor_snapshot_sha256,
                panel_sha256=readiness.panel_sha256,
                strategy_validator_id="panel-runtime-contract-validator",
                strategy_validator_version="1.0.0",
                strategy_input_authority_sha256=canonical_json_hash(authority_payload),
                strategy_input_authority_payload=authority_payload,
                panel_payload=panel_ready_input_to_payload(panel),
                strategy_input_payload=strategy_payload,
            )
        )
        session.commit()
    return input_sha256


def _count(session: Session, model: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_complete_submission_atomically_persists_state_rank_decision_and_outbox(
    session_factory: SessionMaker,
) -> None:
    harness = _build_harness(session_factory)
    panel_input = _panel_input()
    input_sha256 = _register_input(session_factory, harness.strategy, panel_input)

    assert harness.runtime.start() == 0
    assert harness.runtime.submit(panel_input) is True

    with session_factory() as session:
        runtime_state = session.get(StrategyRuntimeState, _WORKER_ID)
        decision = session.scalar(select(StrategyPanelDecision))
        rank = session.scalar(select(EquityRankSnapshot))
        rank_row = session.scalar(select(EquityRankSnapshotRow))
        outbox = session.scalar(select(OutboxEvent))
        assert runtime_state is not None
        assert runtime_state.generation == 2
        assert runtime_state.state_payload["symbol_states"][_SYMBOL]["custom"] == {
            "completed_panels": 1
        }
        assert decision is not None
        assert decision.status == "completed"
        assert decision.strategy_input_sha256 == input_sha256
        assert rank is not None
        assert decision.rank_snapshot_id == rank.rank_snapshot_id
        assert rank_row is not None
        assert rank_row.eligible is False
        assert outbox is not None
        assert outbox.topic == MODEL_REBALANCE_SUBMISSION_TOPIC
        assert outbox.ordering_key == harness.store.identity.ordering_key
        assert len(outbox.ordering_key) <= 128
        assert outbox.payload["input_snapshot_sha256"] == input_sha256
        assert outbox.payload["expected_leg_count"] == 0
    assert harness.strategy.evaluation_calls == 1
    assert harness.failures == []
    assert len(harness.relay_calls) == 1


def test_explicit_panel_exclusion_is_a_complete_audited_submission(
    session_factory: SessionMaker,
) -> None:
    harness = _build_harness(session_factory)
    panel_input = _panel_input(explicitly_excluded=True)
    _register_input(session_factory, harness.strategy, panel_input)
    harness.runtime.start()

    assert harness.runtime.submit(panel_input) is True

    with session_factory() as session:
        decision = session.scalar(select(StrategyPanelDecision))
        rank_row = session.scalar(select(EquityRankSnapshotRow))
        assert decision is not None
        assert decision.observation_count == 0
        assert decision.exclusion_count == 1
        audit_payload = decision.evaluation_audit_payload
        assert isinstance(audit_payload, Mapping)
        assert audit_payload["rows"][0]["exclusion_reason"] == "panel_excluded"
        assert rank_row is not None
        assert rank_row.exclusion_reason == "panel_excluded"
        assert _count(session, OutboxEvent) == 1


def test_incomplete_registered_panel_is_rejected_before_prepare(
    session_factory: SessionMaker,
) -> None:
    harness = _build_harness(session_factory)
    incomplete = _panel_input(complete=False)
    _register_input(session_factory, harness.strategy, incomplete)
    harness.runtime.start()

    with pytest.raises(PanelNotReadyError, match="not ready"):
        harness.runtime.submit(incomplete)

    with session_factory() as session:
        assert _count(session, StrategyPanelDecision) == 0
        assert _count(session, EquityRankSnapshot) == 0
        assert _count(session, OutboxEvent) == 0
        state = session.get(StrategyRuntimeState, _WORKER_ID)
        assert state is not None
        assert state.generation == 1


def test_same_cutoff_correction_is_retained_and_rejected(
    session_factory: SessionMaker,
) -> None:
    harness = _build_harness(session_factory)
    original = _panel_input()
    correction = _panel_input(revision_label="corrected")
    _register_input(session_factory, harness.strategy, original)
    _register_input(session_factory, harness.strategy, correction)
    harness.runtime.start()
    assert harness.runtime.submit(original) is True

    with pytest.raises(PanelCorrectionError, match="revision-pinned"):
        harness.runtime.submit(correction)

    with session_factory() as session:
        decisions = list(
            session.scalars(select(StrategyPanelDecision).order_by(StrategyPanelDecision.status))
        )
        assert {str(row.status) for row in decisions} == {
            "completed",
            "rejected_correction",
        }
        rejected = next(row for row in decisions if row.status == "rejected_correction")
        completed = next(row for row in decisions if row.status == "completed")
        assert rejected.correction_of_decision_key == completed.decision_key
        assert _count(session, EquityRankSnapshot) == 1
        assert _count(session, OutboxEvent) == 1
        state = session.get(StrategyRuntimeState, _WORKER_ID)
        assert state is not None
        assert state.generation == 2
    assert harness.strategy.evaluation_calls == 1


def test_later_cutoff_same_session_correction_is_terminal_across_restart(
    session_factory: SessionMaker,
) -> None:
    first = _build_harness(session_factory)
    original = _panel_input()
    correction = _panel_input(
        revision_label="later-available-correction",
        knowledge_delay=timedelta(minutes=30),
    )
    _register_input(session_factory, first.strategy, original)
    _register_input(session_factory, first.strategy, correction)
    first.runtime.start()
    preparation = first.store.prepare_panel(
        first.strategy,
        panel=original.panel,
        input_payload=first.strategy.serialize_panel_input(original),
        now=_CLOCK,
    )
    assert preparation.status == "prepared"

    with pytest.raises(PanelCorrectionError, match="revision-pinned"):
        first.runtime.submit(correction)

    with session_factory() as session:
        decisions = list(session.scalars(select(StrategyPanelDecision)))
        assert {str(row.status) for row in decisions} == {
            "prepared",
            "rejected_correction",
        }
        rejected = next(row for row in decisions if row.status == "rejected_correction")
        prepared = next(row for row in decisions if row.status == "prepared")
        assert rejected.official_session_date == prepared.official_session_date
        assert rejected.cutoff_at > prepared.cutoff_at
        assert rejected.correction_of_decision_key == prepared.decision_key
        assert rejected.rejection_reason == (
            "immutable panel content already pinned for this worker official session"
        )
        assert _count(session, EquityRankSnapshot) == 0
        assert _count(session, OutboxEvent) == 0
    assert first.strategy.evaluation_calls == 0

    restarted = _build_harness(session_factory)
    assert restarted.runtime.start() == 1
    assert restarted.strategy.evaluation_calls == 1
    assert restarted.runtime.submit(original) is False
    with pytest.raises(PanelCorrectionError, match="revision-pinned"):
        restarted.runtime.submit(correction)

    with session_factory() as session:
        decisions = list(session.scalars(select(StrategyPanelDecision)))
        assert [str(row.status) for row in decisions].count("completed") == 1
        assert [str(row.status) for row in decisions].count("rejected_correction") == 1
        assert _count(session, EquityRankSnapshot) == 1
        assert _count(session, OutboxEvent) == 1
        state = session.get(StrategyRuntimeState, _WORKER_ID)
        assert state is not None
        assert state.generation == 2


def test_exact_completed_input_replays_without_evaluation_or_duplicate_writes(
    session_factory: SessionMaker,
) -> None:
    harness = _build_harness(session_factory)
    panel_input = _panel_input()
    _register_input(session_factory, harness.strategy, panel_input)
    harness.runtime.start()
    assert harness.runtime.submit(panel_input) is True

    assert harness.runtime.submit(panel_input) is False

    with session_factory() as session:
        assert _count(session, StrategyPanelDecision) == 1
        assert _count(session, EquityRankSnapshot) == 1
        assert _count(session, EquityRankSnapshotRow) == 1
        assert _count(session, OutboxEvent) == 1
        state = session.get(StrategyRuntimeState, _WORKER_ID)
        assert state is not None
        assert state.generation == 2
    assert harness.strategy.evaluation_calls == 1
    assert len(harness.relay_calls) == 1


def test_crash_prepared_panel_is_completed_by_fresh_runtime_on_start(
    session_factory: SessionMaker,
) -> None:
    first = _build_harness(session_factory)
    panel_input = _panel_input()
    _register_input(session_factory, first.strategy, panel_input)
    first.runtime.start()
    preparation = first.store.prepare_panel(
        first.strategy,
        panel=panel_input.panel,
        input_payload=first.strategy.serialize_panel_input(panel_input),
        now=_CLOCK,
    )
    assert preparation.status == "prepared"

    restarted = _build_harness(session_factory)
    assert restarted.runtime.start() == 1

    with session_factory() as session:
        decision = session.get(StrategyPanelDecision, preparation.decision_key)
        state = session.get(StrategyRuntimeState, _WORKER_ID)
        assert decision is not None
        assert decision.status == "completed"
        assert state is not None
        assert state.generation == 2
        assert _count(session, EquityRankSnapshot) == 1
        assert _count(session, OutboxEvent) == 1
    assert first.strategy.evaluation_calls == 0
    assert restarted.strategy.evaluation_calls == 1
    assert len(restarted.relay_calls) == 1


def test_failed_completion_rolls_back_state_rank_and_outbox_atomically(
    session_factory: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(session_factory)
    panel_input = _panel_input()
    _register_input(session_factory, harness.strategy, panel_input)
    harness.runtime.start()
    original = harness.store.persist_panel_transition_on_session

    def fail_after_staging(session: Session, **kwargs: Any) -> StrategyRuntimeState:
        original(session, **kwargs)
        raise RuntimeError("injected failure after staging panel transaction")

    monkeypatch.setattr(
        harness.store,
        "persist_panel_transition_on_session",
        fail_after_staging,
    )

    with pytest.raises(RuntimeError, match="after staging"):
        harness.runtime.submit(panel_input)

    with session_factory() as session:
        decision = session.scalar(select(StrategyPanelDecision))
        state = session.get(StrategyRuntimeState, _WORKER_ID)
        assert decision is not None
        assert decision.status == "prepared"
        assert decision.state_generation is None
        assert decision.rank_snapshot_id is None
        assert state is not None
        assert state.generation == 1
        assert state.state_payload["symbol_states"] == {}
        assert _count(session, EquityRankSnapshot) == 0
        assert _count(session, EquityRankSnapshotRow) == 0
        assert _count(session, OutboxEvent) == 0
    assert harness.strategy.evaluation_calls == 1
    assert len(harness.failures) == 1
    assert harness.relay_calls == []


def test_unregistered_complete_input_cannot_bypass_immutable_registration(
    session_factory: SessionMaker,
) -> None:
    harness = _build_harness(session_factory)
    panel_input = _panel_input()
    harness.runtime.start()

    with pytest.raises(ModelStateContractError, match="immutable strategy-specific"):
        harness.runtime.submit(panel_input)

    with session_factory() as session:
        assert _count(session, StrategyPanelInputRevision) == 0
        assert _count(session, StrategyPanelDecision) == 0
        assert _count(session, EquityRankSnapshot) == 0
        assert _count(session, OutboxEvent) == 0


def test_paper_poll_is_exact_owner_and_never_consumes_historical_validation(
    session_factory: SessionMaker,
) -> None:
    owner_binding = PanelRuntimeBinding(
        environment="test",
        data_use_scope="paper_forward",
        entitlement_owner_user_id=_OWNER_USER_ID,
        activation_cutoff=datetime(2020, 1, 1, tzinfo=UTC),
    )
    other_binding = PanelRuntimeBinding(
        environment="test",
        data_use_scope="paper_forward",
        entitlement_owner_user_id=_OTHER_OWNER_USER_ID,
        activation_cutoff=datetime(2020, 1, 1, tzinfo=UTC),
    )

    def expired_clock() -> datetime:
        return datetime(2024, 2, 2, 22, tzinfo=UTC)

    owner_worker = scoped_panel_worker_id("shared-panel:paper", owner_binding)
    other_worker = scoped_panel_worker_id("shared-panel:paper", other_binding)
    owner = _build_harness(
        session_factory,
        worker_id=owner_worker,
        binding=owner_binding,
        clock=expired_clock,
    )
    other = _build_harness(
        session_factory,
        worker_id=other_worker,
        binding=other_binding,
        clock=expired_clock,
    )
    _register_input(session_factory, owner.strategy, _panel_input(policy=_POLICY))
    _register_input(
        session_factory,
        owner.strategy,
        _panel_input(policy=_paper_policy(_OTHER_OWNER_USER_ID)),
    )
    _register_input(
        session_factory,
        owner.strategy,
        _panel_input(policy=_HISTORICAL_POLICY),
    )

    owner.runtime.start()
    assert owner.runtime.poll_input_revisions(limit=10) == 0
    with session_factory() as session:
        decisions = list(session.scalars(select(StrategyPanelDecision)))
        assert len(decisions) == 1
        assert decisions[0].status == "expired"
        assert decisions[0].entitlement_owner_user_id == _OWNER_USER_ID

    with pytest.raises(ModelStateContractError, match="deployment owner"):
        other.runtime.start()
    with session_factory() as session:
        decisions = list(session.scalars(select(StrategyPanelDecision)))
        assert {row.entitlement_owner_user_id for row in decisions} == {_OWNER_USER_ID}
        assert {row.status for row in decisions} == {"expired"}
        historical_sha = canonical_json_hash(
            owner.strategy.serialize_panel_input(_panel_input(policy=_HISTORICAL_POLICY))
        )
        assert not any(row.strategy_input_sha256 == historical_sha for row in decisions)
    assert owner.strategy.evaluation_calls == 0
    assert other.strategy.evaluation_calls == 0


def test_activation_watermark_and_missed_prepared_window_are_terminal(
    session_factory: SessionMaker,
) -> None:
    skipped_binding = PanelRuntimeBinding(
        environment="test",
        data_use_scope="paper_forward",
        entitlement_owner_user_id=_OWNER_USER_ID,
        activation_cutoff=datetime(2024, 2, 1, tzinfo=UTC),
    )
    skipped_worker = scoped_panel_worker_id("activation-panel:paper", skipped_binding)
    skipped = _build_harness(
        session_factory,
        worker_id=skipped_worker,
        binding=skipped_binding,
    )
    skipped_input = _panel_input()
    _register_input(session_factory, skipped.strategy, skipped_input)
    skipped.runtime.start()
    assert skipped.runtime.poll_input_revisions() == 0

    active_binding = PanelRuntimeBinding(
        environment="test",
        data_use_scope="paper_forward",
        entitlement_owner_user_id=_OWNER_USER_ID,
        activation_cutoff=datetime(2020, 1, 1, tzinfo=UTC),
    )
    active_worker = scoped_panel_worker_id("expiry-panel:paper", active_binding)
    active = _build_harness(
        session_factory,
        worker_id=active_worker,
        binding=active_binding,
    )
    active_input = _panel_input(revision_label="prepared-for-expiry")
    _register_input(session_factory, active.strategy, active_input)
    active.runtime.start()
    preparation = active.store.prepare_panel(
        active.strategy,
        panel=active_input.panel,
        input_payload=active.strategy.serialize_panel_input(active_input),
        now=_CLOCK,
    )
    restarted = _build_harness(
        session_factory,
        worker_id=active_worker,
        binding=active_binding,
        clock=lambda: datetime(2024, 2, 2, 22, tzinfo=UTC),
    )
    assert restarted.runtime.start() == 0

    with session_factory() as session:
        skipped_row = session.scalar(
            select(StrategyPanelDecision).where(StrategyPanelDecision.worker_id == skipped_worker)
        )
        expired_row = session.get(StrategyPanelDecision, preparation.decision_key)
        assert skipped_row is not None
        assert skipped_row.status == "skipped"
        assert skipped_row.rejection_reason == "panel_cutoff_precedes_activation_watermark"
        assert expired_row is not None
        assert expired_row.status == "expired"
        assert expired_row.rejection_reason == "declared_execution_window_elapsed"
        assert expired_row.rank_snapshot_id is None
        assert _count(session, OutboxEvent) == 0
    assert skipped.strategy.evaluation_calls == 0
    assert restarted.strategy.evaluation_calls == 0


@contextmanager
def _postgres_database() -> Iterator[SessionMaker]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the PostgreSQL panel-runtime gate")
    base_engine = sa.create_engine(database_url, future=True, pool_pre_ping=True)
    if base_engine.dialect.name != "postgresql":
        base_engine.dispose()
        pytest.skip("PostgreSQL panel-runtime gate requires a PostgreSQL DATABASE_URL")
    schema_name = f"panel_runtime_{uuid4().hex}"
    translated_engine: Engine | None = None
    try:
        with base_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
        translated_engine = base_engine.execution_options(schema_translate_map={None: schema_name})
        Base.metadata.create_all(translated_engine)
        factory = sessionmaker(bind=translated_engine, expire_on_commit=False)
        _seed_catalog(factory)
        yield factory
    finally:
        with base_engine.begin() as connection:
            connection.execute(DropSchema(schema_name, cascade=True, if_exists=True))
        base_engine.dispose()


def test_postgres_same_session_later_cutoff_race_is_worker_serialized() -> None:
    with _postgres_database() as postgres_factory:
        worker_id = "panel-runtime-concurrent-correction-worker"
        first = _build_harness(postgres_factory, worker_id=worker_id)
        second = _build_harness(postgres_factory, worker_id=worker_id)
        original = _panel_input()
        correction = _panel_input(
            revision_label="later-available-correction",
            knowledge_delay=timedelta(minutes=30),
        )
        _register_input(postgres_factory, first.strategy, original)
        _register_input(postgres_factory, first.strategy, correction)
        assert first.runtime.start() == 0
        assert second.runtime.start() == 0
        barrier = threading.Barrier(2)

        def submit(
            runtime: SynchronizedPanelRuntime,
            panel_input: _StrategyPanelInput,
        ) -> str:
            barrier.wait(timeout=10)
            try:
                return "completed" if runtime.submit(panel_input) else "replayed"
            except PanelCorrectionError:
                return "rejected_correction"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(
                future.result(timeout=30)
                for future in (
                    executor.submit(submit, first.runtime, original),
                    executor.submit(submit, second.runtime, correction),
                )
            )

        assert sorted(outcomes) == ["completed", "rejected_correction"]
        assert first.strategy.evaluation_calls + second.strategy.evaluation_calls == 1
        with postgres_factory() as session:
            decisions = list(
                session.scalars(
                    select(StrategyPanelDecision).where(
                        StrategyPanelDecision.worker_id == worker_id
                    )
                )
            )
            assert {str(row.status) for row in decisions} == {
                "completed",
                "rejected_correction",
            }
            completed = next(row for row in decisions if row.status == "completed")
            rejected = next(row for row in decisions if row.status == "rejected_correction")
            assert completed.official_session_date == rejected.official_session_date
            assert completed.cutoff_at != rejected.cutoff_at
            assert rejected.correction_of_decision_key == completed.decision_key
            assert _count(session, EquityRankSnapshot) == 1
            assert _count(session, OutboxEvent) == 1

        restarted = _build_harness(postgres_factory, worker_id=worker_id)
        assert restarted.runtime.start() == 0
        assert restarted.strategy.evaluation_calls == 0


def test_postgres_concurrent_submission_replay_correction_and_restart_acceptance() -> None:
    with _postgres_database() as postgres_factory:
        first = _build_harness(postgres_factory)
        second = _build_harness(postgres_factory)
        first_input = _panel_input()
        _register_input(postgres_factory, first.strategy, first_input)
        assert first.runtime.start() == 0
        assert second.runtime.start() == 0
        barrier = threading.Barrier(2)

        def submit(runtime: SynchronizedPanelRuntime) -> bool:
            barrier.wait(timeout=10)
            return runtime.submit(first_input)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(
                future.result(timeout=30)
                for future in (
                    executor.submit(submit, first.runtime),
                    executor.submit(submit, second.runtime),
                )
            )
        assert sorted(results) == [False, True]
        assert first.strategy.evaluation_calls + second.strategy.evaluation_calls == 1
        assert first.runtime.submit(first_input) is False

        correction = _panel_input(
            revision_label="later-available-correction",
            knowledge_delay=timedelta(minutes=30),
        )
        _register_input(postgres_factory, first.strategy, correction)
        with pytest.raises(PanelCorrectionError, match="revision-pinned"):
            first.runtime.submit(correction)

        correction_restart = _build_harness(postgres_factory)
        assert correction_restart.runtime.start() == 0
        assert correction_restart.strategy.evaluation_calls == 0

        next_input = _panel_input(session_date=_FEBRUARY_SESSION)
        next_correction = _panel_input(
            session_date=_FEBRUARY_SESSION,
            revision_label="later-available-correction",
            knowledge_delay=timedelta(minutes=30),
        )
        _refresh_calendar_evidence(
            postgres_factory,
            observed_at=next_input.panel.cutoff,
        )
        _register_input(postgres_factory, first.strategy, next_input)
        _register_input(postgres_factory, first.strategy, next_correction)
        preparer = _build_harness(
            postgres_factory,
            clock=lambda: _FEBRUARY_CLOCK,
        )
        assert preparer.runtime.start() == 0
        preparation = preparer.store.prepare_panel(
            preparer.strategy,
            panel=next_input.panel,
            input_payload=preparer.strategy.serialize_panel_input(next_input),
            now=_FEBRUARY_CLOCK,
        )
        assert preparation.status == "prepared"
        with pytest.raises(PanelCorrectionError, match="revision-pinned"):
            preparer.runtime.submit(next_correction)
        restarted = _build_harness(
            postgres_factory,
            clock=lambda: _FEBRUARY_CLOCK,
        )
        assert restarted.runtime.start() == 1

        with postgres_factory() as session:
            statuses = list(
                session.scalars(
                    select(StrategyPanelDecision.status).order_by(
                        StrategyPanelDecision.cutoff_at,
                        StrategyPanelDecision.status,
                    )
                )
            )
            assert statuses.count("completed") == 2
            assert statuses.count("rejected_correction") == 2
            assert _count(session, EquityRankSnapshot) == 2
            assert _count(session, EquityRankSnapshotRow) == 2
            assert _count(session, OutboxEvent) == 2
            state = session.get(StrategyRuntimeState, _WORKER_ID)
            assert state is not None
            assert state.generation == 3


def test_panel_owner_revocation_blocks_new_work_without_rewriting_history(
    session_factory: SessionMaker,
) -> None:
    harness = _build_harness(session_factory)
    harness.runtime.start()
    with session_factory() as session:
        session.get(User, _OWNER_USER_ID).is_deployment_owner = False
        session.commit()
    with pytest.raises(ModelStateContractError, match="deployment owner"):
        harness.runtime.poll_input_revisions()
    with session_factory() as session:
        assert _count(session, StrategyPanelDecision) == 0
        assert _count(session, StrategyRuntimeState) == 1
