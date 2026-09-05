"""Quarterly, long-only US quality-compounder portfolio core.

The core consumes one complete point-in-time panel. It does not acquire data,
size an account, convert currency, or place broker orders.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, NoReturn

from USQualityCompounder.panel import (
    USQualityCompounderPanelInput,
    panel_input_from_payload,
    panel_input_to_payload,
)

from lib_common.hashing import canonical_json_hash
from lib_strategy.cross_sectional import CrossSectionalRanker, CrossSectionalSnapshot
from lib_strategy.equity_quality_compounder import (
    QUALITY_COMPOUNDER_CALCULATION_VERSION,
    QUALITY_COMPOUNDER_MINIMUM_PEER_COUNT,
    QUALITY_COMPOUNDER_PEER_TAXONOMY_VERSION,
    QUALITY_COMPOUNDER_REQUIRED_FACTOR_VERSIONS,
    QUALITY_COMPOUNDER_UNIVERSE,
    QUALITY_COMPOUNDER_UNIVERSE_CONTRACT,
    QUALITY_COMPOUNDER_WINSORIZE_LIMIT,
    QualityCompounderExit,
    QualityCompounderHolding,
    QualityCompounderPolicy,
    QualityCompounderPosition,
    QualityCompounderSelection,
    quality_compounder_configuration_sha256,
    select_quality_compounders,
)
from lib_strategy.panels import (
    PanelAuditDecision,
    PanelEvaluationAudit,
    PanelEvaluationRow,
    PanelReadyInput,
    evaluate_panel_readiness,
)
from lib_strategy.signals.emitter import SignalEmitter
from lib_strategy.signals.pure_strategy import (
    MarketState,
    ModelStateContractError,
    PureSignalStrategy,
    StrategyState,
)
from lib_strategy.signals.signal import SignalAction

_ASSET_CLASS = "equity"
_EVALUATION_HORIZON = "3m"
_HORIZON_DAYS = 63.0
_TARGET_WEIGHT_DRIFT_FRACTION = 0.0
_HOLDING_CUSTOM_FIELDS = frozenset(
    {"entity_id", "factor_snapshot_id", "industry", "instrument_id", "sector"}
)


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)


def _state_invalid(message: str) -> NoReturn:
    raise ModelStateContractError(message)


class USQualityCompounderCore(PureSignalStrategy):
    """Select a concentrated target book at a complete quarter-end cutoff."""

    def __init__(
        self,
        strategy_id: str = "us_quality_compounder_v1",
        strategy_type: str = "indicator",
        config: dict[str, Any] | None = None,
        emitter: SignalEmitter | None = None,
    ) -> None:
        super().__init__(
            strategy_id=strategy_id,
            strategy_type=strategy_type,
            config=config,
            emitter=emitter,
        )
        self._policy = QualityCompounderPolicy()
        self._strategy_version = _required_text(
            self.config.get("strategy_version"),
            field_name="strategy_version",
        )
        _require_exact_config(self.config, policy=self._policy)
        self._configuration_sha256 = quality_compounder_configuration_sha256(
            self._strategy_version,
            selection_policy=self._policy,
        )

    @property
    def configuration_sha256(self) -> str:
        """Return the immutable model/configuration identity."""

        return self._configuration_sha256

    def initialize(self) -> None:
        """Initialize a panel-only core without a per-symbol warmup."""

        self.warmup_bars_needed = 0

    def on_data(self, state: MarketState) -> None:
        """Ignore individual bars; only a complete panel may trigger a decision."""

        del state

    def panel_ready_input(self, panel_input: object) -> PanelReadyInput:
        """Expose the generic readiness boundary to the shared runtime."""

        if not isinstance(panel_input, USQualityCompounderPanelInput):
            _invalid("quality-compounder panel input has an incompatible type")
        return panel_input.panel

    def serialize_panel_input(self, panel_input: object) -> Mapping[str, Any]:
        """Encode the exact strategy input for durable replay."""

        if not isinstance(panel_input, USQualityCompounderPanelInput):
            _invalid("quality-compounder panel input has an incompatible type")
        return panel_input_to_payload(panel_input)

    def deserialize_panel_input(self, payload: Mapping[str, Any]) -> object:
        """Restore the exact strategy input through the same invariants."""

        return panel_input_from_payload(payload)

    def evaluate_panel(
        self,
        panel_input: USQualityCompounderPanelInput,
    ) -> PanelEvaluationAudit:
        """Rank, select, emit one full target book, and advance model state."""

        if not isinstance(panel_input, USQualityCompounderPanelInput):
            _invalid("quality-compounder panel input has an incompatible type")
        self._ensure_initialized()
        evaluate_panel_readiness(panel_input.panel).require_complete()
        decision_session = panel_input.panel.session.session_date
        execution_session = panel_input.panel.execution_session.session_date
        if (decision_session.year, (decision_session.month - 1) // 3) == (
            execution_session.year,
            (execution_session.month - 1) // 3,
        ):
            _invalid("quality-compounder evaluation requires the final official quarter session")

        incumbents = self._current_holdings()
        rank_snapshot = CrossSectionalRanker(
            minimum_peer_count=QUALITY_COMPOUNDER_MINIMUM_PEER_COUNT,
            winsorize_limit=QUALITY_COMPOUNDER_WINSORIZE_LIMIT,
            calculation_version=QUALITY_COMPOUNDER_CALCULATION_VERSION,
        ).rank(
            cutoff=panel_input.panel.cutoff,
            entities=panel_input.entities,
            factor_specs=self._policy.factor_specs,
            observations=panel_input.factor_observations,
        )
        if len(rank_snapshot.ranks) != len(panel_input.securities):
            _invalid("observed complete factor snapshots must produce complete ranks")
        selection = select_quality_compounders(
            snapshot=rank_snapshot,
            securities=panel_input.securities,
            incumbents=incumbents,
            entries_allowed=panel_input.entries_allowed,
            policy=self._policy,
        )
        audit = _build_audit(
            panel_input,
            rank_snapshot=rank_snapshot,
            selection=selection,
            incumbents=incumbents,
            configuration_sha256=self._configuration_sha256,
        )
        input_sha256 = canonical_json_hash(panel_input_to_payload(panel_input))
        self._emit_target_book(
            panel_input,
            rank_snapshot=rank_snapshot,
            selection=selection,
            input_sha256=input_sha256,
        )
        self._apply_selection(selection, cutoff=panel_input.panel.cutoff)
        return audit

    def _ensure_initialized(self) -> None:
        if not self._is_initialized:
            self.initialize()
            self._is_initialized = True

    def _current_holdings(self) -> tuple[QualityCompounderHolding, ...]:
        holdings = tuple(
            _holding_from_state(symbol, state)
            for symbol, state in sorted(self._symbol_states.items())
            if state.position == 1
        )
        return tuple(sorted(holdings, key=lambda item: item.symbol))

    def _emit_target_book(
        self,
        panel_input: USQualityCompounderPanelInput,
        *,
        rank_snapshot: CrossSectionalSnapshot,
        selection: QualityCompounderSelection,
        input_sha256: str,
    ) -> None:
        security_by_id = {item.entity_id: item for item in panel_input.securities}
        rank_by_id = {item.entity_id: item for item in rank_snapshot.ranks}
        member_ids = {item.security_id for item in panel_input.panel.members}
        excluded_ids = {item.security_id for item in panel_input.panel.exclusions}
        ordered: list[tuple[str, object]] = [
            ("exit", item)
            for item in sorted(selection.exits, key=lambda value: value.holding.symbol)
        ]
        ordered.extend(
            ("hold" if item.incumbent else "entry", item) for item in selection.positions
        )
        expected_count = len(ordered)
        for sequence, (phase, item) in enumerate(ordered):
            if phase == "exit":
                if not isinstance(item, QualityCompounderExit):
                    _state_invalid("quality-compounder exit sequence is invalid")
                holding = item.holding
                current = security_by_id.get(holding.entity_id)
                current_rank = rank_by_id.get(holding.entity_id)
                current_row_present = holding.entity_id in member_ids
                current_factor_snapshot_id = (
                    current.factor_snapshot_id
                    if current is not None and current_rank is not None
                    else None
                )
                factor_snapshot_id = current_factor_snapshot_id or holding.factor_snapshot_id
                membership_reason = None if current_row_present else "left_effective_universe"
                if current_row_present and current_rank is None:
                    membership_reason = (
                        "panel_excluded"
                        if holding.entity_id in excluded_ids
                        else "factor_evidence_incomplete"
                    )
                self._emit_model_signal(
                    panel_input,
                    action=SignalAction.CLOSE,
                    entity_id=holding.entity_id,
                    symbol=holding.symbol,
                    instrument_id=holding.instrument_id,
                    sector=holding.sector,
                    industry=holding.industry,
                    reference_price=(current.reference_price if current is not None else None),
                    confidence=0.0,
                    allocation=None,
                    factor_snapshot_id=factor_snapshot_id,
                    current_factor_snapshot_id=current_factor_snapshot_id,
                    current_rank_row_present=current_row_present,
                    membership_change_reason=membership_reason,
                    rank=(current_rank.rank if current_rank is not None else None),
                    composite_score=(
                        current_rank.composite_score if current_rank is not None else None
                    ),
                    reason=item.reason,
                    phase=phase,
                    sequence=sequence,
                    expected_count=expected_count,
                    intentional_cash_slots=selection.intentional_cash_slots,
                    rank_snapshot_sha256=rank_snapshot.content_digest,
                    input_sha256=input_sha256,
                )
                continue

            if not isinstance(item, QualityCompounderPosition):
                _state_invalid("quality-compounder target sequence is invalid")
            position = item
            security = position.security
            action = SignalAction.HOLD if phase == "hold" else SignalAction.LONG
            self._emit_model_signal(
                panel_input,
                action=action,
                entity_id=security.entity_id,
                symbol=security.symbol,
                instrument_id=security.instrument_id,
                sector=security.sector,
                industry=security.industry,
                reference_price=security.reference_price,
                confidence=_rank_confidence(
                    position,
                    ranked_count=len(rank_snapshot.ranks),
                ),
                allocation=position.target_weight,
                factor_snapshot_id=security.factor_snapshot_id,
                current_factor_snapshot_id=security.factor_snapshot_id,
                current_rank_row_present=True,
                membership_change_reason=None,
                rank=position.rank,
                composite_score=position.composite_score,
                reason=("qualified_incumbent" if phase == "hold" else "qualified_entry"),
                phase=phase,
                sequence=sequence,
                expected_count=expected_count,
                intentional_cash_slots=selection.intentional_cash_slots,
                rank_snapshot_sha256=rank_snapshot.content_digest,
                input_sha256=input_sha256,
            )

    def _emit_model_signal(
        self,
        panel_input: USQualityCompounderPanelInput,
        *,
        action: SignalAction,
        entity_id: str,
        symbol: str,
        instrument_id: int,
        sector: str,
        industry: str,
        reference_price: float | None,
        confidence: float,
        allocation: float | None,
        factor_snapshot_id: str,
        current_factor_snapshot_id: str | None,
        current_rank_row_present: bool,
        membership_change_reason: str | None,
        rank: float | None,
        composite_score: float | None,
        reason: str,
        phase: str,
        sequence: int,
        expected_count: int,
        intentional_cash_slots: int,
        rank_snapshot_sha256: str,
        input_sha256: str,
    ) -> None:
        panel = panel_input.panel
        self.emit_signal(
            action=action,
            symbol=symbol,
            confidence=confidence,
            timestamp=panel.cutoff,
            entry_price=reference_price,
            horizon=_EVALUATION_HORIZON,
            horizon_days=_HORIZON_DAYS,
            size_hint=allocation,
            asset_class=_ASSET_CLASS,
            sector=sector,
            industry=industry,
            instrument_id=instrument_id,
            strategy_version=self._strategy_version,
            external_signal_id=f"usqc:{input_sha256}:{sequence}",
            expires_at=panel.execution_session.closes_at,
            metadata={
                "confidence_semantics": "rank_percentile_not_probability",
                "configuration_sha256": self._configuration_sha256,
                "composite_score": composite_score,
                "current_rank_factor_snapshot_id": current_factor_snapshot_id,
                "current_rank_row_present": current_rank_row_present,
                "data_use_scope": panel.data_use_scope.value,
                "decision_reason": reason,
                "execute_not_before": panel.execution_session.opens_at.isoformat(),
                "execution_session_sha256": panel.execution_session.content_sha256,
                "exit_reason": reason if action is SignalAction.CLOSE else None,
                "factor_snapshot_id": factor_snapshot_id,
                "intentional_cash_slots": intentional_cash_slots,
                "membership_change_reason": membership_change_reason,
                "model_rebalance_expected_leg_count": expected_count,
                "model_rebalance_phase": phase,
                "model_rebalance_sequence": sequence,
                "provider_authority_sha256": panel.provider_authority_sha256,
                "rank": rank,
                "rank_snapshot_sha256": rank_snapshot_sha256,
                "security_id": entity_id,
                "strategy_input_sha256": input_sha256,
                "target_weight_drift_fraction": _TARGET_WEIGHT_DRIFT_FRACTION,
            },
        )

    def _apply_selection(
        self,
        selection: QualityCompounderSelection,
        *,
        cutoff: datetime,
    ) -> None:
        next_states: dict[str, StrategyState] = {}
        for position in selection.positions:
            security = position.security
            previous = self._symbol_states.get(security.symbol)
            next_states[security.symbol] = StrategyState(
                position=1,
                entry_price=(
                    previous.entry_price
                    if position.incumbent and previous is not None
                    else security.reference_price
                ),
                entry_time=(
                    previous.entry_time if position.incumbent and previous is not None else cutoff
                ),
                bars_in_trade=(previous.bars_in_trade if previous is not None else 0),
                custom={
                    "entity_id": security.entity_id,
                    "factor_snapshot_id": security.factor_snapshot_id,
                    "industry": security.industry,
                    "instrument_id": security.instrument_id,
                    "sector": security.sector,
                },
            )
        self._symbol_states = next_states

    def _validate_restored_model_state(
        self,
        symbol_states: Mapping[str, StrategyState],
    ) -> None:
        for symbol, state in symbol_states.items():
            if state.position not in {0, 1}:
                _state_invalid("quality-compounder model state must be long-only")
            if state.position == 0:
                if state.custom:
                    _state_invalid("flat quality-compounder state cannot carry holding evidence")
                continue
            if state.entry_price is None or state.entry_time is None:
                _state_invalid("quality-compounder holding lacks entry attribution")
            _holding_from_state(symbol, state)


def _build_audit(
    panel_input: USQualityCompounderPanelInput,
    *,
    rank_snapshot: CrossSectionalSnapshot,
    selection: QualityCompounderSelection,
    incumbents: Sequence[QualityCompounderHolding],
    configuration_sha256: str,
) -> PanelEvaluationAudit:
    rank_by_id = {item.entity_id: item for item in rank_snapshot.ranks}
    security_by_id = {item.entity_id: item for item in panel_input.securities}
    position_by_id = {item.security.entity_id: item for item in selection.positions}
    incumbent_ids = {item.entity_id for item in incumbents}
    exclusion_reasons = dict(selection.exclusion_reasons)
    panel_exclusions = {item.security_id for item in panel_input.panel.exclusions}
    rows: list[PanelEvaluationRow] = []
    for member in panel_input.panel.members:
        entity_id = member.security_id
        rank = rank_by_id.get(entity_id)
        position = position_by_id.get(entity_id)
        if rank is None:
            rows.append(
                PanelEvaluationRow(
                    entity_id=entity_id,
                    symbol=member.canonical_symbol,
                    instrument_id=member.instrument_id,
                    factor_snapshot_id=None,
                    rank_complete=False,
                    strategy_eligible=False,
                    decision=PanelAuditDecision.EXCLUDED,
                    incumbent=entity_id in incumbent_ids,
                    rank=None,
                    composite_score=None,
                    target_allocation_hint=None,
                    exclusion_reason=(
                        "panel_excluded"
                        if entity_id in panel_exclusions
                        else exclusion_reasons.get(entity_id, "factor_evidence_incomplete")
                    ),
                )
            )
            continue
        security = security_by_id[entity_id]
        selected = position is not None
        if position is None:
            decision = PanelAuditDecision.EXCLUDED
        elif position.incumbent:
            decision = PanelAuditDecision.HOLD
        else:
            decision = PanelAuditDecision.SELECTED
        rows.append(
            PanelEvaluationRow(
                entity_id=entity_id,
                symbol=member.canonical_symbol,
                instrument_id=member.instrument_id,
                factor_snapshot_id=security.factor_snapshot_id,
                rank_complete=True,
                strategy_eligible=selected,
                decision=decision,
                incumbent=entity_id in incumbent_ids,
                rank=rank.rank,
                composite_score=rank.composite_score,
                target_allocation_hint=(position.target_weight if position is not None else None),
                exclusion_reason=(
                    None if selected else exclusion_reasons.get(entity_id, "not_selected")
                ),
            )
        )
    return PanelEvaluationAudit(
        rank_snapshot=rank_snapshot,
        rows=tuple(sorted(rows, key=lambda item: (item.symbol, item.entity_id))),
        configuration_sha256=configuration_sha256,
        peer_taxonomy_version=QUALITY_COMPOUNDER_PEER_TAXONOMY_VERSION,
        replayed=False,
        intentional_cash_slots=selection.intentional_cash_slots,
        required_factor_versions=QUALITY_COMPOUNDER_REQUIRED_FACTOR_VERSIONS,
        strategy_result=selection,
    )


def _holding_from_state(symbol: str, state: StrategyState) -> QualityCompounderHolding:
    if set(state.custom) != _HOLDING_CUSTOM_FIELDS:
        _state_invalid("quality-compounder holding evidence has an incompatible field set")
    instrument_id = state.custom.get("instrument_id")
    if isinstance(instrument_id, bool) or not isinstance(instrument_id, int):
        _state_invalid("quality-compounder holding has an invalid instrument_id")
    try:
        return QualityCompounderHolding(
            entity_id=_required_text(state.custom.get("entity_id"), field_name="entity_id"),
            instrument_id=instrument_id,
            symbol=symbol,
            factor_snapshot_id=_required_text(
                state.custom.get("factor_snapshot_id"),
                field_name="factor_snapshot_id",
            ),
            sector=_required_text(state.custom.get("sector"), field_name="sector"),
            industry=_required_text(state.custom.get("industry"), field_name="industry"),
        )
    except (TypeError, ValueError) as exc:
        message = "quality-compounder holding evidence is invalid"
        raise ModelStateContractError(message) from exc


def _rank_confidence(
    position: QualityCompounderPosition,
    *,
    ranked_count: int,
) -> float:
    if ranked_count <= 1:
        return 1.0
    return max(0.0, min(1.0, 1.0 - ((position.rank - 1.0) / (ranked_count - 1.0))))


def _require_exact_config(
    config: Mapping[str, Any],
    *,
    policy: QualityCompounderPolicy,
) -> None:
    expected = {
        "asset_class": _ASSET_CLASS,
        "evaluation_horizon": _EVALUATION_HORIZON,
        "trade_direction_mode": "long_only",
        "universe": QUALITY_COMPOUNDER_UNIVERSE,
        "universe_contract": QUALITY_COMPOUNDER_UNIVERSE_CONTRACT,
    }
    for name, expected_value in expected.items():
        if config.get(name) != expected_value:
            _invalid(f"{name} must be {expected_value!r}")
    target_holdings = config.get("target_holdings")
    if isinstance(target_holdings, bool) or not isinstance(target_holdings, (int, str)):
        _invalid("target_holdings must be an integer")
    try:
        parsed_target = int(target_holdings)
    except (TypeError, ValueError) as exc:
        message = "target_holdings must be an integer"
        raise ValueError(message) from exc
    if parsed_target != policy.target_holdings:
        _invalid(f"target_holdings must be {policy.target_holdings}")


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _invalid(f"{field_name} must be a non-blank canonical string")
    return value


__all__ = ["USQualityCompounderCore"]
