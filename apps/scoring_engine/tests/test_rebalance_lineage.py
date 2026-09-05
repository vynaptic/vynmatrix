from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url

from lib_application.db import models as app_models
from lib_common.hashing import canonical_json_hash
from lib_common.internal_events import (
    CanonicalSignalSnapshot,
    ModelRebalanceLegSnapshot,
    ModelRebalanceSubmissionEvent,
    compute_model_rebalance_content_sha256,
    compute_model_rebalance_id,
)
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.equity_optional_factors import optional_factor_source_registry_sha256
from lib_strategy.panels import (
    EffectivePanelMember,
    OfficialSessionCutoff,
    PanelObservationRef,
    PanelReadyInput,
    SessionAuthority,
    panel_ready_input_to_payload,
)
from scoring_engine.dispatcher import DispatchProviderContexts, ExecutionDispatcher
from scoring_engine.domain import MarketContext, MarketRegime
from scoring_engine.engine import ScoreEngine
from scoring_engine.services.rebalance_service import RebalanceScoringService
from scoring_engine.storage import AppScoreStore

_STRATEGY_ID = "rebalance_lineage_strategy"
_STRATEGY_VERSION = "1.0.0"
_STRATEGY_VERSION_ID = 8_801
_INSTRUMENT_ID = 8_802
_WORKER_ID = "rebalance-lineage-worker"
_OWNER_ID = "rebalance-lineage-owner"
_RUNTIME_ENVIRONMENT = "dev"
_ACTIVATION_CUTOFF = datetime(2020, 1, 1, tzinfo=UTC)
_CONFIGURATION = sha256(b"rebalance-lineage-configuration").hexdigest()
_AUTHORITY_POLICY = ProviderAuthorityPolicy(
    policy_version="rebalance-lineage-v1",
    data_use_scope=DataUseScope.PAPER_FORWARD,
    rules=(
        ProviderAuthorityRule(
            provider="test-authority",
            decision=ProviderAuthorityDecision.ALLOW,
            entitlement_scopes=("paper",),
        ),
    ),
    entitlement_owner_user_id=_OWNER_ID,
)


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _store(
    database_url: str = "sqlite+pysqlite:///:memory:",
    *,
    panel_owner_user_id: str = _OWNER_ID,
) -> AppScoreStore:
    store = AppScoreStore(database_url)
    with store.get_session() as session:
        session.add_all(
            [
                app_models.User(
                    user_id=panel_owner_user_id,
                    email=f"{panel_owner_user_id}@example.invalid",
                    base_ccy="USD",
                    status="active",
                ),
                app_models.Instrument(
                    instr_id=_INSTRUMENT_ID,
                    asset_class="equity",
                    canonical="LINEAGE",
                    exchange="NYSE",
                    settlement_currency="USD",
                    market_session_policy="scheduled",
                ),
                app_models.Strategy(
                    strategy_id=_STRATEGY_ID,
                    strategy_name="Rebalance lineage strategy",
                    asset_class="equity",
                ),
                app_models.StrategyVersion(
                    strat_ver_id=_STRATEGY_VERSION_ID,
                    strategy_id=_STRATEGY_ID,
                    semver=_STRATEGY_VERSION,
                    param_schema={},
                    default_params={},
                    status="active",
                ),
                app_models.StrategyRuntimeState(
                    worker_id=_WORKER_ID,
                    strategy_id=_STRATEGY_ID,
                    strategy_version=_STRATEGY_VERSION,
                    symbols=["LINEAGE"],
                    source="test",
                    timeframe="1d",
                    consolidation_minutes=0,
                    config_fingerprint=_CONFIGURATION,
                    state_schema_version=1,
                    state_payload={},
                    generation=1,
                    panel_runtime_environment=_RUNTIME_ENVIRONMENT,
                    panel_data_use_scope="paper_forward",
                    panel_entitlement_owner_user_id=panel_owner_user_id,
                    panel_activation_cutoff=_ACTIVATION_CUTOFF,
                ),
            ]
        )
        session.commit()
    return store


def _seed_factor(
    store: AppScoreStore,
    *,
    label: str,
    effective_session: date,
    cutoff: datetime,
    completeness_status: str = "complete",
) -> str:
    factor_id = _digest(f"factor:{label}")
    with store.get_session() as session:
        session.add(
            app_models.EquityFactorSnapshot(
                factor_snapshot_id=factor_id,
                strategy_id=_STRATEGY_ID,
                strat_ver_id=_STRATEGY_VERSION_ID,
                instr_id=_INSTRUMENT_ID,
                effective_session=effective_session,
                cutoff_at=cutoff,
                calculation_version="1.0.0",
                configuration_digest=_CONFIGURATION,
                source_contract_registry_sha256=optional_factor_source_registry_sha256(),
                peer_taxonomy_version="gics-test-v1",
                peer_group="Information Technology",
                completeness_status=completeness_status,
                expected_factor_count=4,
                available_factor_count=(4 if completeness_status == "complete" else 3),
                content_sha256=factor_id,
            )
        )
        session.commit()
    return factor_id


def _panel(
    *,
    label: str,
    effective_session: date,
    cutoff: datetime,
    authority_policy: ProviderAuthorityPolicy = _AUTHORITY_POLICY,
) -> PanelReadyInput:
    factor_content = _digest(f"factor-content:{label}")
    return PanelReadyInput(
        cutoff=cutoff,
        session=OfficialSessionCutoff(
            mic="XNYS",
            session_date=effective_session,
            opens_at=cutoff - timedelta(hours=6, minutes=30),
            closes_at=cutoff,
            authority=SessionAuthority.AUTHENTICATED_BROKER,
            source_identity="test-broker:XNYS",
            content_sha256=_digest(f"decision-session:{label}"),
        ),
        execution_session=OfficialSessionCutoff(
            mic="XNYS",
            session_date=effective_session + timedelta(days=1),
            opens_at=cutoff + timedelta(days=1),
            closes_at=cutoff + timedelta(days=1, hours=6, minutes=30),
            authority=SessionAuthority.AUTHENTICATED_BROKER,
            source_identity="test-broker:XNYS",
            content_sha256=_digest(f"execution-session:{label}"),
        ),
        data_use_scope=DataUseScope.PAPER_FORWARD,
        provider_authority_policy=authority_policy,
        provider_authority_sha256=authority_policy.digest,
        membership_sha256=_digest(f"membership:{label}"),
        factor_snapshot_sha256=factor_content,
        members=(
            EffectivePanelMember(
                security_id="security-lineage",
                issuer_id="issuer-lineage",
                instrument_id=_INSTRUMENT_ID,
                canonical_symbol="LINEAGE",
            ),
        ),
        observations=(
            PanelObservationRef(
                security_id="security-lineage",
                observation_id=f"observation-{label}",
                observed_at=cutoff - timedelta(days=1),
                available_at=cutoff - timedelta(hours=1),
                content_revision=1,
                content_sha256=_digest(f"observation:{label}"),
            ),
        ),
    )


def _seed_rank(
    store: AppScoreStore,
    *,
    label: str,
    effective_session: date,
    cutoff: datetime,
    factor_id: str | None,
    exclusion_reason: str | None = None,
    omit_row: bool = False,
    authority_policy: ProviderAuthorityPolicy = _AUTHORITY_POLICY,
) -> str:
    rank_id = _digest(f"rank:{label}")
    panel = _panel(
        label=label,
        effective_session=effective_session,
        cutoff=cutoff,
        authority_policy=authority_policy,
    )
    has_row = not omit_row
    strategy_eligible = factor_id is not None and exclusion_reason is None
    with store.get_session() as session:
        session.add(
            app_models.EquityRankSnapshot(
                rank_snapshot_id=rank_id,
                strategy_id=_STRATEGY_ID,
                strat_ver_id=_STRATEGY_VERSION_ID,
                effective_session=effective_session,
                cutoff_at=cutoff,
                configuration_digest=_CONFIGURATION,
                panel_revision_digest=panel.canonical_digest(),
                factor_content_digest=panel.factor_snapshot_sha256,
                data_use_scope="paper_forward",
                provider_authority_digest=authority_policy.digest,
                provider_authority_policy=authority_policy.to_payload(),
                peer_taxonomy_version="gics-test-v1",
                completeness_status="complete",
                expected_instrument_count=(1 if has_row else 0),
                included_instrument_count=(1 if has_row and strategy_eligible else 0),
                excluded_instrument_count=(1 if has_row and not strategy_eligible else 0),
                content_sha256=rank_id,
            )
        )
        if has_row:
            rank_complete = factor_id is not None
            session.add(
                app_models.EquityRankSnapshotRow(
                    rank_snapshot_id=rank_id,
                    instr_id=_INSTRUMENT_ID,
                    factor_snapshot_id=factor_id,
                    eligible=rank_complete,
                    strategy_eligible=strategy_eligible,
                    row_ordinal=0,
                    rank_position=(Decimal("1") if rank_complete else None),
                    composite_score=(Decimal("0.8") if rank_complete else None),
                    target_allocation_hint=(Decimal("0.05") if strategy_eligible else None),
                    decision=("selected" if strategy_eligible else "exit"),
                    incumbent=True,
                    exclusion_reason=exclusion_reason,
                )
            )
        session.commit()
    return rank_id


def _event(
    *,
    store: AppScoreStore,
    label: str,
    effective_session: date,
    cutoff: datetime,
    rank_id: str,
    factor_id: str,
    action: str,
    phase: str,
    current_rank_row_present: bool,
    current_rank_factor_snapshot_id: str | None,
    membership_change_reason: str | None = None,
    symbol: str = "LINEAGE",
    allocation_hint: float = 0.05,
    composite_score: float = 0.8,
    seed_panel: bool = True,
    authority_policy: ProviderAuthorityPolicy = _AUTHORITY_POLICY,
) -> ModelRebalanceSubmissionEvent:
    execute_at = cutoff + timedelta(days=1)
    execution_session = _digest(f"execution-session:{label}")
    strategy_input_payload = {"label": label}
    input_snapshot = canonical_json_hash(strategy_input_payload)
    rank = 1.0 if current_rank_factor_snapshot_id is not None else None
    signal = CanonicalSignalSnapshot(
        signal_id=f"signal-{label}",
        strategy_id=_STRATEGY_ID,
        strategy_type="indicator",
        symbol=symbol,
        action=action,
        confidence=0.9,
        timestamp=cutoff,
        horizon="swing",
        expected_return=0.01,
        predicted_risk=0.02,
        horizon_days=20,
        asset_class="equity",
        instrument_id=str(_INSTRUMENT_ID),
        strategy_version=_STRATEGY_VERSION,
        run_id=f"run-{label}",
        source="paper",
        external_signal_id=f"external-{label}",
        metadata={
            "factor_snapshot_id": factor_id,
            "current_rank_row_present": current_rank_row_present,
            "current_rank_factor_snapshot_id": current_rank_factor_snapshot_id,
            "composite_score": (
                composite_score if current_rank_factor_snapshot_id is not None else None
            ),
            "model_rebalance_sequence": 0,
            "configuration_sha256": _CONFIGURATION,
            "strategy_input_sha256": input_snapshot,
            "rank_snapshot_sha256": rank_id,
            "provider_authority_sha256": authority_policy.digest,
            "data_use_scope": "paper_forward",
            "execution_session_sha256": execution_session,
            "execute_not_before": execute_at.isoformat(),
        },
    )
    leg = ModelRebalanceLegSnapshot(
        leg_id=f"leg-{label}",
        sequence=0,
        phase=phase,
        signal=signal,
        rank=rank,
        allocation_hint=(0.0 if phase == "exit" else allocation_hint),
        factor_snapshot_id=factor_id,
        rank_snapshot_id=rank_id,
        current_rank_row_present=current_rank_row_present,
        current_rank_factor_snapshot_id=current_rank_factor_snapshot_id,
        membership_change_reason=membership_change_reason,
    )
    content = compute_model_rebalance_content_sha256(
        strategy_id=_STRATEGY_ID,
        strategy_version=_STRATEGY_VERSION,
        effective_session=effective_session,
        decision_cutoff=cutoff,
        execute_not_before=execute_at,
        execution_session_sha256=execution_session,
        data_use_scope="paper_forward",
        provider_authority_sha256=authority_policy.digest,
        configuration_sha256=_CONFIGURATION,
        input_snapshot_sha256=input_snapshot,
        rank_snapshot_sha256=rank_id,
        intentional_cash_slots=0,
        legs=[leg],
    )
    event = ModelRebalanceSubmissionEvent(
        event_id=f"event-{label}",
        occurred_at=cutoff,
        run_id=f"run-{label}",
        producer="indicator_runner",
        rebalance_id=compute_model_rebalance_id(
            strategy_id=_STRATEGY_ID,
            strategy_version=_STRATEGY_VERSION,
            effective_session=effective_session,
            execute_not_before=execute_at,
            execution_session_sha256=execution_session,
            data_use_scope="paper_forward",
            provider_authority_sha256=authority_policy.digest,
            configuration_sha256=_CONFIGURATION,
            input_snapshot_sha256=input_snapshot,
            content_sha256=content,
        ),
        strategy_id=_STRATEGY_ID,
        strategy_version=_STRATEGY_VERSION,
        effective_session=effective_session,
        decision_cutoff=cutoff,
        execute_not_before=execute_at,
        execution_session_sha256=execution_session,
        data_use_scope="paper_forward",
        provider_authority_sha256=authority_policy.digest,
        configuration_sha256=_CONFIGURATION,
        input_snapshot_sha256=input_snapshot,
        rank_snapshot_sha256=rank_id,
        content_sha256=content,
        expected_leg_count=1,
        intentional_cash_slots=0,
        legs=[leg],
    )
    if seed_panel:
        _seed_completed_panel_decision(
            store,
            event=event,
            label=label,
            strategy_input_payload=strategy_input_payload,
            authority_policy=authority_policy,
        )
    return event


def _seed_completed_panel_decision(
    store: AppScoreStore,
    *,
    event: ModelRebalanceSubmissionEvent,
    label: str,
    strategy_input_payload: dict[str, str],
    authority_policy: ProviderAuthorityPolicy = _AUTHORITY_POLICY,
) -> None:
    panel = _panel(
        label=label,
        effective_session=event.effective_session,
        cutoff=event.decision_cutoff,
        authority_policy=authority_policy,
    )
    audit_payload = {
        "configuration_sha256": event.configuration_sha256,
        "intentional_cash_slots": event.intentional_cash_slots,
        "rank_snapshot": {"content_digest": event.rank_snapshot_sha256},
    }
    with store.get_session() as session:
        session.add(
            app_models.StrategyPanelDecision(
                decision_key=_digest(f"panel-decision:{label}"),
                worker_id=_WORKER_ID,
                strategy_id=_STRATEGY_ID,
                strategy_version=_STRATEGY_VERSION,
                cutoff_at=event.decision_cutoff,
                official_session_date=event.effective_session,
                official_session_sha256=panel.session.content_sha256,
                execute_not_before=event.execute_not_before,
                execution_session_sha256=event.execution_session_sha256,
                data_use_scope=event.data_use_scope,
                entitlement_owner_user_id=(authority_policy.effective_entitlement_owner_user_id),
                runtime_environment=_RUNTIME_ENVIRONMENT,
                activation_cutoff=_ACTIVATION_CUTOFF,
                provider_authority_policy=authority_policy.to_payload(),
                provider_authority_sha256=event.provider_authority_sha256,
                membership_sha256=panel.membership_sha256,
                factor_snapshot_sha256=panel.factor_snapshot_sha256,
                panel_sha256=panel.canonical_digest(),
                strategy_input_sha256=event.input_snapshot_sha256,
                member_count=1,
                observation_count=1,
                exclusion_count=0,
                maximum_content_revision=1,
                panel_payload=panel_ready_input_to_payload(panel),
                strategy_input_payload=strategy_input_payload,
                status="completed",
                state_schema_version=1,
                prepared_state_generation=1,
                prepared_state_sha256=canonical_json_hash({}),
                state_generation=2,
                state_sha256=canonical_json_hash({"completed": label}),
                state_payload={"completed": label},
                rank_snapshot_id=event.rank_snapshot_sha256,
                evaluation_audit_sha256=canonical_json_hash(audit_payload),
                evaluation_audit_payload=audit_payload,
                signal_envelopes=[leg.signal.model_dump(mode="json") for leg in event.legs],
                completed_at=event.decision_cutoff,
            )
        )
        session.commit()


def _rehashed_event_with_signal_update(
    event: ModelRebalanceSubmissionEvent,
    *,
    field_name: str,
    value: object,
) -> ModelRebalanceSubmissionEvent:
    signal = event.legs[0].signal.model_copy(update={field_name: value})
    leg = event.legs[0].model_copy(update={"signal": signal})
    content = compute_model_rebalance_content_sha256(
        strategy_id=event.strategy_id,
        strategy_version=event.strategy_version,
        effective_session=event.effective_session,
        decision_cutoff=event.decision_cutoff,
        execute_not_before=event.execute_not_before,
        execution_session_sha256=event.execution_session_sha256,
        data_use_scope=event.data_use_scope,
        provider_authority_sha256=event.provider_authority_sha256,
        configuration_sha256=event.configuration_sha256,
        input_snapshot_sha256=event.input_snapshot_sha256,
        rank_snapshot_sha256=event.rank_snapshot_sha256,
        intentional_cash_slots=event.intentional_cash_slots,
        legs=[leg],
    )
    rebalance_id = compute_model_rebalance_id(
        strategy_id=event.strategy_id,
        strategy_version=event.strategy_version,
        effective_session=event.effective_session,
        execute_not_before=event.execute_not_before,
        execution_session_sha256=event.execution_session_sha256,
        data_use_scope=event.data_use_scope,
        provider_authority_sha256=event.provider_authority_sha256,
        configuration_sha256=event.configuration_sha256,
        input_snapshot_sha256=event.input_snapshot_sha256,
        content_sha256=content,
    )
    payload = event.model_dump(mode="python")
    payload.update(
        {
            "event_id": rebalance_id,
            "rebalance_id": rebalance_id,
            "content_sha256": content,
            "legs": [leg.model_dump(mode="python")],
        }
    )
    return ModelRebalanceSubmissionEvent.model_validate(payload)


def _persist(
    store: AppScoreStore,
    event: ModelRebalanceSubmissionEvent,
    *,
    persist_signal: bool = True,
) -> None:
    with store.unit_of_work() as session:
        assert store.classify_model_rebalance(event) == "new"
        if persist_signal:
            signal = event.legs[0].signal
            session.add(
                app_models.CanonicalSignal(
                    strategy_id=_STRATEGY_ID,
                    strat_ver_id=_STRATEGY_VERSION_ID,
                    instr_id=_INSTRUMENT_ID,
                    action={"long": "long", "hold": "hold", "close": "flat"}[signal.action],
                    confidence=Decimal("0.9"),
                    direction=("flat" if signal.action == "close" else "long"),
                    source_runner="indicator",
                    run_id=event.run_id,
                    external_signal_id=signal.external_signal_id,
                    ts=event.decision_cutoff,
                )
            )
            session.flush()
        store.persist_model_rebalance(event)


def _seed_initial_target(store: AppScoreStore) -> tuple[ModelRebalanceSubmissionEvent, str]:
    effective_session = date(2026, 1, 2)
    cutoff = datetime(2026, 1, 2, 21, 0, tzinfo=UTC)
    factor_id = _seed_factor(
        store,
        label="initial",
        effective_session=effective_session,
        cutoff=cutoff,
    )
    rank_id = _seed_rank(
        store,
        label="initial",
        effective_session=effective_session,
        cutoff=cutoff,
        factor_id=factor_id,
    )
    event = _event(
        store=store,
        label="initial",
        effective_session=effective_session,
        cutoff=cutoff,
        rank_id=rank_id,
        factor_id=factor_id,
        action="long",
        phase="entry",
        current_rank_row_present=True,
        current_rank_factor_snapshot_id=factor_id,
    )
    _persist(store, event)
    return event, factor_id


def test_database_store_resolves_personal_paper_entitlement_owner_from_rank_policy() -> None:
    owner_user_id = "personal-owner"
    store = _store(panel_owner_user_id=owner_user_id)
    owner_policy = ProviderAuthorityPolicy(
        policy_version="rebalance-personal-paper-v1",
        data_use_scope=DataUseScope.PAPER_FORWARD,
        rules=_AUTHORITY_POLICY.rules,
        entitlement_owner_user_id=owner_user_id,
    )
    effective_session = date(2026, 1, 2)
    cutoff = datetime(2026, 1, 2, 21, 0, tzinfo=UTC)
    factor_id = _seed_factor(
        store,
        label="personal-owner",
        effective_session=effective_session,
        cutoff=cutoff,
    )
    rank_id = _seed_rank(
        store,
        label="personal-owner",
        effective_session=effective_session,
        cutoff=cutoff,
        factor_id=factor_id,
        authority_policy=owner_policy,
    )
    event = _event(
        store=store,
        label="personal-owner",
        effective_session=effective_session,
        cutoff=cutoff,
        rank_id=rank_id,
        factor_id=factor_id,
        action="long",
        phase="entry",
        current_rank_row_present=True,
        current_rank_factor_snapshot_id=factor_id,
        authority_policy=owner_policy,
    )

    assert store.classify_model_rebalance(event) == "new"
    assert store.resolve_model_rebalance_entitlement_owner(event) == owner_user_id


@pytest.mark.parametrize(
    ("case", "expected_prior"),
    [
        ("left_effective_universe", True),
        ("panel_excluded", True),
        ("factor_evidence_incomplete", False),
    ],
)
def test_exit_lineage_matrix_uses_only_the_required_prior_target_proof(
    case: str,
    expected_prior: bool,
) -> None:
    store = _store()
    initial, prior_factor_id = _seed_initial_target(store)
    effective_session = date(2026, 1, 9)
    cutoff = datetime(2026, 1, 9, 21, 0, tzinfo=UTC)

    if case == "left_effective_universe":
        factor_id = prior_factor_id
        rank_id = _seed_rank(
            store,
            label=case,
            effective_session=effective_session,
            cutoff=cutoff,
            factor_id=None,
            omit_row=True,
        )
        current_row = False
    elif case == "panel_excluded":
        factor_id = prior_factor_id
        rank_id = _seed_rank(
            store,
            label=case,
            effective_session=effective_session,
            cutoff=cutoff,
            factor_id=None,
            exclusion_reason=case,
        )
        current_row = True
    else:
        factor_id = _seed_factor(
            store,
            label=case,
            effective_session=effective_session,
            cutoff=cutoff,
            completeness_status="ineligible",
        )
        rank_id = _seed_rank(
            store,
            label=case,
            effective_session=effective_session,
            cutoff=cutoff,
            factor_id=None,
            exclusion_reason=case,
        )
        current_row = True

    event = _event(
        store=store,
        label=case,
        effective_session=effective_session,
        cutoff=cutoff,
        rank_id=rank_id,
        factor_id=factor_id,
        action="close",
        phase="exit",
        current_rank_row_present=current_row,
        current_rank_factor_snapshot_id=None,
        membership_change_reason=case,
    )
    _persist(store, event)

    with store.get_session() as session:
        leg = session.get(app_models.ModelRebalanceLeg, (event.rebalance_id, 0))
        assert leg is not None
        expected_signal_snapshot = event.legs[0].signal.model_dump(mode="json")
        assert leg.signal_snapshot == expected_signal_snapshot
        assert leg.signal_snapshot_sha256 == canonical_json_hash(expected_signal_snapshot)
        assert leg.current_rank_row_present is current_row
        assert leg.current_rank_factor_snapshot_id is None
        if expected_prior:
            assert leg.prior_model_rebalance_id == initial.rebalance_id
            assert leg.prior_model_leg_id == initial.legs[0].leg_id
        else:
            assert leg.prior_model_rebalance_id is None
            assert leg.prior_model_leg_id is None


def test_a_later_close_invalidates_a_second_membership_exit() -> None:
    store = _store()
    _seed_initial_target(store)
    close_session = date(2026, 1, 9)
    close_cutoff = datetime(2026, 1, 9, 21, 0, tzinfo=UTC)
    factor_id = _seed_factor(
        store,
        label="complete-close",
        effective_session=close_session,
        cutoff=close_cutoff,
    )
    close_rank_id = _seed_rank(
        store,
        label="complete-close",
        effective_session=close_session,
        cutoff=close_cutoff,
        factor_id=factor_id,
        exclusion_reason="absolute_trend_failed",
    )
    close_event = _event(
        store=store,
        label="complete-close",
        effective_session=close_session,
        cutoff=close_cutoff,
        rank_id=close_rank_id,
        factor_id=factor_id,
        action="close",
        phase="exit",
        current_rank_row_present=True,
        current_rank_factor_snapshot_id=factor_id,
        membership_change_reason="absolute_trend_failed",
    )
    _persist(store, close_event)

    later_session = date(2026, 1, 16)
    later_cutoff = datetime(2026, 1, 16, 21, 0, tzinfo=UTC)
    later_rank_id = _seed_rank(
        store,
        label="second-exit",
        effective_session=later_session,
        cutoff=later_cutoff,
        factor_id=None,
        omit_row=True,
    )
    later_event = _event(
        store=store,
        label="second-exit",
        effective_session=later_session,
        cutoff=later_cutoff,
        rank_id=later_rank_id,
        factor_id=factor_id,
        action="close",
        phase="exit",
        current_rank_row_present=False,
        current_rank_factor_snapshot_id=None,
        membership_change_reason="left_effective_universe",
    )

    with pytest.raises(ValueError, match="immediately previous model still targeted"):
        store.classify_model_rebalance(later_event)


def test_model_stream_rejects_out_of_order_effective_sessions() -> None:
    store = _store()
    _seed_initial_target(store)
    effective_session = date(2025, 12, 26)
    cutoff = datetime(2025, 12, 26, 21, 0, tzinfo=UTC)
    factor_id = _seed_factor(
        store,
        label="out-of-order",
        effective_session=effective_session,
        cutoff=cutoff,
    )
    rank_id = _seed_rank(
        store,
        label="out-of-order",
        effective_session=effective_session,
        cutoff=cutoff,
        factor_id=factor_id,
    )
    event = _event(
        store=store,
        label="out-of-order",
        effective_session=effective_session,
        cutoff=cutoff,
        rank_id=rank_id,
        factor_id=factor_id,
        action="long",
        phase="entry",
        current_rank_row_present=True,
        current_rank_factor_snapshot_id=factor_id,
    )

    with pytest.raises(ValueError, match="out of order or diverges"):
        store.classify_model_rebalance(event)


def test_missing_canonical_leg_rolls_back_the_model_header() -> None:
    store = _store()
    effective_session = date(2026, 2, 6)
    cutoff = datetime(2026, 2, 6, 21, 0, tzinfo=UTC)
    factor_id = _seed_factor(
        store,
        label="missing-canonical",
        effective_session=effective_session,
        cutoff=cutoff,
    )
    rank_id = _seed_rank(
        store,
        label="missing-canonical",
        effective_session=effective_session,
        cutoff=cutoff,
        factor_id=factor_id,
    )
    event = _event(
        store=store,
        label="missing-canonical",
        effective_session=effective_session,
        cutoff=cutoff,
        rank_id=rank_id,
        factor_id=factor_id,
        action="long",
        phase="entry",
        current_rank_row_present=True,
        current_rank_factor_snapshot_id=factor_id,
    )

    with pytest.raises(ValueError, match=r"Canonical signal .* is missing"):
        _persist(store, event, persist_signal=False)

    with store.get_session() as session:
        assert session.get(app_models.ModelRebalance, event.rebalance_id) is None
        assert (
            session.query(app_models.ModelRebalanceLeg)
            .filter_by(rebalance_id=event.rebalance_id)
            .count()
            == 0
        )


def test_model_batch_requires_its_completed_panel_decision() -> None:
    store = _store()
    effective_session = date(2026, 2, 13)
    cutoff = datetime(2026, 2, 13, 21, 0, tzinfo=UTC)
    factor_id = _seed_factor(
        store,
        label="missing-panel",
        effective_session=effective_session,
        cutoff=cutoff,
    )
    rank_id = _seed_rank(
        store,
        label="missing-panel",
        effective_session=effective_session,
        cutoff=cutoff,
        factor_id=factor_id,
    )
    event = _event(
        store=store,
        label="missing-panel",
        effective_session=effective_session,
        cutoff=cutoff,
        rank_id=rank_id,
        factor_id=factor_id,
        action="long",
        phase="entry",
        current_rank_row_present=True,
        current_rank_factor_snapshot_id=factor_id,
        seed_panel=False,
    )

    with pytest.raises(ValueError, match="completed synchronized-panel"):
        store.classify_model_rebalance(event)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("confidence", 0.4),
        ("expected_return", 0.99),
        ("predicted_risk", 0.9),
        ("horizon", "intraday"),
        ("horizon_days", 1.0),
        ("entry_price", 999.0),
        ("size_hint", 0.9),
        ("sector", "Utilities"),
        ("industry", "Electric Utilities"),
        ("run_id", "altered-run"),
    ],
)
def test_model_batch_requires_the_exact_completed_panel_signal(
    field_name: str,
    value: object,
) -> None:
    store = _store()
    effective_session = date(2026, 2, 20)
    cutoff = datetime(2026, 2, 20, 21, 0, tzinfo=UTC)
    factor_id = _seed_factor(
        store,
        label="exact-signal",
        effective_session=effective_session,
        cutoff=cutoff,
    )
    rank_id = _seed_rank(
        store,
        label="exact-signal",
        effective_session=effective_session,
        cutoff=cutoff,
        factor_id=factor_id,
    )
    persisted = _event(
        store=store,
        label="exact-signal",
        effective_session=effective_session,
        cutoff=cutoff,
        rank_id=rank_id,
        factor_id=factor_id,
        action="long",
        phase="entry",
        current_rank_row_present=True,
        current_rank_factor_snapshot_id=factor_id,
    )
    altered = _rehashed_event_with_signal_update(
        persisted,
        field_name=field_name,
        value=value,
    )

    with pytest.raises(ValueError, match="exact completed synchronized-panel"):
        store.classify_model_rebalance(altered)


@pytest.mark.parametrize(
    ("event_overrides", "message"),
    [
        ({"action": "hold", "phase": "hold"}, "phase disagrees"),
        ({"allocation_hint": 0.04}, "allocation differs"),
        ({"composite_score": 0.7}, "composite score differs"),
        ({"symbol": "WRONG"}, "catalogue identity"),
    ],
)
def test_model_leg_must_match_the_exact_rank_and_catalogue_decision(
    event_overrides: dict[str, object],
    message: str,
) -> None:
    store = _store()
    effective_session = date(2026, 3, 6)
    cutoff = datetime(2026, 3, 6, 21, 0, tzinfo=UTC)
    factor_id = _seed_factor(
        store,
        label="semantic-proof",
        effective_session=effective_session,
        cutoff=cutoff,
    )
    rank_id = _seed_rank(
        store,
        label="semantic-proof",
        effective_session=effective_session,
        cutoff=cutoff,
        factor_id=factor_id,
    )
    event_parameters: dict[str, object] = {
        "store": store,
        "label": "semantic-proof",
        "effective_session": effective_session,
        "cutoff": cutoff,
        "rank_id": rank_id,
        "factor_id": factor_id,
        "action": "long",
        "phase": "entry",
        "current_rank_row_present": True,
        "current_rank_factor_snapshot_id": factor_id,
    }
    event_parameters.update(event_overrides)
    event = _event(**event_parameters)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=message):
        store.classify_model_rebalance(event)


def _postgres_store() -> tuple[Engine, AppScoreStore, str]:
    raw_url = os.getenv("DATABASE_URL")
    if not raw_url:
        pytest.skip("DATABASE_URL is required for PostgreSQL rebalance acceptance")
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql") or "async" in url.drivername:
        pytest.skip("PostgreSQL rebalance acceptance requires a synchronous PostgreSQL URL")

    admin_engine = create_engine(raw_url, future=True)
    schema = f"scoring_rebalance_{uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        for table in sorted(
            app_models.Base.metadata.tables.values(),
            key=lambda candidate: candidate.name,
        ):
            connection.exec_driver_sql(
                f'CREATE TABLE "{schema}"."{table.name}" (LIKE public."{table.name}" INCLUDING ALL)'
            )
    runtime_url = url.update_query_dict(
        {"options": f"-csearch_path={schema},public"}
    ).render_as_string(hide_password=False)
    return admin_engine, _store(runtime_url), schema


def _seed_postgres_binding(store: AppScoreStore) -> tuple[str, int, int]:
    user_id = _OWNER_ID
    account_id = 8_803
    binding_id = 8_804
    with store.get_session() as session:
        session.add(
            app_models.Broker(
                broker_id=8_805,
                code="paper",
                name="PostgreSQL test paper broker",
                capabilities={},
            )
        )
        session.flush()
        session.add(
            app_models.LinkedBrokerAccount(
                account_id=account_id,
                user_id=user_id,
                broker_id=8_805,
                environment="paper",
                display_name="PostgreSQL rebalance account",
                base_ccy="USD",
                status="connected",
                paper_initial_equity=Decimal("100000"),
                paper_initial_cash=Decimal("100000"),
            )
        )
        session.flush()
        session.add(
            app_models.UserStrategyBinding(
                binding_id=binding_id,
                user_id=user_id,
                strategy_id=_STRATEGY_ID,
                broker_account_id=account_id,
                asset_score_threshold=Decimal("0"),
                execution_modes_allowed=["spot"],
                preferred_mode="spot",
                mode_selection_policy="fixed",
                asset_classes_allowed=["equity"],
                instruments_allowed=[str(_INSTRUMENT_ID)],
                max_position_pct=Decimal("0.10"),
                max_daily_loss_pct=Decimal("0.05"),
                max_open_positions=30,
                allowed_brokers=["paper"],
                is_active=True,
                autopilot=True,
                entries_enabled=True,
                exits_enabled=True,
            )
        )
        session.commit()
    return user_id, account_id, binding_id


def _provider_contexts(
    *,
    user_id: str,
    account_id: int,
    binding_id: int,
    configured_account_id: int | None = None,
) -> DispatchProviderContexts:
    return DispatchProviderContexts(
        profiles={
            user_id: {
                "accounts": {
                    str(account_id): {
                        "account_id": account_id,
                        "broker": "paper",
                        "environment": "paper",
                        "status": "connected",
                    }
                }
            }
        },
        strategy_configs={
            (user_id, binding_id, account_id): {
                "user_id": user_id,
                "binding_id": binding_id,
                "strategy_id": _STRATEGY_ID,
                "broker_account_id": configured_account_id or account_id,
                "broker": "paper",
                "execution_mode": "spot",
                "execution_modes_allowed": ["spot"],
                "mode_selection_policy": "fixed",
                "allowed_brokers": ["paper"],
                "autopilot": True,
                "entries_enabled": True,
                "exits_enabled": True,
            }
        },
    )


@pytest.mark.integration
def test_postgres_batch_rollback_replay_tenant_fence_and_outbox_ordering() -> None:
    admin_engine, store, schema = _postgres_store()
    user_id, account_id, binding_id = _seed_postgres_binding(store)
    effective_session = date(2026, 4, 2)
    cutoff = datetime(2026, 4, 2, 20, 0, tzinfo=UTC)
    factor_id = _seed_factor(
        store,
        label="postgres-atomic",
        effective_session=effective_session,
        cutoff=cutoff,
    )
    rank_id = _seed_rank(
        store,
        label="postgres-atomic",
        effective_session=effective_session,
        cutoff=cutoff,
        factor_id=factor_id,
    )
    event = _event(
        store=store,
        label="postgres-atomic",
        effective_session=effective_session,
        cutoff=cutoff,
        rank_id=rank_id,
        factor_id=factor_id,
        action="long",
        phase="entry",
        current_rank_row_present=True,
        current_rank_factor_snapshot_id=factor_id,
    )
    engine = ScoreEngine(store)
    engine.pipeline.set_market_context(
        "LINEAGE",
        MarketContext(
            regime=MarketRegime.BULL_QUIET,
            as_of=cutoff,
            realized_vol_20d=0.01,
        ),
    )
    service = RebalanceScoringService(
        engine=engine,
        dispatcher=ExecutionDispatcher(
            "http://execution-engine",
            store=store,
            runtime_mode="paper",
        ),
    )

    try:
        with pytest.raises(ValueError, match="conflicts with configured account"):
            service.ingest(
                event,
                provider_contexts=_provider_contexts(
                    user_id=user_id,
                    account_id=account_id,
                    binding_id=binding_id,
                    configured_account_id=account_id + 1,
                ),
            )
        with store.get_session() as session:
            assert session.scalar(select(func.count()).select_from(app_models.CanonicalSignal)) == 0
            assert session.scalar(select(func.count()).select_from(app_models.ModelRebalance)) == 0
            assert (
                session.scalar(select(func.count()).select_from(app_models.AccountRebalancePlan))
                == 0
            )
            assert session.scalar(select(func.count()).select_from(app_models.OutboxEvent)) == 0

        contexts = _provider_contexts(
            user_id=user_id,
            account_id=account_id,
            binding_id=binding_id,
        )
        result = service.ingest(event, provider_contexts=contexts)
        assert result.replayed is False
        assert len(result.plan_ids) == 1
        with store.get_session() as session:
            outbox_rows = session.scalars(
                select(app_models.OutboxEvent).order_by(app_models.OutboxEvent.created_at)
            ).all()
            command = next(
                row for row in outbox_rows if row.topic == "execution.rebalance.commands"
            )
            assert command.ordering_key == f"{user_id}:{account_id}"
            assert command.available_at == event.execute_not_before
            outbox_count = len(outbox_rows)

        replay = service.ingest(event, provider_contexts=contexts)
        assert replay.replayed is True
        assert replay.plan_ids == result.plan_ids
        with store.get_session() as session:
            assert (
                session.scalar(select(func.count()).select_from(app_models.OutboxEvent))
                == outbox_count
            )
    finally:
        store._engine.dispose()
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin_engine.dispose()
