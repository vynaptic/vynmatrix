"""Typed model-rebalance projection tests for rank-incomplete panel exits."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from indicator_runner.model_rebalance_projection import build_model_rebalance_event
from indicator_runner.runtime_journal import StrategyRuntimeIdentity
from lib_application.db.models import StrategyPanelDecision
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
    PanelObservationRef,
    PanelReadyInput,
    SessionAuthority,
)
from lib_strategy.signals.signal import Signal, SignalAction

_CUTOFF = datetime(2025, 2, 3, 21, tzinfo=UTC)
_EXECUTION_OPEN = datetime(2025, 2, 4, 14, 30, tzinfo=UTC)
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_FACTOR_SNAPSHOT_ID = "d" * 64


def _policy() -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="test-policy-v1",
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        rules=(
            ProviderAuthorityRule(
                provider="test-provider",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("test-history",),
            ),
        ),
    )


def _panel(*, panel_excluded: bool) -> PanelReadyInput:
    policy = _policy()
    member = EffectivePanelMember("FIGI-AAA", "ISSUER-AAA", 1, "AAA")
    return PanelReadyInput(
        cutoff=_CUTOFF,
        session=OfficialSessionCutoff(
            mic="XNYS",
            session_date=date(2025, 2, 3),
            opens_at=datetime(2025, 2, 3, 14, 30, tzinfo=UTC),
            closes_at=_CUTOFF,
            authority=SessionAuthority.RESEARCH_LIBRARY,
            source_identity="research-calendar:test:XNYS",
            content_sha256=_DIGEST_A,
        ),
        execution_session=OfficialSessionCutoff(
            mic="XNYS",
            session_date=date(2025, 2, 4),
            opens_at=_EXECUTION_OPEN,
            closes_at=_EXECUTION_OPEN + timedelta(hours=6, minutes=30),
            authority=SessionAuthority.RESEARCH_LIBRARY,
            source_identity="research-calendar:test:XNYS",
            content_sha256=_DIGEST_B,
        ),
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        provider_authority_policy=policy,
        provider_authority_sha256=policy.digest,
        membership_sha256=_DIGEST_B,
        factor_snapshot_sha256=_DIGEST_C,
        members=(member,),
        observations=(
            ()
            if panel_excluded
            else (
                PanelObservationRef(
                    security_id=member.security_id,
                    observation_id="factor-panel:AAA:2025-02-03",
                    observed_at=_CUTOFF,
                    available_at=_CUTOFF,
                    content_revision=1,
                    content_sha256=_DIGEST_C,
                ),
            )
        ),
        exclusions=(
            (
                PanelExclusion(
                    security_id=member.security_id,
                    reason_code="source_factor_disposition",
                    disposition_identity="factor-panel:AAA:2025-02-03",
                    content_sha256=_DIGEST_C,
                ),
            )
            if panel_excluded
            else ()
        ),
    )


def _audit(*, exclusion_reason: str) -> PanelEvaluationAudit:
    snapshot = CrossSectionalSnapshot(
        cutoff=_CUTOFF,
        calculation_version="panel-v1",
        minimum_peer_count=2,
        winsorize_limit=3.0,
        fallback_policy=PeerFallbackPolicy.INELIGIBLE,
        factor_specs=(FactorSpec(name="quality", weight=1.0),),
        contributions=(),
        ranks=(),
        missing_decisions=(
            MissingFactorDecision(
                entity_id="FIGI-AAA",
                symbol="AAA",
                factor_name="quality",
                reason=MissingFactorReason.SOURCE_MISSING,
                detail=exclusion_reason,
            ),
        ),
    )
    return PanelEvaluationAudit(
        rank_snapshot=snapshot,
        rows=(
            PanelEvaluationRow(
                entity_id="FIGI-AAA",
                symbol="AAA",
                instrument_id=1,
                factor_snapshot_id=None,
                rank_complete=False,
                strategy_eligible=False,
                decision=PanelAuditDecision.EXCLUDED,
                incumbent=True,
                rank=None,
                composite_score=None,
                target_allocation_hint=None,
                exclusion_reason=exclusion_reason,
            ),
        ),
        configuration_sha256=_DIGEST_A,
        peer_taxonomy_version="taxonomy-v1",
        replayed=False,
        intentional_cash_slots=0,
        required_factor_versions=(("quality", "quality-v1"),),
    )


def _signal(*, membership_change_reason: str, rank_snapshot_id: str) -> Signal:
    strategy_input_sha256 = _DIGEST_B
    return Signal(
        strategy_id="sp500-rotation",
        strategy_type="indicator",
        symbol="AAA",
        action=SignalAction.CLOSE,
        confidence=1.0,
        timestamp=_CUTOFF,
        size_hint=0.0,
        asset_class="equity",
        instrument_id=1,
        strategy_version="1.0.0",
        source="backtest",
        external_signal_id=f"exit:{membership_change_reason}",
        metadata={
            "configuration_sha256": _DIGEST_A,
            "strategy_input_sha256": strategy_input_sha256,
            "rank_snapshot_sha256": rank_snapshot_id,
            "provider_authority_sha256": _policy().digest,
            "data_use_scope": DataUseScope.HISTORICAL_VALIDATION.value,
            "execution_session_sha256": _DIGEST_B,
            "execute_not_before": _EXECUTION_OPEN.isoformat(),
            "model_rebalance_expected_leg_count": 1,
            "intentional_cash_slots": 0,
            "model_rebalance_sequence": 0,
            "model_rebalance_phase": "exit",
            "factor_snapshot_id": _FACTOR_SNAPSHOT_ID,
            "current_rank_row_present": True,
            "current_rank_factor_snapshot_id": None,
            "membership_change_reason": membership_change_reason,
            "rank": None,
        },
    )


@pytest.mark.parametrize(
    ("membership_change_reason", "panel_excluded"),
    [
        ("factor_evidence_incomplete", False),
        ("panel_excluded", True),
    ],
)
def test_rank_incomplete_exit_projects_exact_membership_disposition(
    membership_change_reason: str,
    panel_excluded: bool,
) -> None:
    panel = _panel(panel_excluded=panel_excluded)
    audit = _audit(exclusion_reason=membership_change_reason)
    identity = StrategyRuntimeIdentity(
        worker_id="panel-worker",
        strategy_id="sp500-rotation",
        strategy_version="1.0.0",
        symbols=(),
        source="historical-manifest",
        timeframe="1d",
        consolidation_minutes=0,
        config_fingerprint=_DIGEST_C,
    )
    panel_row = cast(
        StrategyPanelDecision,
        SimpleNamespace(
            decision_key=_DIGEST_A,
            strategy_input_sha256=_DIGEST_B,
        ),
    )
    signal = _signal(
        membership_change_reason=membership_change_reason,
        rank_snapshot_id=audit.rank_snapshot.content_digest,
    )

    event = build_model_rebalance_event(
        identity=identity,
        panel_row=panel_row,
        panel=panel,
        audit=audit,
        signals=(signal,),
        rank_snapshot_id=audit.rank_snapshot.content_digest,
    )

    assert event.expected_leg_count == 1
    assert event.legs[0].leg_id == signal.external_signal_id
    assert event.legs[0].current_rank_row_present is True
    assert event.legs[0].current_rank_factor_snapshot_id is None
    assert event.legs[0].membership_change_reason == membership_change_reason
