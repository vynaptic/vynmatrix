"""Contract tests for synchronized point-in-time panel readiness."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.panels import (
    EffectivePanelMember,
    OfficialSessionCutoff,
    PanelContractError,
    PanelExclusion,
    PanelIssueCode,
    PanelObservationRef,
    PanelReadyInput,
    SessionAuthority,
    evaluate_panel_readiness,
    panel_ready_input_from_payload,
    panel_ready_input_to_payload,
)

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_OPEN = datetime(2025, 12, 31, 14, 30, tzinfo=UTC)
_CLOSE = datetime(2025, 12, 31, 21, 0, tzinfo=UTC)


def _session() -> OfficialSessionCutoff:
    return OfficialSessionCutoff(
        mic="XNYS",
        session_date=date(2025, 12, 31),
        opens_at=_OPEN,
        closes_at=_CLOSE,
        authority=SessionAuthority.OFFICIAL_EXCHANGE,
        source_identity="nyse-market-hours-2025",
        content_sha256=_DIGEST_A,
    )


def _execution_session() -> OfficialSessionCutoff:
    return OfficialSessionCutoff(
        mic="XNYS",
        session_date=date(2026, 1, 2),
        opens_at=datetime(2026, 1, 2, 14, 30, tzinfo=UTC),
        closes_at=datetime(2026, 1, 2, 21, 0, tzinfo=UTC),
        authority=SessionAuthority.OFFICIAL_EXCHANGE,
        source_identity="nyse-market-hours-2026",
        content_sha256=_DIGEST_C,
    )


def _policy(
    scope: DataUseScope = DataUseScope.HISTORICAL_VALIDATION,
    *,
    version: str = "policy-v1",
) -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version=version,
        data_use_scope=scope,
        rules=(
            ProviderAuthorityRule(
                provider="sec",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("public-sec-api",),
            ),
        ),
    )


def _observation(security_id: str, *, available_at: datetime = _CLOSE) -> PanelObservationRef:
    return PanelObservationRef(
        security_id=security_id,
        observation_id=f"snapshot:{security_id}:2025-12-31",
        observed_at=_CLOSE,
        available_at=available_at,
        content_revision=1,
        content_sha256=_DIGEST_B,
    )


def _candidate(
    *,
    observations: tuple[PanelObservationRef, ...],
    scope: DataUseScope = DataUseScope.HISTORICAL_VALIDATION,
    policy_version: str = "policy-v1",
    cutoff: datetime = _CLOSE,
) -> PanelReadyInput:
    policy = _policy(scope, version=policy_version)
    return PanelReadyInput(
        cutoff=cutoff,
        session=_session(),
        execution_session=_execution_session(),
        data_use_scope=scope,
        provider_authority_policy=policy,
        provider_authority_sha256=policy.digest,
        membership_sha256=_DIGEST_B,
        factor_snapshot_sha256=_DIGEST_C,
        members=(
            EffectivePanelMember("figi:BBG000B9XRY4", "issuer:AAPL", 101, "AAPL"),
            EffectivePanelMember("figi:BBG000BPH459", "issuer:MSFT", 202, "MSFT"),
        ),
        observations=observations,
    )


def test_complete_panel_is_ordering_invariant() -> None:
    aapl = _observation("figi:BBG000B9XRY4")
    msft = _observation("figi:BBG000BPH459")

    first = evaluate_panel_readiness(_candidate(observations=(aapl, msft)))
    second = evaluate_panel_readiness(_candidate(observations=(msft, aapl)))

    assert first.complete is True
    assert first.required_count == 2
    assert first.observed_count == 2
    assert first.panel_sha256 == second.panel_sha256


def test_after_close_knowledge_and_observations_are_actionable_before_next_open() -> None:
    knowledge_cutoff = _CLOSE + timedelta(hours=2)
    observations = (
        _observation("figi:BBG000B9XRY4", available_at=knowledge_cutoff),
        _observation("figi:BBG000BPH459", available_at=knowledge_cutoff),
    )

    candidate = _candidate(observations=observations, cutoff=knowledge_cutoff)
    restored = panel_ready_input_from_payload(panel_ready_input_to_payload(candidate))
    readiness = evaluate_panel_readiness(restored)

    assert readiness.complete is True
    assert restored == candidate


def test_pre_close_and_at_or_after_execution_open_cutoffs_fail_contract() -> None:
    observations = (
        _observation("figi:BBG000B9XRY4"),
        _observation("figi:BBG000BPH459"),
    )

    with pytest.raises(PanelContractError, match="cannot precede the completed session close"):
        _candidate(observations=observations, cutoff=_CLOSE - timedelta(microseconds=1))
    with pytest.raises(PanelContractError, match="must precede the execution-session open"):
        _candidate(observations=observations, cutoff=_execution_session().opens_at)
    with pytest.raises(PanelContractError, match="must precede the execution-session open"):
        _candidate(
            observations=observations,
            cutoff=_execution_session().opens_at + timedelta(microseconds=1),
        )


def test_historical_close_cutoff_remains_valid() -> None:
    observations = (
        _observation("figi:BBG000B9XRY4"),
        _observation("figi:BBG000BPH459"),
    )

    assert evaluate_panel_readiness(_candidate(observations=observations)).complete is True


def test_panel_identity_binds_data_use_scope_and_provider_authority() -> None:
    candidate = _candidate(
        observations=(
            _observation("figi:BBG000B9XRY4"),
            _observation("figi:BBG000BPH459"),
        )
    )

    observations = candidate.observations
    paper = _candidate(observations=observations, scope=DataUseScope.PAPER_FORWARD)
    different_authority = _candidate(
        observations=observations,
        policy_version="policy-v2",
    )

    assert candidate.canonical_digest() != paper.canonical_digest()
    assert candidate.canonical_digest() != different_authority.canonical_digest()


def test_missing_late_and_ambiguous_members_fail_closed() -> None:
    aapl = _observation("figi:BBG000B9XRY4", available_at=_CLOSE + timedelta(seconds=1))
    candidate = _candidate(observations=(aapl,))
    candidate = PanelReadyInput(
        cutoff=candidate.cutoff,
        session=candidate.session,
        execution_session=candidate.execution_session,
        data_use_scope=candidate.data_use_scope,
        provider_authority_policy=candidate.provider_authority_policy,
        provider_authority_sha256=candidate.provider_authority_sha256,
        membership_sha256=candidate.membership_sha256,
        factor_snapshot_sha256=candidate.factor_snapshot_sha256,
        members=candidate.members,
        observations=candidate.observations,
        exclusions=(
            PanelExclusion(
                security_id="figi:BBG000B9XRY4",
                reason_code="unresolved_corporate_action",
                disposition_identity="disposition:aapl:2025-12-31",
                content_sha256=_DIGEST_C,
            ),
        ),
    )

    readiness = evaluate_panel_readiness(candidate)
    codes = {(issue.code, issue.security_id) for issue in readiness.issues}
    assert readiness.complete is False
    assert (PanelIssueCode.LATE_OBSERVATION, "figi:BBG000B9XRY4") in codes
    assert (
        PanelIssueCode.MEMBER_BOTH_OBSERVED_AND_EXCLUDED,
        "figi:BBG000B9XRY4",
    ) in codes
    assert (PanelIssueCode.MISSING_MEMBER, "figi:BBG000BPH459") in codes


def test_explicit_exclusion_completes_member_disposition() -> None:
    candidate = _candidate(observations=(_observation("figi:BBG000B9XRY4"),))
    candidate = PanelReadyInput(
        cutoff=candidate.cutoff,
        session=candidate.session,
        execution_session=candidate.execution_session,
        data_use_scope=candidate.data_use_scope,
        provider_authority_policy=candidate.provider_authority_policy,
        provider_authority_sha256=candidate.provider_authority_sha256,
        membership_sha256=candidate.membership_sha256,
        factor_snapshot_sha256=candidate.factor_snapshot_sha256,
        members=candidate.members,
        observations=candidate.observations,
        exclusions=(
            PanelExclusion(
                security_id="figi:BBG000BPH459",
                reason_code="provider_unavailable",
                disposition_identity="disposition:msft:2025-12-31",
                content_sha256=_DIGEST_C,
            ),
        ),
    )

    readiness = evaluate_panel_readiness(candidate)
    assert readiness.complete is True
    assert readiness.observed_count == 1
    assert readiness.excluded_count == 1


def test_naive_timestamps_and_invalid_digests_are_rejected() -> None:
    with pytest.raises(PanelContractError, match="timezone-aware"):
        OfficialSessionCutoff(
            mic="XNYS",
            session_date=date(2025, 12, 31),
            opens_at=_OPEN.replace(tzinfo=None),
            closes_at=_CLOSE,
            authority=SessionAuthority.OFFICIAL_EXCHANGE,
            source_identity="nyse-market-hours-2025",
            content_sha256=_DIGEST_A,
        )
    policy = _policy()
    with pytest.raises(PanelContractError, match="SHA-256"):
        PanelReadyInput(
            cutoff=_CLOSE,
            session=_session(),
            execution_session=_execution_session(),
            data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
            provider_authority_policy=policy,
            provider_authority_sha256=policy.digest,
            membership_sha256="not-a-digest",
            factor_snapshot_sha256=_DIGEST_C,
            members=(),
            observations=(),
        )
