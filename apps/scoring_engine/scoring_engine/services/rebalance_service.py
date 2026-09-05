"""Atomic scoring and tenant/account fan-out for model portfolio rebalances."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Final, Literal

from lib_common.hashing import canonical_json_hash
from lib_common.internal_events import (
    ModelRebalanceLegSnapshot,
    ModelRebalanceSubmissionEvent,
    RebalanceExecutionCommandEvent,
    RebalanceExecutionLegSnapshot,
    compute_account_plan_id,
    compute_rebalance_execution_content_sha256,
)
from lib_common.paper_promotion import (
    PaperPromotionModelContext,
    canonical_paper_broker_code,
)
from lib_data.market_data import normalize_product_symbol
from lib_strategy.signals.signal import Signal, SignalAction
from scoring_engine.binding_evaluation import matches_exact_promotion_values
from scoring_engine.dispatcher import (
    DispatchProviderContexts,
    ExecutionDispatcher,
    FrozenBindingContext,
)
from scoring_engine.engine import ScoreEngine
from scoring_engine.models import (
    AccountRebalancePlanDraft,
    AccountRebalancePlanLegDraft,
    ScoreRecord,
    ScoringUserBinding,
    TriggerDecision,
)
from scoring_engine.pipeline_events import enqueue_scoring_pipeline_events
from scoring_engine.storage import ScoreStore

_PLAN_TTL: Final = timedelta(days=7)
_REASON_LIMIT: Final = 100
_ExecutablePhase = Literal["exit", "reduce", "entry"]


@dataclass(frozen=True)
class RebalanceIngestionResult:
    """Stable result for a newly ingested or exactly replayed model batch."""

    rebalance_id: str
    replayed: bool
    canonical_signal_count: int
    plan_ids: tuple[str, ...]
    outbox_event_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "rebalance_id": self.rebalance_id,
            "replayed": self.replayed,
            "canonical_signal_count": self.canonical_signal_count,
            "plan_ids": list(self.plan_ids),
            "outbox_event_ids": list(self.outbox_event_ids),
        }


@dataclass(frozen=True)
class _Assessment:
    leg: ModelRebalanceLegSnapshot
    signal: Signal
    score: ScoreRecord
    phase: _ExecutablePhase
    action: str
    disposition: str
    allocation_hint: float
    reason_code: str
    trigger: TriggerDecision | None
    creates_cash_slot: bool = False

    @property
    def selected(self) -> bool:
        return self.disposition == "selected"


def _binding_sort_key(binding: ScoringUserBinding) -> tuple[str, int, str, int]:
    return (
        binding.user_id,
        int(binding.broker_account_id or 0),
        str(binding.strategy_id or ""),
        int(binding.binding_id or 0),
    )


def _reason_code(value: str) -> str:
    normalized = "_".join(value.strip().lower().split()) or "unspecified"
    return normalized[:_REASON_LIMIT]


def _threshold_reason(decision: TriggerDecision | None) -> str:
    if decision is None:
        return "binding_scope_rejected"
    reason = decision.reason.lower()
    for token, code in (
        ("asset score", "asset_score_threshold"),
        ("sector score", "sector_score_threshold"),
        ("market score", "market_score_threshold"),
        ("scoring abstained", "scoring_abstained"),
        ("entry authority", "entry_authority_disabled"),
        ("manual approval", "manual_approval_required"),
        ("conflicts with", "direction_conflict"),
    ):
        if token in reason:
            return code
    return _reason_code(decision.reason)


class RebalanceScoringService:
    """Persist a complete model batch and fan it out to exact paper bindings."""

    def __init__(
        self,
        *,
        engine: ScoreEngine,
        dispatcher: ExecutionDispatcher,
        plan_ttl: timedelta = _PLAN_TTL,
    ) -> None:
        if plan_ttl <= timedelta(0):
            message = "Rebalance plan_ttl must be positive"
            raise ValueError(message)
        self._engine = engine
        self._store: ScoreStore = engine.store
        self._dispatcher = dispatcher
        self._plan_ttl = plan_ttl

    def preflight_exact_replay(
        self,
        event: ModelRebalanceSubmissionEvent,
    ) -> RebalanceIngestionResult | None:
        """Return an exact persisted replay without resolving tenant providers.

        The immutable event and every frozen signal are still fully validated.
        A genuinely new batch returns ``None`` so the API can resolve the
        tenant/account provider context needed to build its account plans.
        """

        self._require_paper_forward(event)
        self._require_promoted_model_scope(event)
        self._validated_signals(event)
        with self._store.unit_of_work():
            if self._store.classify_model_rebalance(event) != "replay":
                return None
            self._store.resolve_model_rebalance_entitlement_owner(event)
            if self._engine.paper_promotion_required:
                # The API must resolve current provider/account authority before
                # a promoted replay can be returned.
                return None
            return self._replay_result(event)

    def ingest(
        self,
        event: ModelRebalanceSubmissionEvent,
        *,
        provider_contexts: DispatchProviderContexts,
    ) -> RebalanceIngestionResult:
        """Score, persist, plan, and enqueue one batch in a single unit of work."""

        self._require_paper_forward(event)
        promotion_context = self._require_promoted_model_scope(event)
        signals = self._validated_signals(event)
        with self._store.unit_of_work():
            classification = self._store.classify_model_rebalance(event)
            if classification == "replay":
                entitlement_owner_user_id = self._store.resolve_model_rebalance_entitlement_owner(
                    event
                )
                if self._engine.paper_promotion_required:
                    bindings = self._exact_bindings(
                        event,
                        entitlement_owner_user_id=entitlement_owner_user_id,
                    )
                    binding = bindings[0]
                    scope = self._engine.paper_promotion_scope
                    if scope is None:
                        msg = "Cloud-paper model rebalance has no promotion authority"
                        raise ValueError(msg)
                    frozen = self._dispatcher.freeze_portfolio_binding_context_resolved(
                        decision=self._binding_trigger(binding),
                        strategy_id=event.strategy_id,
                        asset_class=scope.asset_class,
                        provider_contexts=provider_contexts,
                    )
                    self._validate_frozen_paper_binding(
                        frozen,
                        binding,
                        expected_broker=scope.broker_code,
                    )
                return self._replay_result(event)
            entitlement_owner_user_id = self._store.resolve_model_rebalance_entitlement_owner(event)
            scores = self._engine.ingest_signals(
                signals,
                scoring_action_overrides={
                    signal.signal_id: SignalAction.LONG
                    for leg, signal in zip(event.legs, signals, strict=True)
                    if leg.phase == "hold"
                },
            )
            if len(scores) != event.expected_leg_count:
                message = "Scoring did not produce one result for every model leg"
                raise ValueError(message)
            self._store.persist_model_rebalance(event)
            plan_ids: list[str] = []
            outbox_ids: list[str] = []
            queued_users_by_signal: dict[str, list[str]] = {
                str(signal.external_signal_id): [] for signal in signals
            }
            for binding in self._exact_bindings(
                event,
                entitlement_owner_user_id=entitlement_owner_user_id,
            ):
                draft = self._build_plan(
                    event=event,
                    binding=binding,
                    signals=signals,
                    scores=scores,
                    provider_contexts=provider_contexts,
                    promotion_context=promotion_context,
                )
                plan_ids.append(self._store.persist_account_rebalance_plan(draft))
                outbox_ids.append(self._dispatcher.enqueue_rebalance(draft.command))
                for command_leg in draft.command.legs:
                    queued_users_by_signal[command_leg.external_signal_id].append(binding.user_id)
            for signal, score in zip(signals, scores, strict=True):
                enqueue_scoring_pipeline_events(
                    store=self._store,
                    signal=signal,
                    score=score,
                    queued_users=queued_users_by_signal[str(signal.external_signal_id)],
                )
        return RebalanceIngestionResult(
            rebalance_id=event.rebalance_id,
            replayed=False,
            canonical_signal_count=len(signals),
            plan_ids=tuple(plan_ids),
            outbox_event_ids=tuple(outbox_ids),
        )

    def _replay_result(self, event: ModelRebalanceSubmissionEvent) -> RebalanceIngestionResult:
        plan_ids = tuple(self._store.list_account_rebalance_plan_ids(event.rebalance_id))
        scope = self._engine.paper_promotion_scope
        if self._engine.paper_promotion_required:
            if scope is None:
                msg = "Cloud-paper model rebalance has no promotion authority"
                raise ValueError(msg)
            expected_plan_id = compute_account_plan_id(
                model_rebalance_id=event.rebalance_id,
                user_id=scope.user_id,
                binding_id=scope.strategy_binding_id,
                broker_account_id=scope.broker_account_id,
                data_use_scope=event.data_use_scope,
                provider_authority_sha256=event.provider_authority_sha256,
                execute_not_before=event.execute_not_before,
                execution_session_sha256=event.execution_session_sha256,
            )
            if plan_ids != (expected_plan_id,):
                msg = "Persisted replay plans differ from the exact paper promotion route"
                raise ValueError(msg)
        return RebalanceIngestionResult(
            rebalance_id=event.rebalance_id,
            replayed=True,
            canonical_signal_count=event.expected_leg_count,
            plan_ids=plan_ids,
            outbox_event_ids=(),
        )

    @staticmethod
    def _require_paper_forward(event: ModelRebalanceSubmissionEvent) -> None:
        if event.data_use_scope != "paper_forward":
            message = (
                "Operational rebalance scoring accepts paper_forward batches only; "
                "historical_validation and live_forward cannot materialize account plans"
            )
            raise ValueError(message)

    @staticmethod
    def _validated_signals(event: ModelRebalanceSubmissionEvent) -> tuple[Signal, ...]:
        signals: list[Signal] = []
        expected_source = "paper"
        metadata_fields = {
            "configuration_sha256": event.configuration_sha256,
            "strategy_input_sha256": event.input_snapshot_sha256,
            "rank_snapshot_sha256": event.rank_snapshot_sha256,
            "provider_authority_sha256": event.provider_authority_sha256,
            "data_use_scope": event.data_use_scope,
            "execution_session_sha256": event.execution_session_sha256,
            "execute_not_before": event.execute_not_before.isoformat(),
        }
        for leg in event.legs:
            signal = Signal.from_dict(leg.signal.model_dump(mode="json"))
            if signal.strategy_version != event.strategy_version:
                message = f"Model leg {leg.sequence} strategy version is inconsistent"
                raise ValueError(message)
            if signal.timestamp != event.decision_cutoff:
                message = f"Model leg {leg.sequence} timestamp is not the decision cutoff"
                raise ValueError(message)
            if signal.source != expected_source:
                message = f"Model leg {leg.sequence} is not a paper-forward signal"
                raise ValueError(message)
            expected_action = {
                "exit": SignalAction.CLOSE,
                "entry": SignalAction.LONG,
                "hold": SignalAction.HOLD,
                "reduce": SignalAction.HOLD,
            }.get(leg.phase)
            if expected_action is not None and signal.action is not expected_action:
                message = f"Model leg {leg.sequence} action/phase is inconsistent"
                raise ValueError(message)
            for key, expected in metadata_fields.items():
                if signal.metadata.get(key) != expected:
                    message = f"Model leg {leg.sequence} metadata {key} is inconsistent"
                    raise ValueError(message)
            if signal.metadata.get("factor_snapshot_id") != leg.factor_snapshot_id:
                message = f"Model leg {leg.sequence} factor metadata is inconsistent"
                raise ValueError(message)
            if signal.metadata.get("current_rank_row_present") is not leg.current_rank_row_present:
                message = f"Model leg {leg.sequence} current-rank presence is inconsistent"
                raise ValueError(message)
            if (
                signal.metadata.get("current_rank_factor_snapshot_id")
                != leg.current_rank_factor_snapshot_id
            ):
                message = f"Model leg {leg.sequence} current factor metadata is inconsistent"
                raise ValueError(message)
            signals.append(signal)
        return tuple(signals)

    def _require_promoted_model_scope(
        self,
        event: ModelRebalanceSubmissionEvent,
    ) -> PaperPromotionModelContext | None:
        """Require the exact manifested synchronized model before persistence."""

        if not self._engine.paper_promotion_required:
            return None
        scope = self._engine.paper_promotion_scope
        if scope is None:
            msg = "Cloud-paper model rebalance has no promotion authority"
            raise ValueError(msg)
        if not scope.is_synchronized_portfolio:
            msg = "Single-instrument promotion cannot authorize a model rebalance"
            raise ValueError(msg)

        authorized_instruments = dict(scope.instruments)
        observed_instruments: dict[int, str] = {}
        for leg in event.legs:
            raw_instrument_id = leg.signal.instrument_id
            try:
                instrument_id = int(raw_instrument_id or 0)
            except (TypeError, ValueError) as exc:
                msg = "Promoted model leg has no exact instrument identity"
                raise ValueError(msg) from exc
            symbol = str(leg.signal.symbol).strip()
            asset_class = str(leg.signal.asset_class or "").strip().lower()
            if instrument_id <= 0 or not symbol or not asset_class:
                msg = "Promoted model leg has incomplete asset/instrument scope"
                raise ValueError(msg)
            if instrument_id in observed_instruments or symbol in observed_instruments.values():
                msg = "Promoted model rebalance contains duplicate instrument identity"
                raise ValueError(msg)
            if authorized_instruments.get(instrument_id) != symbol:
                msg = "Promoted model leg is outside the authorized instrument allowlist"
                raise ValueError(msg)
            if asset_class != scope.asset_class:
                msg = "Promoted model leg has the wrong asset class"
                raise ValueError(msg)
            observed_instruments[instrument_id] = symbol
        context = PaperPromotionModelContext(
            asset_class=scope.asset_class,
            data_use_scope=event.data_use_scope,
            model_configuration_sha256=event.configuration_sha256,
            instrument_set_sha256=scope.instrument_set_sha256,
        )
        if (
            event.strategy_id != scope.strategy_id
            or event.strategy_version != scope.strategy_version
            or context.asset_class != scope.asset_class
            or context.data_use_scope != scope.data_use_scope
            or context.model_configuration_sha256 != scope.model_configuration_sha256
            or context.instrument_set_sha256 != scope.instrument_set_sha256
        ):
            msg = "Model rebalance differs from the exact paper promotion scope"
            raise ValueError(msg)
        return context

    def _exact_bindings(
        self,
        event: ModelRebalanceSubmissionEvent,
        *,
        entitlement_owner_user_id: str | None,
    ) -> tuple[ScoringUserBinding, ...]:
        bindings = (
            binding
            for binding in self._store.list_bindings()
            if binding.strategy_id == event.strategy_id
            and (entitlement_owner_user_id is None or binding.user_id == entitlement_owner_user_id)
            and binding.binding_id is not None
            and binding.binding_id > 0
            and binding.broker_account_id is not None
            and binding.broker_account_id > 0
            and (binding.entries_enabled or binding.exits_enabled)
        )
        selected = tuple(sorted(bindings, key=_binding_sort_key))
        if not self._engine.paper_promotion_required:
            return selected
        scope = self._engine.paper_promotion_scope
        if scope is None:
            msg = "Cloud-paper model rebalance has no promotion authority"
            raise ValueError(msg)
        promoted_symbols = {normalize_product_symbol(symbol) for _, symbol in scope.instruments}
        selected = tuple(
            binding
            for binding in selected
            if binding.user_id == scope.user_id
            and binding.binding_id == scope.strategy_binding_id
            and binding.broker_account_id == scope.broker_account_id
            and matches_exact_promotion_values(
                binding.allowed_brokers,
                expected={scope.broker_code},
                normalize=canonical_paper_broker_code,
            )
            and matches_exact_promotion_values(
                binding.asset_filter,
                expected=promoted_symbols,
                normalize=normalize_product_symbol,
            )
        )
        if len(selected) != 1:
            msg = (
                "Paper promotion requires one active binding with the exact "
                "user/account/broker/instrument scope"
            )
            raise ValueError(msg)
        return selected

    def _assess(
        self,
        *,
        event: ModelRebalanceSubmissionEvent,
        binding: ScoringUserBinding,
        signals: tuple[Signal, ...],
        scores: list[ScoreRecord],
        promotion_context: PaperPromotionModelContext | None,
    ) -> tuple[_Assessment, ...]:
        assessments: list[_Assessment] = []
        for leg, signal, score in zip(event.legs, signals, scores, strict=True):
            if leg.phase == "exit":
                selected = binding.exits_enabled
                assessments.append(
                    _Assessment(
                        leg=leg,
                        signal=signal,
                        score=score,
                        phase="exit",
                        action="exit" if selected else "reject",
                        disposition="selected" if selected else "rejected",
                        allocation_hint=0.0,
                        reason_code=(
                            "risk_reducing_exit" if selected else "exit_authority_disabled"
                        ),
                        trigger=self._binding_trigger(binding, should_execute=selected),
                    )
                )
                continue
            if leg.phase == "reduce":
                selected = binding.exits_enabled
                assessments.append(
                    _Assessment(
                        leg=leg,
                        signal=signal,
                        score=score,
                        phase="reduce",
                        action="reduce" if selected else "reject",
                        disposition="selected" if selected else "rejected",
                        allocation_hint=leg.allocation_hint,
                        reason_code=(
                            "risk_reducing_target" if selected else "exit_authority_disabled"
                        ),
                        trigger=self._binding_trigger(binding, should_execute=selected),
                    )
                )
                continue
            decision = self._target_decision(
                event,
                binding,
                score,
                promotion_context=promotion_context,
            )
            if decision is not None and decision.should_execute:
                assessments.append(
                    _Assessment(
                        leg=leg,
                        signal=signal,
                        score=score,
                        phase="entry",
                        action="entry",
                        disposition="selected",
                        allocation_hint=leg.allocation_hint,
                        reason_code=f"{leg.phase}_target_selected",
                        trigger=decision,
                    )
                )
            elif leg.phase == "hold" and _threshold_reason(decision) in {
                "entry_authority_disabled",
                "manual_approval_required",
            }:
                # A valid incumbent target remains in the account plan when
                # exit authority exists. The execution fence may apply only a
                # negative target delta before its entry phase; frozen policy
                # still prevents a flat/underweight account from increasing.
                # Without exit authority, preserve and audit the hold without
                # creating an executable leg.
                reason = _threshold_reason(decision)
                if binding.exits_enabled:
                    assessments.append(
                        _Assessment(
                            leg=leg,
                            signal=signal,
                            score=score,
                            phase="entry",
                            action="entry",
                            disposition="selected",
                            allocation_hint=leg.allocation_hint,
                            reason_code=f"{reason}_risk_reduction_only",
                            trigger=self._binding_trigger(binding, should_execute=True),
                        )
                    )
                else:
                    assessments.append(
                        _Assessment(
                            leg=leg,
                            signal=signal,
                            score=score,
                            phase="entry",
                            action="reject",
                            disposition="rejected",
                            allocation_hint=leg.allocation_hint,
                            reason_code=f"{reason}_hold_preserved",
                            trigger=decision,
                        )
                    )
            elif leg.phase == "hold" and binding.exits_enabled:
                assessments.append(
                    _Assessment(
                        leg=leg,
                        signal=signal,
                        score=score,
                        phase="reduce",
                        action="reduce",
                        disposition="selected",
                        allocation_hint=0.0,
                        reason_code=f"{_threshold_reason(decision)}_reduction_only",
                        trigger=self._binding_trigger(binding, should_execute=True),
                        creates_cash_slot=True,
                    )
                )
            else:
                assessments.append(
                    _Assessment(
                        leg=leg,
                        signal=signal,
                        score=score,
                        phase="entry",
                        action="reject",
                        disposition="rejected",
                        allocation_hint=0.0,
                        reason_code=_threshold_reason(decision),
                        trigger=decision,
                        creates_cash_slot=True,
                    )
                )
        return self._apply_position_limit(binding, tuple(assessments))

    def _target_decision(
        self,
        event: ModelRebalanceSubmissionEvent,
        binding: ScoringUserBinding,
        score: ScoreRecord,
        *,
        promotion_context: PaperPromotionModelContext | None,
    ) -> TriggerDecision | None:
        # Model rebalances are frozen at ``decision_cutoff``. The ordinary
        # hierarchy read models expose only "latest" aggregates and therefore
        # cannot prove that their constituents were available at that cutoff.
        # Never let a newer sector/market aggregate decide a delayed portfolio
        # batch. Until immutable cutoff-bound hierarchy snapshots are part of
        # the plan lineage, bindings that request those thresholds retain
        # risk-reducing legs but fail closed for target exposure.
        if binding.sector_score_threshold is not None or binding.market_score_threshold is not None:
            return TriggerDecision(
                user_id=binding.user_id,
                should_execute=False,
                reason="Point-in-time hierarchy score context unavailable",
                binding_id=binding.binding_id,
                broker_account_id=binding.broker_account_id,
                execution_mode=binding.execution_mode,
                sizing_profile=binding.sizing_profile,
                risk_caps=binding.risk_caps,
                allowed_brokers=binding.allowed_brokers,
            )
        decisions = self._engine.evaluate_bindings(
            score,
            bindings=[binding],
            sector_score=None,
            market_score=None,
            signal_strategy_id=event.strategy_id,
            signal_strategy_version=event.strategy_version,
            signal_action=SignalAction.LONG,
            promotion_model_context=promotion_context,
        )
        return decisions[0] if decisions else None

    @staticmethod
    def _apply_position_limit(
        binding: ScoringUserBinding,
        assessments: tuple[_Assessment, ...],
    ) -> tuple[_Assessment, ...]:
        raw_limit = binding.risk_caps.get("max_open_positions")
        if raw_limit is None:
            return assessments
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            message = "max_open_positions must be a positive integer"
            raise ValueError(message) from exc
        if limit <= 0:
            message = "max_open_positions must be a positive integer"
            raise ValueError(message)
        selected_targets = 0
        limited: list[_Assessment] = []
        for item in assessments:
            if not (item.selected and item.phase == "entry"):
                limited.append(item)
                continue
            selected_targets += 1
            if selected_targets <= limit:
                limited.append(item)
            elif item.leg.phase == "hold" and binding.exits_enabled:
                limited.append(
                    replace(
                        item,
                        phase="reduce",
                        action="reduce",
                        allocation_hint=0.0,
                        reason_code="max_open_positions_reduction_only",
                        creates_cash_slot=True,
                    )
                )
            else:
                limited.append(
                    replace(
                        item,
                        action="reject",
                        disposition="rejected",
                        allocation_hint=0.0,
                        reason_code="max_open_positions",
                        creates_cash_slot=True,
                    )
                )
        return tuple(limited)

    def _build_plan(
        self,
        *,
        event: ModelRebalanceSubmissionEvent,
        binding: ScoringUserBinding,
        signals: tuple[Signal, ...],
        scores: list[ScoreRecord],
        provider_contexts: DispatchProviderContexts,
        promotion_context: PaperPromotionModelContext | None,
    ) -> AccountRebalancePlanDraft:
        binding_id = int(binding.binding_id or 0)
        account_id = int(binding.broker_account_id or 0)
        if binding_id <= 0 or account_id <= 0:
            message = "Rebalance fan-out requires an exact persisted binding/account"
            raise ValueError(message)
        assessments = self._assess(
            event=event,
            binding=binding,
            signals=signals,
            scores=scores,
            promotion_context=promotion_context,
        )
        plan_id = compute_account_plan_id(
            model_rebalance_id=event.rebalance_id,
            user_id=binding.user_id,
            binding_id=binding_id,
            broker_account_id=account_id,
            data_use_scope=event.data_use_scope,
            provider_authority_sha256=event.provider_authority_sha256,
            execute_not_before=event.execute_not_before,
            execution_session_sha256=event.execution_session_sha256,
        )
        draft_legs, command_legs = self._plan_legs(plan_id, assessments)
        if assessments:
            representative = next(
                (item for item in assessments if item.selected),
                assessments[0],
            )
            trigger = representative.trigger or self._binding_trigger(binding)
            frozen = self._dispatcher.freeze_binding_context_resolved(
                decision=trigger,
                signal=representative.signal,
                provider_contexts=provider_contexts,
                score_context={
                    "asset_score": representative.score.score,
                    "score_scope": "model_rebalance",
                    "score_target": representative.score.target,
                },
            )
        else:
            frozen = self._dispatcher.freeze_portfolio_binding_context_resolved(
                decision=self._binding_trigger(binding),
                strategy_id=event.strategy_id,
                asset_class=(
                    promotion_context.asset_class if promotion_context is not None else "equity"
                ),
                provider_contexts=provider_contexts,
            )
        self._validate_frozen_paper_binding(
            frozen,
            binding,
            expected_broker=(
                self._engine.paper_promotion_scope.broker_code
                if self._engine.paper_promotion_scope is not None
                else None
            ),
        )
        expires_at = event.execute_not_before + self._plan_ttl
        cash_slots = event.intentional_cash_slots + sum(
            item.creates_cash_slot for item in assessments
        )
        content_sha256 = compute_rebalance_execution_content_sha256(
            account_plan_id=plan_id,
            model_rebalance_id=event.rebalance_id,
            user_id=binding.user_id,
            strategy_id=event.strategy_id,
            binding_id=binding_id,
            broker_account_id=account_id,
            data_use_scope=event.data_use_scope,
            provider_authority_sha256=event.provider_authority_sha256,
            decision_cutoff=event.decision_cutoff,
            execute_not_before=event.execute_not_before,
            execution_session_sha256=event.execution_session_sha256,
            execution_policy=frozen.execution_policy,
            broker_route=frozen.broker_route,
            expires_at=expires_at,
            intentional_cash_slots=cash_slots,
            legs=list(command_legs),
        )
        command = RebalanceExecutionCommandEvent(
            occurred_at=event.occurred_at,
            run_id=event.run_id,
            correlation_id=event.rebalance_id,
            causation_id=event.event_id,
            producer="scoring_engine",
            account_plan_id=plan_id,
            model_rebalance_id=event.rebalance_id,
            user_id=binding.user_id,
            strategy_id=event.strategy_id,
            binding_id=binding_id,
            data_use_scope=event.data_use_scope,
            provider_authority_sha256=event.provider_authority_sha256,
            decision_cutoff=event.decision_cutoff,
            execute_not_before=event.execute_not_before,
            execution_session_sha256=event.execution_session_sha256,
            execution_policy=frozen.execution_policy,
            broker_route=frozen.broker_route,
            expires_at=expires_at,
            content_sha256=content_sha256,
            expected_leg_count=len(command_legs),
            intentional_cash_slots=cash_slots,
            legs=list(command_legs),
        )
        return AccountRebalancePlanDraft(
            command=command,
            model_leg_count=event.expected_leg_count,
            legs=draft_legs,
        )

    @staticmethod
    def _plan_legs(
        plan_id: str,
        assessments: tuple[_Assessment, ...],
    ) -> tuple[tuple[AccountRebalancePlanLegDraft, ...], tuple[RebalanceExecutionLegSnapshot, ...]]:
        selected = sorted(
            (item for item in assessments if item.selected),
            key=lambda item: ({"exit": 0, "reduce": 1, "entry": 2}[item.phase], item.leg.sequence),
        )
        command_sequence = {item.leg.sequence: index for index, item in enumerate(selected)}
        exits = tuple(
            command_sequence[item.leg.sequence]
            for item in selected
            if item.phase in {"exit", "reduce"}
        )
        drafts: list[AccountRebalancePlanLegDraft] = []
        commands: list[RebalanceExecutionLegSnapshot] = []
        for item in assessments:
            sequence = command_sequence.get(item.leg.sequence)
            dependencies = exits if item.selected and item.phase == "entry" else ()
            model_signal_snapshot_sha256 = canonical_json_hash(
                item.leg.signal.model_dump(mode="json")
            )
            plan_leg_id = canonical_json_hash(
                {
                    "schema": "account-rebalance-plan-leg-v1",
                    "account_plan_id": plan_id,
                    "model_leg_id": item.leg.leg_id,
                    "phase": item.phase,
                    "action": item.action,
                    "disposition": item.disposition,
                    "reason_code": item.reason_code,
                }
            )
            instrument_id = int(item.signal.instrument_id or 0)
            drafts.append(
                AccountRebalancePlanLegDraft(
                    model_sequence=item.leg.sequence,
                    command_sequence=sequence,
                    model_leg_id=item.leg.leg_id,
                    model_signal_snapshot_sha256=model_signal_snapshot_sha256,
                    plan_leg_id=plan_leg_id,
                    instrument_id=instrument_id,
                    external_signal_id=str(item.signal.external_signal_id),
                    symbol=item.signal.symbol,
                    phase=item.phase,
                    action=item.action,
                    disposition=item.disposition,
                    rank=item.leg.rank,
                    allocation_hint=item.allocation_hint,
                    required=item.selected,
                    depends_on_sequences=dependencies,
                    reason_code=_reason_code(item.reason_code),
                )
            )
            if sequence is not None:
                commands.append(
                    RebalanceExecutionLegSnapshot(
                        plan_leg_id=plan_leg_id,
                        sequence=sequence,
                        phase=item.phase,
                        model_leg_id=item.leg.leg_id,
                        model_signal_snapshot_sha256=model_signal_snapshot_sha256,
                        external_signal_id=str(item.signal.external_signal_id),
                        symbol=item.signal.symbol,
                        rank=item.leg.rank,
                        allocation_hint=item.allocation_hint,
                        required=True,
                        depends_on_sequences=list(dependencies),
                    )
                )
        commands.sort(key=lambda item: item.sequence)
        return tuple(drafts), tuple(commands)

    @staticmethod
    def _binding_trigger(
        binding: ScoringUserBinding,
        *,
        should_execute: bool | None = None,
    ) -> TriggerDecision:
        return TriggerDecision(
            user_id=binding.user_id,
            should_execute=(binding.autopilot if should_execute is None else should_execute),
            reason="portfolio_rebalance_binding",
            binding_id=binding.binding_id,
            broker_account_id=binding.broker_account_id,
            execution_mode=binding.execution_mode,
            sizing_profile=binding.sizing_profile,
            risk_caps=binding.risk_caps,
            allowed_brokers=binding.allowed_brokers,
        )

    @staticmethod
    def _validate_frozen_paper_binding(
        frozen: FrozenBindingContext,
        binding: ScoringUserBinding,
        *,
        expected_broker: str | None,
    ) -> None:
        route = frozen.broker_route
        policy = frozen.execution_policy
        if (
            route.broker_environment != "paper"
            or route.broker.strip().lower() not in {"paper", "ibkr"}
            or route.live_enabled
            or not route.sandbox
        ):
            message = "Paper-forward rebalance requires a sandboxed paper or IBKR paper route"
            raise ValueError(message)
        if expected_broker is not None and canonical_paper_broker_code(
            route.broker
        ) != canonical_paper_broker_code(expected_broker):
            msg = "Frozen route does not match the promoted paper broker"
            raise ValueError(msg)
        if route.broker_account_id != binding.broker_account_id:
            message = "Frozen route does not match the binding broker account"
            raise ValueError(message)
        if (
            policy.user_id != binding.user_id
            or policy.binding_id != binding.binding_id
            or policy.strategy_id != binding.strategy_id
            or policy.autopilot != binding.autopilot
            or policy.entries_enabled != binding.entries_enabled
            or policy.exits_enabled != binding.exits_enabled
        ):
            message = "Frozen policy does not match the active exact paper binding"
            raise ValueError(message)


__all__ = ["RebalanceIngestionResult", "RebalanceScoringService"]
