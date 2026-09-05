"""Immutable provider and entitlement authority for strategy evidence.

The contract is provider-neutral and default-deny. Acquisition code records
the actual provider and entitlement on source lineage; application services
apply this policy to those persisted facts before factor or rank identities
can be completed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, Self

from lib_common.hashing import canonical_json_hash

_POLICY_SCHEMA_V1 = "provider-authority-policy-v1"
_POLICY_SCHEMA_V2 = "provider-authority-policy-v2"
_POLICY_SCHEMA_V3 = "provider-authority-policy-v3"
_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_USER_ID_MAX_LENGTH = 50


class DataAuthorityError(ValueError):
    """Raised when source lineage is outside its declared authority."""


class DataUseScope(StrEnum):
    """How persisted evidence is authorized to be consumed."""

    HISTORICAL_VALIDATION = "historical_validation"
    PAPER_FORWARD = "paper_forward"
    LIVE_FORWARD = "live_forward"


class ProviderAuthorityDecision(StrEnum):
    """Explicit decision for one canonical provider identity."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ProviderAuthorityRule:
    """Provider decision and exact entitlement allow-list."""

    provider: str
    decision: ProviderAuthorityDecision
    entitlement_scopes: tuple[str, ...] = ()
    entitlement_owner_user_id: str | None = None

    def __post_init__(self) -> None:
        _require_provider(self.provider)
        if not isinstance(self.decision, ProviderAuthorityDecision):
            _invalid("provider authority decision must be a ProviderAuthorityDecision")
        if not isinstance(self.entitlement_scopes, tuple):
            _invalid("provider entitlement scopes must be an immutable tuple")
        for entitlement_scope in self.entitlement_scopes:
            _require_text(entitlement_scope, field_name="entitlement_scope")
        canonical_scopes = tuple(sorted(set(self.entitlement_scopes)))
        if self.entitlement_scopes != canonical_scopes:
            _invalid("provider entitlement scopes must be unique and canonically ordered")
        if self.decision is ProviderAuthorityDecision.ALLOW and not self.entitlement_scopes:
            _invalid("allowed providers require at least one exact entitlement scope")
        if self.decision is ProviderAuthorityDecision.DENY and self.entitlement_scopes:
            _invalid("denied providers cannot carry entitlement scopes")
        if self.entitlement_owner_user_id is not None:
            owner = _require_text(
                self.entitlement_owner_user_id,
                field_name="entitlement_owner_user_id",
            )
            if len(owner) > _USER_ID_MAX_LENGTH:
                _invalid("entitlement_owner_user_id exceeds the canonical user-id limit")
            if self.decision is ProviderAuthorityDecision.DENY:
                _invalid("denied providers cannot carry an entitlement owner")

    def to_payload(self, *, include_owner: bool = False) -> dict[str, object]:
        """Return deterministic JSON-safe policy material."""

        payload: dict[str, object] = {
            "decision": self.decision.value,
            "entitlement_scopes": list(self.entitlement_scopes),
            "provider": self.provider,
        }
        if include_owner:
            payload["entitlement_owner_user_id"] = self.entitlement_owner_user_id
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        """Restore and validate one canonical provider rule."""

        if not isinstance(payload, Mapping):
            _invalid("provider authority rule payload must be an object")
        keys = set(payload)
        owner_key_present = "entitlement_owner_user_id" in keys
        expected = {"decision", "entitlement_scopes", "provider"}
        if owner_key_present:
            expected.add("entitlement_owner_user_id")
        _require_exact_keys(payload, expected=expected, field_name="provider authority rule")
        raw_scopes = payload.get("entitlement_scopes")
        if not isinstance(raw_scopes, list) or not all(
            isinstance(item, str) for item in raw_scopes
        ):
            _invalid("provider authority entitlement_scopes must be an array of strings")
        try:
            decision = ProviderAuthorityDecision(
                _require_text(payload.get("decision"), field_name="decision")
            )
        except ValueError as exc:
            message = "provider authority decision is invalid"
            raise DataAuthorityError(message) from exc
        raw_owner = payload.get("entitlement_owner_user_id")
        if raw_owner is not None and not isinstance(raw_owner, str):
            _invalid("provider authority rule owner must be a string or null")
        return cls(
            provider=_require_text(payload.get("provider"), field_name="provider"),
            decision=decision,
            entitlement_scopes=tuple(raw_scopes),
            entitlement_owner_user_id=raw_owner,
        )


@dataclass(frozen=True, slots=True)
class ProviderAuthorityPolicy:
    """Canonical default-deny authority bound to one data-use scope."""

    policy_version: str
    data_use_scope: DataUseScope
    rules: tuple[ProviderAuthorityRule, ...]
    entitlement_owner_user_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.policy_version, field_name="policy_version")
        if not isinstance(self.data_use_scope, DataUseScope):
            _invalid("data_use_scope must be a DataUseScope")
        if not isinstance(self.rules, tuple):
            _invalid("provider authority rules must be an immutable tuple")
        if not self.rules:
            _invalid("provider authority policy must contain at least one rule")
        if not all(isinstance(rule, ProviderAuthorityRule) for rule in self.rules):
            _invalid("provider authority policy contains an invalid rule")
        providers = tuple(rule.provider for rule in self.rules)
        if len(providers) != len(set(providers)):
            _invalid("provider authority policy contains duplicate providers")
        canonical_rules = tuple(sorted(self.rules, key=lambda rule: rule.provider))
        if self.rules != canonical_rules:
            _invalid("provider authority rules must use canonical provider ordering")
        if not any(rule.decision is ProviderAuthorityDecision.ALLOW for rule in self.rules):
            _invalid("provider authority policy must allow at least one provider")
        if self.entitlement_owner_user_id is not None and any(
            rule.entitlement_owner_user_id is not None for rule in self.rules
        ):
            _invalid("provider authority cannot mix legacy policy and per-rule owners")
        if self.entitlement_owner_user_id is not None:
            owner = _require_text(
                self.entitlement_owner_user_id,
                field_name="entitlement_owner_user_id",
            )
            if len(owner) > _USER_ID_MAX_LENGTH:
                _invalid("entitlement_owner_user_id exceeds the canonical user-id limit")
            if self.data_use_scope is DataUseScope.LIVE_FORWARD:
                _invalid("owner-scoped personal evidence cannot authorize live-forward use")
        rule_owners = {
            rule.entitlement_owner_user_id
            for rule in self.rules
            if rule.entitlement_owner_user_id is not None
        }
        if len(rule_owners) > 1:
            _invalid("one authority policy cannot combine multiple personal owners")
        if rule_owners and self.data_use_scope is DataUseScope.LIVE_FORWARD:
            _invalid("owner-scoped personal evidence cannot authorize live-forward use")

    @property
    def digest(self) -> str:
        """Return the immutable content identity for this policy."""

        return canonical_json_hash(self.to_payload())

    @property
    def effective_entitlement_owner_user_id(self) -> str | None:
        """Return the unique personal owner, leaving public evidence unowned."""

        if self.entitlement_owner_user_id is not None:
            return self.entitlement_owner_user_id
        return next(
            (
                rule.entitlement_owner_user_id
                for rule in self.rules
                if rule.entitlement_owner_user_id is not None
            ),
            None,
        )

    def to_payload(self) -> dict[str, object]:
        """Return deterministic JSON-safe policy material."""

        if any(rule.entitlement_owner_user_id is not None for rule in self.rules):
            return {
                "data_use_scope": self.data_use_scope.value,
                "policy_version": self.policy_version,
                "rules": [rule.to_payload(include_owner=True) for rule in self.rules],
                "schema": _POLICY_SCHEMA_V3,
            }
        return {
            "data_use_scope": self.data_use_scope.value,
            "entitlement_owner_user_id": self.entitlement_owner_user_id,
            "policy_version": self.policy_version,
            "rules": [rule.to_payload() for rule in self.rules],
            "schema": _POLICY_SCHEMA_V2,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        """Restore and validate one canonical policy."""

        if not isinstance(payload, Mapping):
            _invalid("provider authority policy payload must be an object")
        schema = payload.get("schema")
        if schema == _POLICY_SCHEMA_V1:
            _require_exact_keys(
                payload,
                expected={"data_use_scope", "policy_version", "rules", "schema"},
                field_name="provider authority policy",
            )
            owner_user_id = None
        elif schema == _POLICY_SCHEMA_V2:
            _require_exact_keys(
                payload,
                expected={
                    "data_use_scope",
                    "entitlement_owner_user_id",
                    "policy_version",
                    "rules",
                    "schema",
                },
                field_name="provider authority policy",
            )
            raw_owner = payload.get("entitlement_owner_user_id")
            if raw_owner is not None and not isinstance(raw_owner, str):
                _invalid("provider authority entitlement owner must be a string or null")
            owner_user_id = raw_owner
        elif schema == _POLICY_SCHEMA_V3:
            _require_exact_keys(
                payload,
                expected={"data_use_scope", "policy_version", "rules", "schema"},
                field_name="provider authority policy",
            )
            owner_user_id = None
        else:
            _invalid("provider authority policy schema is unsupported")
        raw_rules = payload.get("rules")
        if not isinstance(raw_rules, list):
            _invalid("provider authority rules must be an array")
        if schema == _POLICY_SCHEMA_V3 and any(
            not isinstance(item, Mapping) or "entitlement_owner_user_id" not in item
            for item in raw_rules
        ):
            _invalid("provider authority v3 rules require explicit owner semantics")
        try:
            data_use_scope = DataUseScope(
                _require_text(payload.get("data_use_scope"), field_name="data_use_scope")
            )
        except ValueError as exc:
            message = "provider authority data_use_scope is invalid"
            raise DataAuthorityError(message) from exc
        return cls(
            policy_version=_require_text(
                payload.get("policy_version"),
                field_name="policy_version",
            ),
            data_use_scope=data_use_scope,
            rules=tuple(ProviderAuthorityRule.from_payload(item) for item in raw_rules),
            entitlement_owner_user_id=owner_user_id,
        )

    def require_authorized(
        self,
        *,
        provider: str,
        entitlement_scope: str,
        entitlement_owner_user_id: str | None = None,
    ) -> None:
        """Fail closed unless exact persisted lineage is authorized."""

        _require_provider(provider)
        _require_text(entitlement_scope, field_name="entitlement_scope")
        owner = (
            _require_text(
                entitlement_owner_user_id,
                field_name="entitlement_owner_user_id",
            )
            if entitlement_owner_user_id is not None
            else None
        )
        rule = next((item for item in self.rules if item.provider == provider), None)
        if rule is None:
            message = (
                f"provider {provider!r} is not declared by authority "
                f"{self.digest} for {self.data_use_scope.value}"
            )
            raise DataAuthorityError(message)
        expected_owner = (
            rule.entitlement_owner_user_id
            if any(item.entitlement_owner_user_id is not None for item in self.rules)
            else self.entitlement_owner_user_id
        )
        if owner != expected_owner:
            message = (
                f"provider {provider!r} personal entitlement owner is not authorized "
                f"by {self.digest} for {self.data_use_scope.value}"
            )
            raise DataAuthorityError(message)
        if rule.decision is ProviderAuthorityDecision.DENY:
            message = (
                f"provider {provider!r} is denied by authority "
                f"{self.digest} for {self.data_use_scope.value}"
            )
            raise DataAuthorityError(message)
        if entitlement_scope not in rule.entitlement_scopes:
            message = (
                f"provider {provider!r} entitlement {entitlement_scope!r} is not "
                f"authorized by {self.digest} for {self.data_use_scope.value}"
            )
            raise DataAuthorityError(message)


def _require_provider(value: object) -> str:
    provider = _require_text(value, field_name="provider")
    if _PROVIDER_PATTERN.fullmatch(provider) is None:
        _invalid("provider must be a canonical lowercase identifier")
    return provider


def _require_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field_name} must be a non-blank canonical string")
    return value


def _require_exact_keys(
    payload: Mapping[str, Any],
    *,
    expected: set[str],
    field_name: str,
) -> None:
    actual = set(payload)
    if actual != expected:
        _invalid(
            f"{field_name} keys must be exactly {sorted(expected)!r}; "
            f"received {sorted(str(item) for item in actual)!r}"
        )


def _invalid(message: str) -> NoReturn:
    raise DataAuthorityError(message)


__all__ = [
    "DataAuthorityError",
    "DataUseScope",
    "ProviderAuthorityDecision",
    "ProviderAuthorityPolicy",
    "ProviderAuthorityRule",
]
