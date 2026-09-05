"""Owner-isolated identity contract for synchronized panel workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from lib_common.hashing import canonical_json_hash
from lib_strategy.signals.pure_strategy import ModelStateContractError

_USER_ID_MAX_LENGTH = 50


class PanelCorrectionError(RuntimeError):
    """A different input attempted to replace a revision-pinned official session."""


@dataclass(frozen=True)
class PanelPreparation:
    """Durable preparation result used by the panel capability adapter."""

    decision_key: str
    status: str

    @property
    def should_evaluate(self) -> bool:
        return self.status == "prepared"


@dataclass(frozen=True)
class PanelRuntimeBinding:
    """Immutable owner, scope, environment, and activation boundary."""

    environment: str
    data_use_scope: str
    entitlement_owner_user_id: str
    activation_cutoff: datetime

    def __post_init__(self) -> None:
        environment = self.environment.strip().lower()
        owner = self.entitlement_owner_user_id.strip()
        scope = self.data_use_scope.strip().lower()
        if not environment:
            message = "Panel runtime environment must be non-empty"
            raise ModelStateContractError(message)
        if not owner or len(owner) > _USER_ID_MAX_LENGTH:
            message = "Panel runtime entitlement owner exceeds the canonical user-id limit"
            raise ModelStateContractError(message)
        if scope != "paper_forward":
            message = "Synchronized indicator workers permit only paper_forward inputs"
            raise ModelStateContractError(message)
        if self.activation_cutoff.tzinfo is None or self.activation_cutoff.utcoffset() is None:
            message = "Panel runtime activation cutoff must be timezone-aware"
            raise ModelStateContractError(message)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "data_use_scope", scope)
        object.__setattr__(self, "entitlement_owner_user_id", owner)
        object.__setattr__(self, "activation_cutoff", self.activation_cutoff.astimezone(UTC))

    @property
    def digest(self) -> str:
        """Return the content identity used for worker and outbox partitions."""

        return canonical_json_hash(
            {
                "activation_cutoff": self.activation_cutoff.isoformat(),
                "data_use_scope": self.data_use_scope,
                "entitlement_owner_user_id": self.entitlement_owner_user_id,
                "environment": self.environment,
                "schema": "indicator-panel-runtime-binding-v1",
            }
        )


def scoped_panel_worker_id(base_worker_id: str, binding: PanelRuntimeBinding) -> str:
    """Return an owner-isolated durable worker partition within the DB limit."""

    base = base_worker_id.strip()
    if not base:
        message = "Panel worker id must not be empty"
        raise ModelStateContractError(message)
    suffix = f":panel:{binding.digest[:16]}"
    return f"{base[: 100 - len(suffix)]}{suffix}"


def panel_outbox_ordering_key(worker_id: str, binding: PanelRuntimeBinding) -> str:
    """Return a bounded exact-worker partition for the transactional outbox."""

    digest = canonical_json_hash(
        {
            "binding_sha256": binding.digest,
            "schema": "indicator-panel-outbox-ordering-v1",
            "worker_id": worker_id,
        }
    )
    return f"panel:{digest}"


__all__ = [
    "PanelCorrectionError",
    "PanelPreparation",
    "PanelRuntimeBinding",
    "panel_outbox_ordering_key",
    "scoped_panel_worker_id",
]
