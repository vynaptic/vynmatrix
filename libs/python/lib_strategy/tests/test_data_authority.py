"""Provider-neutral evidence-authority contract tests."""

from __future__ import annotations

import pytest

from lib_strategy.data_authority import (
    DataAuthorityError,
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)


def _historical_policy() -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="sp500-public-history-v1",
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        rules=(
            ProviderAuthorityRule(
                provider="eodhd",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("historical_validation_only",),
            ),
            ProviderAuthorityRule(
                provider="sec",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("public",),
            ),
        ),
    )


def test_policy_digest_is_canonical_and_round_trips() -> None:
    policy = _historical_policy()

    restored = ProviderAuthorityPolicy.from_payload(policy.to_payload())

    assert restored == policy
    assert restored.digest == policy.digest


def test_legacy_shared_policy_payload_remains_readable() -> None:
    payload = _historical_policy().to_payload()
    payload["schema"] = "provider-authority-policy-v1"
    payload.pop("entitlement_owner_user_id")

    restored = ProviderAuthorityPolicy.from_payload(payload)

    assert restored.entitlement_owner_user_id is None


def test_policy_codec_rejects_noncanonical_extra_material() -> None:
    payload = _historical_policy().to_payload()
    payload["provider_override"] = "sec"

    with pytest.raises(DataAuthorityError, match="keys must be exactly"):
        ProviderAuthorityPolicy.from_payload(payload)


def test_policy_requires_canonical_rule_and_entitlement_ordering() -> None:
    with pytest.raises(DataAuthorityError, match="canonical provider ordering"):
        ProviderAuthorityPolicy(
            policy_version="v1",
            data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
            rules=tuple(reversed(_historical_policy().rules)),
        )

    with pytest.raises(DataAuthorityError, match="canonically ordered"):
        ProviderAuthorityRule(
            provider="sec",
            decision=ProviderAuthorityDecision.ALLOW,
            entitlement_scopes=("public-b", "public-a"),
        )


def test_authority_is_default_deny_and_entitlement_exact() -> None:
    policy = _historical_policy()

    policy.require_authorized(
        provider="eodhd",
        entitlement_scope="historical_validation_only",
    )

    with pytest.raises(DataAuthorityError, match="not declared"):
        policy.require_authorized(provider="unknown", entitlement_scope="public")
    with pytest.raises(DataAuthorityError, match="is not authorized"):
        policy.require_authorized(provider="eodhd", entitlement_scope="paper")


def test_forward_policy_can_explicitly_deny_eodhd_without_global_ban() -> None:
    policy = ProviderAuthorityPolicy(
        policy_version="sp500-public-forward-v1",
        data_use_scope=DataUseScope.PAPER_FORWARD,
        rules=(
            ProviderAuthorityRule(
                provider="eodhd",
                decision=ProviderAuthorityDecision.DENY,
            ),
            ProviderAuthorityRule(
                provider="sec",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("public",),
            ),
        ),
    )

    policy.require_authorized(provider="sec", entitlement_scope="public")
    with pytest.raises(DataAuthorityError, match="is denied"):
        policy.require_authorized(
            provider="eodhd",
            entitlement_scope="historical_validation_only",
        )


def test_personal_entitlement_is_owner_bound_for_research_and_paper_only() -> None:
    policy = ProviderAuthorityPolicy(
        policy_version="personal-history-v1",
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        rules=(
            ProviderAuthorityRule(
                provider="eodhd",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("personal-research",),
            ),
        ),
        entitlement_owner_user_id="research-owner",
    )

    policy.require_authorized(
        provider="eodhd",
        entitlement_scope="personal-research",
        entitlement_owner_user_id="research-owner",
    )
    with pytest.raises(DataAuthorityError, match="personal entitlement owner"):
        policy.require_authorized(
            provider="eodhd",
            entitlement_scope="personal-research",
            entitlement_owner_user_id="another-user",
        )
    with pytest.raises(DataAuthorityError, match="personal entitlement owner"):
        policy.require_authorized(
            provider="eodhd",
            entitlement_scope="personal-research",
        )
    paper = ProviderAuthorityPolicy(
        policy_version="personal-paper-v1",
        data_use_scope=DataUseScope.PAPER_FORWARD,
        rules=policy.rules,
        entitlement_owner_user_id="research-owner",
    )
    paper.require_authorized(
        provider="eodhd",
        entitlement_scope="personal-research",
        entitlement_owner_user_id="research-owner",
    )
    with pytest.raises(DataAuthorityError, match="cannot authorize live-forward"):
        ProviderAuthorityPolicy(
            policy_version="personal-live-v1",
            data_use_scope=DataUseScope.LIVE_FORWARD,
            rules=policy.rules,
            entitlement_owner_user_id="research-owner",
        )


def test_mixed_public_and_personal_rules_preserve_exact_owner_semantics() -> None:
    policy = ProviderAuthorityPolicy(
        policy_version="mixed-history-v1",
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        rules=(
            ProviderAuthorityRule(
                provider="eodhd",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("personal-research",),
                entitlement_owner_user_id="research-owner",
            ),
            ProviderAuthorityRule(
                provider="official-exchange",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("public",),
            ),
            ProviderAuthorityRule(
                provider="sec",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("public",),
            ),
        ),
    )

    assert policy.to_payload()["schema"] == "provider-authority-policy-v3"
    assert policy.effective_entitlement_owner_user_id == "research-owner"
    restored = ProviderAuthorityPolicy.from_payload(policy.to_payload())
    assert restored == policy
    assert restored.digest == policy.digest
    restored.require_authorized(
        provider="eodhd",
        entitlement_scope="personal-research",
        entitlement_owner_user_id="research-owner",
    )
    restored.require_authorized(provider="sec", entitlement_scope="public")
    restored.require_authorized(
        provider="official-exchange",
        entitlement_scope="public",
    )
    with pytest.raises(DataAuthorityError, match="personal entitlement owner"):
        restored.require_authorized(
            provider="eodhd",
            entitlement_scope="personal-research",
        )
    with pytest.raises(DataAuthorityError, match="personal entitlement owner"):
        restored.require_authorized(
            provider="sec",
            entitlement_scope="public",
            entitlement_owner_user_id="research-owner",
        )


def test_policy_rejects_ambiguous_or_live_per_rule_personal_owners() -> None:
    rules = (
        ProviderAuthorityRule(
            provider="eodhd",
            decision=ProviderAuthorityDecision.ALLOW,
            entitlement_scopes=("personal-research",),
            entitlement_owner_user_id="owner-a",
        ),
        ProviderAuthorityRule(
            provider="licensed-news",
            decision=ProviderAuthorityDecision.ALLOW,
            entitlement_scopes=("personal-research",),
            entitlement_owner_user_id="owner-b",
        ),
    )
    with pytest.raises(DataAuthorityError, match="multiple personal owners"):
        ProviderAuthorityPolicy(
            policy_version="ambiguous-v1",
            data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
            rules=rules,
        )
    with pytest.raises(DataAuthorityError, match="cannot authorize live-forward"):
        ProviderAuthorityPolicy(
            policy_version="live-v1",
            data_use_scope=DataUseScope.LIVE_FORWARD,
            rules=(rules[0],),
        )
