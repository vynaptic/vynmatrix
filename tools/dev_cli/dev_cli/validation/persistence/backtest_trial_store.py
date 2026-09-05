"""Registration and lifecycle storage for immutable validation trials."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dev_cli.validation.persistence.backtest_manifest_store import (
    canonical_manifest_bytes,
    manifest_sha256,
    validate_sha256_digest,
)
from lib_application.db.models import BacktestExperiment, BacktestTrial, StrategyVersion

if TYPE_CHECKING:
    from sqlalchemy import Engine


class BacktestTrialStatus(StrEnum):
    """Persisted lifecycle states for one immutable trial identity."""

    REGISTERED = "registered"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class BacktestTrialRegistration:
    """Identity fields required before a trial may execute."""

    experiment_id: int
    strategy_id: str
    strategy_semver: str
    sequence: int
    trial_family: str
    fold_id: str
    cost_scenario: str
    parameters: Mapping[str, Any]
    manifest_hash: str


def stable_trial_id(registration: BacktestTrialRegistration) -> str:
    """Derive a database-independent identity from registered trial content."""

    identity = {
        "cost_scenario": registration.cost_scenario,
        "fold_id": registration.fold_id,
        "manifest_hash": registration.manifest_hash,
        "parameters": dict(registration.parameters),
        "sequence": registration.sequence,
        "strategy_id": registration.strategy_id,
        "strategy_semver": registration.strategy_semver,
        "trial_family": registration.trial_family,
    }
    return manifest_sha256(identity)


class BacktestTrialStore:
    """Register immutable trials and apply idempotent lifecycle transitions."""

    _TRANSITIONS: ClassVar[dict[BacktestTrialStatus, set[BacktestTrialStatus]]] = {
        BacktestTrialStatus.REGISTERED: {
            BacktestTrialStatus.RUNNING,
            BacktestTrialStatus.FAILED,
            BacktestTrialStatus.INTERRUPTED,
            BacktestTrialStatus.ABANDONED,
        },
        BacktestTrialStatus.RUNNING: {
            BacktestTrialStatus.FAILED,
            BacktestTrialStatus.INTERRUPTED,
        },
    }

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(self, registration: BacktestTrialRegistration) -> str:
        """Register a trial once; identical retries return the same identity."""

        normalized = self._validate_registration(registration)
        trial_id = stable_trial_id(registration)
        with Session(self._engine) as session:
            experiment = session.get(BacktestExperiment, registration.experiment_id)
            if experiment is None:
                msg = f"backtest experiment {registration.experiment_id} is not registered"
                raise ValueError(msg)
            if experiment.strategy_id != registration.strategy_id:
                msg = "backtest experiment strategy does not match trial strategy"
                raise ValueError(msg)

            version = session.execute(
                select(StrategyVersion).where(
                    StrategyVersion.strategy_id == registration.strategy_id,
                    StrategyVersion.semver == registration.strategy_semver,
                )
            ).scalar_one_or_none()
            if version is None:
                msg = (
                    "exact strategy version is not registered: "
                    f"{registration.strategy_id}@{registration.strategy_semver}"
                )
                raise ValueError(msg)

            expected = {
                "trial_id": trial_id,
                "experiment_id": registration.experiment_id,
                "strategy_id": registration.strategy_id,
                "strat_ver_id": version.strat_ver_id,
                "sequence": registration.sequence,
                "trial_family": registration.trial_family,
                "fold_id": registration.fold_id,
                "cost_scenario": registration.cost_scenario,
                "parameters": normalized,
                "manifest_hash": registration.manifest_hash,
            }
            existing = session.get(BacktestTrial, trial_id)
            if existing is not None:
                self._require_same_identity(existing, expected)
                return trial_id

            occupied = session.execute(
                select(BacktestTrial).where(
                    BacktestTrial.experiment_id == registration.experiment_id,
                    BacktestTrial.sequence == registration.sequence,
                )
            ).scalar_one_or_none()
            if occupied is not None:
                msg = (
                    f"experiment {registration.experiment_id} sequence "
                    f"{registration.sequence} already belongs to {occupied.trial_id}"
                )
                raise ValueError(msg)

            session.add(BacktestTrial(**expected))
            try:
                session.commit()
            except IntegrityError:
                # A concurrent registrar may have committed the same stable
                # identity or occupied the sequence after our initial reads.
                session.rollback()
                concurrent = session.get(BacktestTrial, trial_id)
                if concurrent is not None:
                    self._require_same_identity(concurrent, expected)
                    return trial_id
                occupied = session.execute(
                    select(BacktestTrial).where(
                        BacktestTrial.experiment_id == registration.experiment_id,
                        BacktestTrial.sequence == registration.sequence,
                    )
                ).scalar_one_or_none()
                if occupied is not None:
                    msg = (
                        f"experiment {registration.experiment_id} sequence "
                        f"{registration.sequence} already belongs to {occupied.trial_id}"
                    )
                    raise ValueError(msg) from None
                raise
        return trial_id

    def mark_running(self, trial_id: str) -> None:
        """Start a registered trial; repeated starts are idempotent."""

        self._transition(trial_id, BacktestTrialStatus.RUNNING)

    def mark_failed(self, trial_id: str, *, error_class: str, error_context: str) -> None:
        """Retain a non-secret failure description without overwriting retries."""

        self._transition(
            trial_id,
            BacktestTrialStatus.FAILED,
            error_class=error_class,
            error_context=error_context,
        )

    def mark_interrupted(self, trial_id: str, *, error_context: str) -> None:
        """Record process/operator interruption as a terminal attempted trial."""

        self._transition(
            trial_id,
            BacktestTrialStatus.INTERRUPTED,
            error_class="Interrupted",
            error_context=error_context,
        )

    def mark_abandoned(self, trial_id: str, *, error_context: str) -> None:
        """Record a pre-execution abandonment without deleting the registry row."""

        self._transition(
            trial_id,
            BacktestTrialStatus.ABANDONED,
            error_class="Abandoned",
            error_context=error_context,
        )

    def _transition(
        self,
        trial_id: str,
        target: BacktestTrialStatus,
        *,
        error_class: str | None = None,
        error_context: str | None = None,
    ) -> None:
        now = datetime.now(tz=UTC)
        with Session(self._engine) as session:
            trial = session.execute(
                select(BacktestTrial).where(BacktestTrial.trial_id == trial_id).with_for_update()
            ).scalar_one_or_none()
            if trial is None:
                msg = f"backtest trial is not registered: {trial_id}"
                raise ValueError(msg)
            current = BacktestTrialStatus(trial.status)
            if current == target:
                return
            if target not in self._TRANSITIONS.get(current, set()):
                msg = f"illegal backtest trial transition: {current.value} -> {target.value}"
                raise ValueError(msg)

            trial.status = target.value
            trial.updated_at = now
            if target is BacktestTrialStatus.RUNNING:
                trial.started_at = now
            else:
                trial.completed_at = now
                trial.error_class = self._bounded(error_class, 255, "error_class")
                trial.error_context = self._bounded(error_context, 4000, "error_context")
            session.commit()

    @staticmethod
    def _validate_registration(registration: BacktestTrialRegistration) -> dict[str, Any]:
        if registration.experiment_id <= 0:
            msg = "experiment_id must be positive"
            raise ValueError(msg)
        if registration.sequence < 0:
            msg = "trial sequence must be non-negative"
            raise ValueError(msg)
        for field, value, maximum in (
            ("strategy_id", registration.strategy_id, 50),
            ("strategy_semver", registration.strategy_semver, 20),
            ("trial_family", registration.trial_family, 64),
            ("fold_id", registration.fold_id, 64),
            ("cost_scenario", registration.cost_scenario, 64),
        ):
            if not value or len(value) > maximum:
                msg = f"{field} must contain 1-{maximum} characters"
                raise ValueError(msg)
        validate_sha256_digest(registration.manifest_hash, field="manifest_hash")
        if not isinstance(registration.parameters, Mapping):
            msg = "trial parameters must be a JSON object"
            raise TypeError(msg)
        payload = canonical_manifest_bytes({"parameters": dict(registration.parameters)})
        normalized = json.loads(payload)["parameters"]
        if not isinstance(normalized, dict):
            msg = "trial parameters must be a JSON object"
            raise TypeError(msg)
        return normalized

    @staticmethod
    def _require_same_identity(existing: BacktestTrial, expected: Mapping[str, Any]) -> None:
        mismatches = [key for key, value in expected.items() if getattr(existing, key) != value]
        if mismatches:
            fields = ", ".join(mismatches)
            msg = f"trial identity overwrite prohibited; mismatched fields: {fields}"
            raise ValueError(msg)

    @staticmethod
    def _bounded(value: str | None, maximum: int, field: str) -> str | None:
        if value is None:
            return None
        if not value or len(value) > maximum:
            msg = f"{field} must contain 1-{maximum} characters"
            raise ValueError(msg)
        return value


__all__ = [
    "BacktestTrialRegistration",
    "BacktestTrialStatus",
    "BacktestTrialStore",
    "stable_trial_id",
]
