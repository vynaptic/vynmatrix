"""Database persistence operations for atomic portfolio rebalance ingestion."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import isclose
from typing import Any, Literal

from sqlalchemy import select, text

from lib_common.hashing import canonical_json_hash
from lib_common.internal_events import CanonicalSignalSnapshot, ModelRebalanceSubmissionEvent
from lib_strategy.data_authority import ProviderAuthorityPolicy
from lib_strategy.panels import panel_ready_input_from_payload

from ._storage_base import app_models
from .models import AccountRebalancePlanDraft

_PLAN_PROGRESS_DEADLINE = timedelta(hours=1)


def _db_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _same_number(left: Any, right: float | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return isclose(float(left), right, rel_tol=0.0, abs_tol=1e-10)


def _instrument_id(value: str | int | None) -> int:
    try:
        instrument_id = int(value or 0)
    except (TypeError, ValueError) as exc:
        message = f"Model rebalance instrument identity is not numeric: {value!r}"
        raise ValueError(message) from exc
    if instrument_id <= 0:
        message = "Model rebalance instrument identity must be positive"
        raise ValueError(message)
    return instrument_id


def _model_leg_sha256(
    leg: Any,
    prior_lineage: tuple[str, str] | None,
) -> str:
    payload = leg.model_dump(mode="json")
    payload["resolved_prior_model_rebalance_id"] = (
        prior_lineage[0] if prior_lineage is not None else None
    )
    payload["resolved_prior_model_leg_id"] = prior_lineage[1] if prior_lineage is not None else None
    return canonical_json_hash(payload)


def _model_signal_snapshot(leg: Any) -> tuple[dict[str, Any], str]:
    payload = leg.signal.model_dump(mode="json")
    return payload, canonical_json_hash(payload)


def _completed_panel_matches_event(
    row: Any,
    rank_snapshot: Any,
    event: ModelRebalanceSubmissionEvent,
) -> bool:
    """Return whether one completed panel row proves the submitted batch lineage."""

    try:
        panel = panel_ready_input_from_payload(row.panel_payload)
    except (TypeError, ValueError):
        return False
    audit_payload = row.evaluation_audit_payload
    strategy_input_payload = row.strategy_input_payload
    signal_envelopes = row.signal_envelopes
    if (
        not isinstance(audit_payload, Mapping)
        or not isinstance(strategy_input_payload, Mapping)
        or not isinstance(signal_envelopes, list)
    ):
        return False
    audit_rank = audit_payload.get("rank_snapshot")
    if not isinstance(audit_rank, Mapping):
        return False
    if (
        canonical_json_hash(dict(strategy_input_payload)) != str(row.strategy_input_sha256)
        or canonical_json_hash(dict(audit_payload)) != str(row.evaluation_audit_sha256)
        or panel.canonical_digest() != str(row.panel_sha256)
    ):
        return False
    expected_signals = tuple(leg.signal for leg in event.legs)
    actual_signals = _ordered_panel_signals(signal_envelopes)
    if actual_signals != expected_signals:
        return False
    actual = {
        "strategy_id": str(row.strategy_id),
        "strategy_version": str(row.strategy_version),
        "cutoff_at": _db_timestamp(row.cutoff_at),
        "official_session_date": row.official_session_date,
        "execute_not_before": _db_timestamp(row.execute_not_before),
        "execution_session_sha256": str(row.execution_session_sha256),
        "data_use_scope": str(row.data_use_scope),
        "provider_authority_sha256": str(row.provider_authority_sha256),
        "strategy_input_sha256": str(row.strategy_input_sha256),
        "rank_snapshot_id": str(row.rank_snapshot_id),
        "panel_sha256": str(row.panel_sha256),
        "factor_snapshot_sha256": str(row.factor_snapshot_sha256),
        "provider_authority_policy": row.provider_authority_policy,
        "audit_configuration_sha256": audit_payload.get("configuration_sha256"),
        "audit_rank_snapshot_sha256": audit_rank.get("content_digest"),
        "audit_cash_slots": audit_payload.get("intentional_cash_slots"),
        "panel_cutoff": _db_timestamp(panel.cutoff),
        "panel_session_date": panel.session.session_date,
        "panel_execute_not_before": _db_timestamp(panel.execution_session.opens_at),
        "panel_execution_session_sha256": panel.execution_session.content_sha256,
        "panel_data_use_scope": panel.data_use_scope.value,
        "panel_provider_authority_sha256": panel.provider_authority_sha256,
        "panel_factor_snapshot_sha256": panel.factor_snapshot_sha256,
        "panel_provider_authority_policy": panel.provider_authority_policy.to_payload(),
    }
    expected = {
        "strategy_id": event.strategy_id,
        "strategy_version": event.strategy_version,
        "cutoff_at": _db_timestamp(event.decision_cutoff),
        "official_session_date": event.effective_session,
        "execute_not_before": _db_timestamp(event.execute_not_before),
        "execution_session_sha256": event.execution_session_sha256,
        "data_use_scope": event.data_use_scope,
        "provider_authority_sha256": event.provider_authority_sha256,
        "strategy_input_sha256": event.input_snapshot_sha256,
        "rank_snapshot_id": event.rank_snapshot_sha256,
        "panel_sha256": str(rank_snapshot.panel_revision_digest),
        "factor_snapshot_sha256": str(rank_snapshot.factor_content_digest),
        "provider_authority_policy": rank_snapshot.provider_authority_policy,
        "audit_configuration_sha256": event.configuration_sha256,
        "audit_rank_snapshot_sha256": event.rank_snapshot_sha256,
        "audit_cash_slots": event.intentional_cash_slots,
        "panel_cutoff": _db_timestamp(event.decision_cutoff),
        "panel_session_date": event.effective_session,
        "panel_execute_not_before": _db_timestamp(event.execute_not_before),
        "panel_execution_session_sha256": event.execution_session_sha256,
        "panel_data_use_scope": event.data_use_scope,
        "panel_provider_authority_sha256": event.provider_authority_sha256,
        "panel_factor_snapshot_sha256": str(rank_snapshot.factor_content_digest),
        "panel_provider_authority_policy": rank_snapshot.provider_authority_policy,
    }
    return actual == expected


def _ordered_panel_signals(
    signal_envelopes: list[Any],
) -> tuple[CanonicalSignalSnapshot, ...] | None:
    """Return exact canonical signals only for one complete contiguous batch."""

    ordered: list[tuple[int, CanonicalSignalSnapshot]] = []
    for envelope in signal_envelopes:
        if not isinstance(envelope, Mapping):
            return None
        try:
            signal = CanonicalSignalSnapshot.model_validate(dict(envelope))
        except (TypeError, ValueError):
            return None
        sequence = signal.metadata.get("model_rebalance_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            return None
        ordered.append((sequence, signal))
    ordered.sort()
    if [sequence for sequence, _ in ordered] != list(range(len(ordered))):
        return None
    return tuple(signal for _, signal in ordered)


class _RebalanceStoreOps:
    """Mixin joined to ``AppScoreStore``'s active transaction/session boundary."""

    def _resolve_rebalance_lineage(
        self,
        session: Any,
        event: ModelRebalanceSubmissionEvent,
    ) -> tuple[int, Any]:
        if app_models is None:
            message = "Application models are unavailable"
            raise RuntimeError(message)
        version_id = self._resolve_strategy_version(  # type: ignore[attr-defined]
            session,
            event.strategy_id,
            event.strategy_version,
        )
        if version_id is None:
            message = f"Unknown strategy version {event.strategy_id!r}/{event.strategy_version!r}"
            raise ValueError(message)
        rank_snapshot = session.get(app_models.EquityRankSnapshot, event.rank_snapshot_sha256)
        if rank_snapshot is None:
            message = (
                "Model rebalance requires its existing immutable rank snapshot "
                f"{event.rank_snapshot_sha256}"
            )
            raise ValueError(message)
        actual = {
            "strategy_id": str(rank_snapshot.strategy_id),
            "strat_ver_id": int(rank_snapshot.strat_ver_id),
            "effective_session": rank_snapshot.effective_session,
            "cutoff_at": _db_timestamp(rank_snapshot.cutoff_at),
            "configuration_sha256": str(rank_snapshot.configuration_digest),
            "data_use_scope": str(rank_snapshot.data_use_scope),
            "provider_authority_sha256": str(rank_snapshot.provider_authority_digest),
            "status": str(rank_snapshot.completeness_status),
            "content_sha256": str(rank_snapshot.content_sha256),
        }
        expected = {
            "strategy_id": event.strategy_id,
            "strat_ver_id": version_id,
            "effective_session": event.effective_session,
            "cutoff_at": _db_timestamp(event.decision_cutoff),
            "configuration_sha256": event.configuration_sha256,
            "data_use_scope": event.data_use_scope,
            "provider_authority_sha256": event.provider_authority_sha256,
            "status": "complete",
            "content_sha256": event.rank_snapshot_sha256,
        }
        if actual != expected:
            message = "Model rebalance rank snapshot lineage does not match the submission"
            raise ValueError(message)
        try:
            authority = ProviderAuthorityPolicy.from_payload(
                rank_snapshot.provider_authority_policy
            )
        except (TypeError, ValueError) as exc:
            message = "Model rebalance rank snapshot has an invalid provider authority policy"
            raise ValueError(message) from exc
        if (
            authority.digest != event.provider_authority_sha256
            or authority.data_use_scope.value != event.data_use_scope
        ):
            message = "Model rebalance provider authority policy differs from its frozen lineage"
            raise ValueError(message)
        self._require_completed_panel_decision(session, event, rank_snapshot)
        return version_id, rank_snapshot

    def resolve_model_rebalance_entitlement_owner(
        self,
        event: ModelRebalanceSubmissionEvent,
    ) -> str | None:
        """Resolve personal-use ownership from the exact immutable rank policy."""

        if app_models is None:
            message = "Application models are unavailable"
            raise RuntimeError(message)
        with self._session() as session:  # type: ignore[attr-defined]
            _version_id, rank_snapshot = self._resolve_rebalance_lineage(session, event)
            authority = ProviderAuthorityPolicy.from_payload(
                rank_snapshot.provider_authority_policy
            )
            return authority.effective_entitlement_owner_user_id

    @staticmethod
    def _require_completed_panel_decision(
        session: Any,
        event: ModelRebalanceSubmissionEvent,
        rank_snapshot: Any,
    ) -> None:
        """Prove the batch is the exact output of a completed panel transition."""

        if app_models is None:
            message = "Application models are unavailable"
            raise RuntimeError(message)
        candidates = session.scalars(
            select(app_models.StrategyPanelDecision).where(
                app_models.StrategyPanelDecision.rank_snapshot_id == event.rank_snapshot_sha256,
                app_models.StrategyPanelDecision.status == "completed",
            )
        ).all()
        if not any(_completed_panel_matches_event(row, rank_snapshot, event) for row in candidates):
            message = (
                "Model rebalance is not backed by an exact completed synchronized-panel decision"
            )
            raise ValueError(message)

    def _lock_and_resolve_previous_model(
        self,
        session: Any,
        event: ModelRebalanceSubmissionEvent,
        *,
        version_id: int,
    ) -> Any | None:
        """Serialize one strategy/version stream and enforce session ordering."""

        if app_models is None:
            message = "Application models are unavailable"
            raise RuntimeError(message)
        bind = session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:partition_key, 0))"),
                {"partition_key": f"model-rebalance:{event.strategy_id}:{version_id}"},
            )
        existing = session.get(app_models.ModelRebalance, event.rebalance_id)
        query = session.query(app_models.ModelRebalance).filter(
            app_models.ModelRebalance.strategy_id == event.strategy_id,
            app_models.ModelRebalance.strat_ver_id == version_id,
        )
        if existing is not None:
            return (
                query.filter(app_models.ModelRebalance.effective_session < event.effective_session)
                .order_by(
                    app_models.ModelRebalance.effective_session.desc(),
                    app_models.ModelRebalance.decision_cutoff.desc(),
                )
                .first()
            )
        latest = query.order_by(
            app_models.ModelRebalance.effective_session.desc(),
            app_models.ModelRebalance.decision_cutoff.desc(),
        ).first()
        if latest is None:
            return None
        if event.effective_session <= latest.effective_session or _db_timestamp(
            event.decision_cutoff
        ) <= _db_timestamp(latest.decision_cutoff):
            message = (
                "Model rebalance is out of order or diverges from the existing "
                "strategy-version effective session"
            )
            raise ValueError(message)
        return latest

    @staticmethod
    def _resolve_previous_target(
        session: Any,
        leg: Any,
        *,
        instrument_id: int,
        previous_model: Any | None,
    ) -> tuple[str, str]:
        if app_models is None:
            message = "Application models are unavailable"
            raise RuntimeError(message)
        if previous_model is None:
            message = f"Model exit leg {leg.sequence} has no immediately previous model"
            raise ValueError(message)
        previous_count = (
            session.query(app_models.ModelRebalanceLeg)
            .filter_by(rebalance_id=previous_model.rebalance_id)
            .count()
        )
        if previous_count != int(previous_model.expected_leg_count):
            message = "Immediately previous model rebalance is incomplete"
            raise ValueError(message)
        prior = (
            session.query(app_models.ModelRebalanceLeg)
            .filter(
                app_models.ModelRebalanceLeg.rebalance_id == previous_model.rebalance_id,
                app_models.ModelRebalanceLeg.instr_id == instrument_id,
            )
            .one_or_none()
        )
        if prior is None or (
            str(prior.factor_snapshot_id) != leg.factor_snapshot_id
            or str(prior.phase) not in {"entry", "hold", "reduce"}
            or str(prior.action) not in {"long", "hold"}
        ):
            message = (
                f"Model exit leg {leg.sequence} cannot prove that the immediately "
                "previous model still targeted the instrument"
            )
            raise ValueError(message)
        return str(previous_model.rebalance_id), str(prior.leg_id)

    @staticmethod
    def _validate_current_rank_leg(
        event: ModelRebalanceSubmissionEvent,
        leg: Any,
        *,
        version_id: int,
        instrument_id: int,
        rank_row: Any | None,
        factor: Any,
    ) -> bool:
        """Validate current-row lineage; return whether prior target proof is needed."""

        if rank_row is None:
            message = f"Model leg {leg.sequence} requires its current rank row"
            raise ValueError(message)
        decision = str(rank_row.decision)
        allowed_decisions = {
            "entry": {"selected"},
            "hold": {"hold"},
            "reduce": {"hold"},
            "exit": {"eligible", "excluded", "exit"},
        }[leg.phase]
        if decision not in allowed_decisions:
            message = f"Model leg {leg.sequence} phase disagrees with persisted rank decision"
            raise ValueError(message)
        if leg.phase in {"entry", "hold", "reduce"} and not bool(rank_row.strategy_eligible):
            message = f"Model leg {leg.sequence} targets a rank-ineligible instrument"
            raise ValueError(message)
        expected_allocation = None if leg.phase == "exit" else leg.allocation_hint
        if not _same_number(rank_row.target_allocation_hint, expected_allocation):
            message = f"Model leg {leg.sequence} allocation differs from persisted rank evidence"
            raise ValueError(message)
        raw_composite = leg.signal.metadata.get("composite_score")
        if raw_composite is not None and isinstance(raw_composite, bool):
            message = f"Model leg {leg.sequence} composite score is invalid"
            raise ValueError(message)
        try:
            submitted_composite = float(raw_composite) if raw_composite is not None else None
        except (TypeError, ValueError) as exc:
            message = f"Model leg {leg.sequence} composite score is invalid"
            raise ValueError(message) from exc
        if not _same_number(rank_row.composite_score, submitted_composite):
            message = (
                f"Model leg {leg.sequence} composite score differs from persisted rank evidence"
            )
            raise ValueError(message)
        current_factor_id = leg.current_rank_factor_snapshot_id
        if current_factor_id is None:
            valid_exclusion = (
                leg.phase == "exit"
                and str(rank_row.decision) in {"exit", "excluded"}
                and bool(rank_row.incumbent)
                and rank_row.factor_snapshot_id is None
                and str(rank_row.exclusion_reason or "") == str(leg.membership_change_reason or "")
            )
            if not valid_exclusion:
                message = (
                    f"Model exit leg {leg.sequence} rank-incomplete current-row "
                    "lineage is inconsistent"
                )
                raise ValueError(message)
            if leg.membership_change_reason == "panel_excluded":
                return True
            ineligible_factor_identity = (
                int(factor.strat_ver_id),
                factor.effective_session,
                _db_timestamp(factor.cutoff_at),
                str(factor.configuration_digest),
            )
            valid_factor = ineligible_factor_identity == (
                version_id,
                event.effective_session,
                _db_timestamp(event.decision_cutoff),
                event.configuration_sha256,
            ) and str(factor.completeness_status) in {"incomplete", "ineligible"}
            if not valid_factor:
                message = f"Model exit leg {leg.sequence} ineligible factor lineage is inconsistent"
                raise ValueError(message)
            return False
        if (
            current_factor_id != leg.factor_snapshot_id
            or str(rank_row.factor_snapshot_id or "") != current_factor_id
        ):
            message = (
                f"Model leg {leg.sequence} factor/rank lineage does not match "
                "the persisted per-instrument rank row"
            )
            raise ValueError(message)
        if not _same_number(rank_row.rank_position, leg.rank):
            message = f"Model leg {leg.sequence} rank differs from persisted rank evidence"
            raise ValueError(message)
        factor_identity = (
            str(factor.strategy_id),
            int(factor.strat_ver_id),
            int(factor.instr_id),
            factor.effective_session,
            _db_timestamp(factor.cutoff_at),
            str(factor.configuration_digest),
            str(factor.completeness_status),
        )
        expected_factor = (
            event.strategy_id,
            version_id,
            instrument_id,
            event.effective_session,
            _db_timestamp(event.decision_cutoff),
            event.configuration_sha256,
            "complete",
        )
        if factor_identity != expected_factor:
            message = f"Model leg {leg.sequence} factor snapshot lineage is inconsistent"
            raise ValueError(message)
        return False

    def _validate_rebalance_legs(
        self,
        session: Any,
        event: ModelRebalanceSubmissionEvent,
        *,
        version_id: int,
        previous_model: Any | None,
    ) -> dict[int, tuple[str, str]]:
        if app_models is None:
            message = "Application models are unavailable"
            raise RuntimeError(message)
        prior_lineage: dict[int, tuple[str, str]] = {}
        for leg in event.legs:
            if leg.rank_snapshot_id != event.rank_snapshot_sha256:
                message = f"Model leg {leg.sequence} references a different rank snapshot"
                raise ValueError(message)
            instrument_id = _instrument_id(leg.signal.instrument_id)
            instrument = session.get(app_models.Instrument, instrument_id)
            if instrument is None or (
                str(instrument.asset_class) != "equity"
                or not bool(instrument.is_tradable)
                or str(instrument.canonical) != leg.signal.symbol
            ):
                message = f"Model leg {leg.sequence} instrument catalogue identity is inconsistent"
                raise ValueError(message)
            rank_row = session.get(
                app_models.EquityRankSnapshotRow,
                (leg.rank_snapshot_id, instrument_id),
            )
            factor = session.get(app_models.EquityFactorSnapshot, leg.factor_snapshot_id)
            if factor is None or (
                str(factor.strategy_id) != event.strategy_id
                or int(factor.instr_id) != instrument_id
            ):
                message = f"Model leg {leg.sequence} factor snapshot is unavailable"
                raise ValueError(message)

            if not leg.current_rank_row_present:
                if rank_row is not None:
                    message = (
                        f"Model exit leg {leg.sequence} claims a universe departure "
                        "but a current rank row exists"
                    )
                    raise ValueError(message)
                prior_lineage[leg.sequence] = self._resolve_previous_target(
                    session,
                    leg,
                    instrument_id=instrument_id,
                    previous_model=previous_model,
                )
                continue
            needs_prior = self._validate_current_rank_leg(
                event,
                leg,
                version_id=version_id,
                instrument_id=instrument_id,
                rank_row=rank_row,
                factor=factor,
            )
            if needs_prior:
                prior_lineage[leg.sequence] = self._resolve_previous_target(
                    session,
                    leg,
                    instrument_id=instrument_id,
                    previous_model=previous_model,
                )
        return prior_lineage

    def classify_model_rebalance(
        self,
        event: ModelRebalanceSubmissionEvent,
    ) -> Literal["new", "replay"]:
        """Preflight immutable lineage, collisions, and exact replay identity."""

        if app_models is None:
            message = "Application models are unavailable"
            raise RuntimeError(message)
        with self._session() as session:  # type: ignore[attr-defined]
            version_id, _rank = self._resolve_rebalance_lineage(session, event)
            previous_model = self._lock_and_resolve_previous_model(
                session,
                event,
                version_id=version_id,
            )
            prior_lineage = self._validate_rebalance_legs(
                session,
                event,
                version_id=version_id,
                previous_model=previous_model,
            )
            existing = session.get(app_models.ModelRebalance, event.rebalance_id)
            if existing is not None:
                persisted = (
                    str(existing.strategy_id),
                    int(existing.strat_ver_id),
                    str(existing.rank_snapshot_id),
                    existing.effective_session,
                    _db_timestamp(existing.decision_cutoff),
                    _db_timestamp(existing.execute_not_before),
                    str(existing.execution_session_sha256),
                    str(existing.data_use_scope),
                    str(existing.provider_authority_sha256),
                    str(existing.configuration_sha256),
                    str(existing.input_snapshot_sha256),
                    int(existing.expected_leg_count),
                    int(existing.intentional_cash_slots),
                    str(existing.content_sha256),
                )
                submitted = (
                    event.strategy_id,
                    version_id,
                    event.rank_snapshot_sha256,
                    event.effective_session,
                    _db_timestamp(event.decision_cutoff),
                    _db_timestamp(event.execute_not_before),
                    event.execution_session_sha256,
                    event.data_use_scope,
                    event.provider_authority_sha256,
                    event.configuration_sha256,
                    event.input_snapshot_sha256,
                    event.expected_leg_count,
                    event.intentional_cash_slots,
                    event.content_sha256,
                )
                if persisted != submitted:
                    message = (
                        f"Model rebalance {event.rebalance_id} replayed with different content"
                    )
                    raise ValueError(message)
                persisted_legs = (
                    session.query(app_models.ModelRebalanceLeg)
                    .filter_by(rebalance_id=event.rebalance_id)
                    .order_by(app_models.ModelRebalanceLeg.sequence)
                    .all()
                )
                submitted_legs = [
                    (
                        leg.sequence,
                        leg.leg_id,
                        _model_leg_sha256(leg, prior_lineage.get(leg.sequence)),
                        _model_signal_snapshot(leg)[1],
                    )
                    for leg in event.legs
                ]
                existing_legs = [
                    (
                        int(row.sequence),
                        str(row.leg_id),
                        str(row.leg_sha256),
                        str(row.signal_snapshot_sha256),
                    )
                    for row in persisted_legs
                ]
                if existing_legs != submitted_legs:
                    message = "Model rebalance replay has different ordered leg content"
                    raise ValueError(message)
                if any(
                    canonical_json_hash(row.signal_snapshot) != str(row.signal_snapshot_sha256)
                    for row in persisted_legs
                ):
                    message = "Persisted model rebalance signal snapshot digest is invalid"
                    raise ValueError(message)
                return "replay"

            external_ids = [leg.signal.external_signal_id for leg in event.legs]
            collision = (
                session.query(app_models.CanonicalSignal.external_signal_id)
                .filter(app_models.CanonicalSignal.external_signal_id.in_(external_ids))
                .first()
                if external_ids
                else None
            )
            if collision is not None:
                message = f"New model rebalance collides with signal {collision[0]!r}"
                raise ValueError(message)
        return "new"

    def persist_model_rebalance(self, event: ModelRebalanceSubmissionEvent) -> None:
        """Persist a complete model header and exact canonical-signal legs."""

        if app_models is None:
            message = "Application models are unavailable"
            raise RuntimeError(message)
        with self._session() as session:  # type: ignore[attr-defined]
            version_id, _rank = self._resolve_rebalance_lineage(session, event)
            previous_model = self._lock_and_resolve_previous_model(
                session,
                event,
                version_id=version_id,
            )
            prior_lineage = self._validate_rebalance_legs(
                session,
                event,
                version_id=version_id,
                previous_model=previous_model,
            )
            session.add(
                app_models.ModelRebalance(
                    rebalance_id=event.rebalance_id,
                    strategy_id=event.strategy_id,
                    strat_ver_id=version_id,
                    rank_snapshot_id=event.rank_snapshot_sha256,
                    effective_session=event.effective_session,
                    decision_cutoff=event.decision_cutoff,
                    execute_not_before=event.execute_not_before,
                    execution_session_sha256=event.execution_session_sha256,
                    data_use_scope=event.data_use_scope,
                    provider_authority_sha256=event.provider_authority_sha256,
                    configuration_sha256=event.configuration_sha256,
                    input_snapshot_sha256=event.input_snapshot_sha256,
                    expected_leg_count=event.expected_leg_count,
                    intentional_cash_slots=event.intentional_cash_slots,
                    content_sha256=event.content_sha256,
                )
            )
            session.flush()
            for leg in event.legs:
                instrument_id = _instrument_id(leg.signal.instrument_id)
                resolved_prior = prior_lineage.get(leg.sequence)
                signal_snapshot, signal_snapshot_sha256 = _model_signal_snapshot(leg)
                canonical = (
                    session.query(app_models.CanonicalSignal)
                    .filter_by(external_signal_id=leg.signal.external_signal_id)
                    .one_or_none()
                )
                if canonical is None:
                    message = f"Canonical signal {leg.signal.external_signal_id!r} is missing"
                    raise ValueError(message)
                actual_signal = (
                    str(canonical.strategy_id),
                    int(canonical.strat_ver_id or 0),
                    int(canonical.instr_id),
                    _db_timestamp(canonical.ts),
                    str(canonical.action),
                )
                expected_signal = (
                    event.strategy_id,
                    version_id,
                    instrument_id,
                    _db_timestamp(event.decision_cutoff),
                    {"long": "long", "hold": "hold", "close": "flat"}.get(
                        leg.signal.action.lower()
                    ),
                )
                if actual_signal != expected_signal:
                    message = f"Canonical signal for model leg {leg.sequence} is inconsistent"
                    raise ValueError(message)
                session.add(
                    app_models.ModelRebalanceLeg(
                        rebalance_id=event.rebalance_id,
                        sequence=leg.sequence,
                        leg_id=leg.leg_id,
                        leg_sha256=_model_leg_sha256(leg, resolved_prior),
                        signal_snapshot=signal_snapshot,
                        signal_snapshot_sha256=signal_snapshot_sha256,
                        rank_snapshot_id=leg.rank_snapshot_id,
                        factor_snapshot_id=leg.factor_snapshot_id,
                        current_rank_row_present=leg.current_rank_row_present,
                        current_rank_instr_id=(
                            instrument_id if leg.current_rank_row_present else None
                        ),
                        current_rank_factor_snapshot_id=(leg.current_rank_factor_snapshot_id),
                        prior_model_rebalance_id=(
                            resolved_prior[0] if resolved_prior is not None else None
                        ),
                        prior_model_leg_id=(
                            resolved_prior[1] if resolved_prior is not None else None
                        ),
                        membership_change_reason=leg.membership_change_reason,
                        instr_id=instrument_id,
                        external_signal_id=leg.signal.external_signal_id,
                        phase=leg.phase,
                        action=str(canonical.action),
                        rank_position=Decimal(str(leg.rank)) if leg.rank is not None else None,
                        allocation_hint=Decimal(str(leg.allocation_hint)),
                    )
                )
            session.flush()
            self._maybe_commit(session)  # type: ignore[attr-defined]

    def list_account_rebalance_plan_ids(self, model_rebalance_id: str) -> list[str]:
        """Return existing plans in deterministic account/strategy order."""

        if app_models is None:
            return []
        with self._session() as session:  # type: ignore[attr-defined]
            rows = (
                session.query(app_models.AccountRebalancePlan.account_plan_id)
                .filter_by(model_rebalance_id=model_rebalance_id)
                .order_by(
                    app_models.AccountRebalancePlan.user_id,
                    app_models.AccountRebalancePlan.broker_account_id,
                    app_models.AccountRebalancePlan.strategy_id,
                    app_models.AccountRebalancePlan.binding_id,
                )
                .all()
            )
        return [str(row[0]) for row in rows]

    def persist_account_rebalance_plan(self, draft: AccountRebalancePlanDraft) -> str:
        """Persist one frozen account plan and its complete target dispositions."""

        if app_models is None:
            message = "Application models are unavailable"
            raise RuntimeError(message)
        command = draft.command
        with self._session() as session:  # type: ignore[attr-defined]
            existing = session.get(app_models.AccountRebalancePlan, command.account_plan_id)
            if existing is not None:
                if str(existing.content_sha256) != command.content_sha256:
                    message = "Account rebalance plan identity conflicts with frozen content"
                    raise ValueError(message)
                return str(existing.account_plan_id)
            policy = command.execution_policy.model_dump(mode="json")
            route = command.broker_route.model_dump(mode="json")
            session.add(
                app_models.AccountRebalancePlan(
                    account_plan_id=command.account_plan_id,
                    model_rebalance_id=command.model_rebalance_id,
                    user_id=command.user_id,
                    binding_id=command.binding_id,
                    broker_account_id=command.broker_route.broker_account_id,
                    strategy_id=command.strategy_id,
                    data_use_scope=command.data_use_scope,
                    provider_authority_sha256=command.provider_authority_sha256,
                    decision_cutoff=command.decision_cutoff,
                    execute_not_before=command.execute_not_before,
                    execution_session_sha256=command.execution_session_sha256,
                    expires_at=command.expires_at,
                    execution_policy=policy,
                    broker_route=route,
                    execution_policy_sha256=canonical_json_hash(policy),
                    broker_route_sha256=canonical_json_hash(route),
                    content_sha256=command.content_sha256,
                    model_leg_count=draft.model_leg_count,
                    execution_leg_count=command.expected_leg_count,
                    intentional_cash_slots=command.intentional_cash_slots,
                    progress_deadline_at=command.execute_not_before + _PLAN_PROGRESS_DEADLINE,
                )
            )
            session.flush()
            for leg in draft.legs:
                session.add(
                    app_models.AccountRebalancePlanLeg(
                        account_plan_id=command.account_plan_id,
                        user_id=command.user_id,
                        broker_account_id=command.broker_route.broker_account_id,
                        model_sequence=leg.model_sequence,
                        command_sequence=leg.command_sequence,
                        model_rebalance_id=command.model_rebalance_id,
                        model_leg_id=leg.model_leg_id,
                        model_signal_snapshot_sha256=leg.model_signal_snapshot_sha256,
                        plan_leg_id=leg.plan_leg_id,
                        instr_id=leg.instrument_id,
                        external_signal_id=leg.external_signal_id,
                        symbol=leg.symbol,
                        phase=leg.phase,
                        action=leg.action,
                        disposition=leg.disposition,
                        rank_position=(Decimal(str(leg.rank)) if leg.rank is not None else None),
                        allocation_hint=Decimal(str(leg.allocation_hint)),
                        required=leg.required,
                        depends_on_sequences=list(leg.depends_on_sequences),
                        reason_code=leg.reason_code,
                    )
                )
            session.flush()
            self._maybe_commit(session)  # type: ignore[attr-defined]
        return command.account_plan_id


__all__ = ["_RebalanceStoreOps"]
