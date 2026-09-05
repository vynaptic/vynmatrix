"""Offline replay of production scoring requirements from immutable signal evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from dev_cli.validation.backtest.engine import RawSignalEvidence
from dev_cli.validation.evidence import canonical_json_bytes
from lib_strategy.signals.adapters.scoring import (
    build_scoring_view,
    evaluate_entry_signal_requirements,
)


@dataclass(frozen=True)
class ScoringReplayBindingState:
    """Operational binding context recorded with an offline scoring replay."""

    binding_id: str | None
    active: bool
    autopilot: bool
    execution_mode: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.active, bool):
            msg = "active must be a bool"
            raise TypeError(msg)
        if not isinstance(self.autopilot, bool):
            msg = "autopilot must be a bool"
            raise TypeError(msg)
        for field_name in ("binding_id", "execution_mode"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                msg = f"{field_name} must be a non-blank string when supplied"
                raise ValueError(msg)

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe context without implying threshold evaluation."""

        return {
            "binding_id": self.binding_id,
            "active": self.active,
            "autopilot": self.autopilot,
            "execution_mode": self.execution_mode,
            "activation_disposition": "binding_active" if self.active else "inactive_binding",
            "approval_disposition": (
                "autopilot_enabled" if self.autopilot else "manual_approval_required"
            ),
            "threshold_evaluation": "not_evaluated",
        }


def replay_scoring_raw_signal_ledger(
    raw_signal_ledger: Sequence[RawSignalEvidence],
    *,
    require_stop_loss: bool,
    require_explicit_scoring_inputs: bool,
    binding: ScoringReplayBindingState,
) -> dict[str, dict[str, object]]:
    """Replay entry requirements directly from immutable raw-signal evidence."""

    if not isinstance(require_stop_loss, bool):
        msg = "require_stop_loss must be a bool"
        raise TypeError(msg)
    if not isinstance(require_explicit_scoring_inputs, bool):
        msg = "require_explicit_scoring_inputs must be a bool"
        raise TypeError(msg)

    replay_ledger: dict[str, dict[str, object]] = {}
    binding_payload = binding.to_dict()
    for evidence in raw_signal_ledger:
        external_signal_id = evidence.external_signal_id
        if not isinstance(external_signal_id, str) or not external_signal_id.strip():
            msg = "raw signal replay encountered a missing canonical external_signal_id"
            raise ValueError(msg)
        if external_signal_id in replay_ledger:
            msg = (
                f"duplicate canonical external_signal_id in raw signal ledger: {external_signal_id}"
            )
            raise ValueError(msg)

        signal = evidence.to_signal()
        if (
            signal.external_signal_id != external_signal_id
            or signal.signal_id != external_signal_id
        ):
            msg = (
                f"canonical signal identity mismatch for external_signal_id={external_signal_id!r}"
            )
            raise ValueError(msg)
        scoring_view = build_scoring_view(signal)
        scoring_input_source = str(scoring_view.metadata["scoring_input_source"])
        requirement = evaluate_entry_signal_requirements(
            signal,
            require_stop_loss=require_stop_loss,
            require_explicit_scoring_inputs=require_explicit_scoring_inputs,
            scoring_input_source=scoring_input_source,
        )
        if not requirement.applicable:
            disposition = "not_applicable_non_entry"
        elif requirement.allowed:
            disposition = "allowed"
        else:
            disposition = "blocked"

        replay_ledger[external_signal_id] = {
            "external_signal_id": external_signal_id,
            "strategy_id": signal.strategy_id,
            "strategy_type": signal.strategy_type,
            "symbol": signal.symbol,
            "action": signal.action.value,
            "generated_at": signal.timestamp.isoformat(),
            "valid_until": signal.expires_at.isoformat() if signal.expires_at else None,
            "entry_requirements": {
                "applicable": requirement.applicable,
                "allowed": requirement.allowed,
                "disposition": disposition,
                "blocked_reason": requirement.rule_code,
                "message": requirement.message,
                "context": dict(requirement.context),
            },
            "scoring_input_source": scoring_input_source,
            "explicit_values": {
                "expected_return": signal.expected_return,
                "predicted_risk": signal.predicted_risk,
                "horizon": signal.horizon,
                "horizon_days": signal.horizon_days,
            },
            "derived_values": {
                "direction": scoring_view.direction,
                "expected_return": scoring_view.expected_return,
                "predicted_risk": scoring_view.predicted_risk,
                "horizon_days": scoring_view.horizon_days,
                "sharpe_raw": scoring_view.sharpe_raw,
            },
            "stop_status": {
                "applicable": signal.is_entry,
                "required": signal.is_entry and require_stop_loss,
                "present": signal.stop_loss is not None,
                "stop_loss": signal.stop_loss,
            },
            "binding": dict(binding_payload),
        }

    canonical_json_bytes(replay_ledger)
    return replay_ledger


__all__ = [
    "ScoringReplayBindingState",
    "replay_scoring_raw_signal_ledger",
]
