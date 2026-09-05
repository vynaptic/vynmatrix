"""User-binding evaluation and execution-mode selection (extracted from ``engine.py``).

``BindingEvaluator`` owns the scoring → execution decision surface: 3-tier
threshold evaluation, strategy/asset scoping, cloud-paper promotion scoping,
wildcard suppression, and mode-performance ranking. ``ScoreEngine`` keeps
``evaluate_bindings()`` / ``select_best_mode()`` as thin delegates so the
documented public surface is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError

from lib_common.logging import get_logger
from lib_common.paper_promotion import (
    PaperPromotionModelContext,
    PaperPromotionScope,
    canonical_paper_broker_code,
)
from lib_data.market_data import normalize_product_symbol
from lib_strategy.scoring.binding_evaluator import (
    BindingEvaluationResult,
    evaluate_binding_thresholds,
)
from lib_strategy.signals.normalization import normalize_scoring_action

from .models import (
    ModePerformance,
    ScoreRecord,
    ScoringUserBinding,
    TriggerDecision,
)
from .storage import ScoreStore

logger = get_logger(__name__)


def matches_exact_promotion_values(
    values: Iterable[str],
    *,
    expected: set[str],
    normalize: Callable[[str], str],
) -> bool:
    """Require one non-wildcard, collision-free normalized binding scope."""

    observed: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or value.strip() == "*":
            return False
        normalized = normalize(value)
        if not normalized or normalized in observed:
            return False
        observed.append(normalized)
    return len(observed) == len(expected) and set(observed) == expected


def _binding_matches_asset(score_target: str, asset_filter: list[str]) -> bool:
    """Return True when the score target is allowed by the binding asset filter."""
    if not asset_filter or "*" in asset_filter:
        return True

    normalized_target = normalize_product_symbol(score_target)
    normalized_filter = {
        normalize_product_symbol(asset) for asset in asset_filter if asset and asset != "*"
    }
    return normalized_target in normalized_filter


def _select_effective_bindings(
    bindings: list[ScoringUserBinding],
    *,
    signal_strategy_id: str | None,
) -> list[ScoringUserBinding]:
    """Prefer strategy bindings within the same user/broker-account route."""
    if not signal_strategy_id:
        return bindings

    routes_with_strategy_binding = {
        (binding.user_id, binding.broker_account_id)
        for binding in bindings
        if binding.strategy_id == signal_strategy_id
    }
    if not routes_with_strategy_binding:
        return bindings

    selected: list[ScoringUserBinding] = []
    for binding in bindings:
        route = (binding.user_id, binding.broker_account_id)
        if route in routes_with_strategy_binding and binding.strategy_id is None:
            continue
        selected.append(binding)
    return selected


@dataclass
class _BindingEvaluationContext:
    """Inputs and per-evaluation cache shared across binding decisions."""

    score: ScoreRecord
    sector_score: ScoreRecord | None
    market_score: ScoreRecord | None
    signal_strategy_id: str | None
    signal_strategy_version: str | None
    normalized_action: str | None
    best_mode_horizon: str
    mode_perf_cache: dict[tuple[int, str], list[ModePerformance]]


class BindingEvaluator:
    """Evaluates user bindings against 3-tier scores and selects execution modes.

    Extracted verbatim from ``ScoreEngine`` so signal ingestion and binding
    evaluation are separate responsibilities. Constructed with the
    collaborators the evaluation block actually uses: the score store and the
    cloud-paper promotion policy.
    """

    def __init__(
        self,
        store: ScoreStore,
        *,
        paper_promotion_scope: PaperPromotionScope | None = None,
        paper_promotion_required: bool = False,
    ) -> None:
        self.store = store
        self._paper_promotion_scope = paper_promotion_scope
        self._paper_promotion_required = paper_promotion_required

    def _select_and_audit_bindings(
        self,
        bindings: list[ScoringUserBinding],
        signal_strategy_id: str | None,
    ) -> list[ScoringUserBinding]:
        """Apply strategy preference and suppress wildcard fallback after deactivation."""
        bindings = _select_effective_bindings(
            bindings,
            signal_strategy_id=signal_strategy_id,
        )
        if signal_strategy_id:
            bindings = self._suppress_wildcard_over_inactive_binding(
                bindings,
                signal_strategy_id,
            )
        return bindings

    def _scope_bindings_to_paper_promotion(
        self,
        bindings: list[ScoringUserBinding],
        *,
        score: ScoreRecord,
        signal_strategy_id: str | None,
        signal_strategy_version: str | None,
        promotion_model_context: PaperPromotionModelContext | None,
    ) -> list[ScoringUserBinding]:
        """Restrict cloud-paper decisions to the one evidence-backed route."""
        if not self._paper_promotion_required:
            return bindings
        scope = self._paper_promotion_scope
        if scope is None:
            logger.error(
                "Paper execution decisions suppressed: promotion authority is unavailable",
            )
            return []
        strategy_mismatch = (
            score.scope != "asset"
            or signal_strategy_id != scope.strategy_id
            or signal_strategy_version != scope.strategy_version
        )
        if scope.is_synchronized_portfolio:
            model_mismatch = promotion_model_context is None or (
                promotion_model_context.asset_class != scope.asset_class
                or promotion_model_context.data_use_scope != scope.data_use_scope
                or promotion_model_context.model_configuration_sha256
                != scope.model_configuration_sha256
                or promotion_model_context.instrument_set_sha256 != scope.instrument_set_sha256
            )
        else:
            model_mismatch = (
                promotion_model_context is not None
                or scope.canonical_instrument is None
                or normalize_product_symbol(score.target)
                != normalize_product_symbol(scope.canonical_instrument)
            )
        if strategy_mismatch or model_mismatch:
            logger.warning(
                "Paper execution decisions suppressed outside promoted signal scope: "
                "strategy=%s version=%s target=%s model_scope=%s",
                signal_strategy_id,
                signal_strategy_version,
                score.target,
                scope.model_scope,
            )
            return []

        scoped = [
            binding
            for binding in bindings
            if binding.user_id == scope.user_id
            and binding.binding_id == scope.strategy_binding_id
            and binding.broker_account_id == scope.broker_account_id
            and binding.strategy_id == scope.strategy_id
            and matches_exact_promotion_values(
                binding.allowed_brokers,
                expected={scope.broker_code},
                normalize=canonical_paper_broker_code,
            )
        ]
        authorized_symbols = (
            {normalize_product_symbol(scope.canonical_instrument)}
            if scope.canonical_instrument is not None
            else {normalize_product_symbol(symbol) for _, symbol in scope.instruments}
        )
        scoped = [
            binding
            for binding in scoped
            if matches_exact_promotion_values(
                binding.asset_filter,
                expected=authorized_symbols,
                normalize=normalize_product_symbol,
            )
        ]
        if len(scoped) != 1:
            logger.error(
                "Paper execution decisions suppressed: expected one promoted binding "
                "user=%s binding=%s account=%s strategy=%s, found=%s",
                scope.user_id,
                scope.strategy_binding_id,
                scope.broker_account_id,
                scope.strategy_id,
                len(scoped),
            )
            return []
        return scoped

    def _suppress_wildcard_over_inactive_binding(
        self,
        bindings: list[ScoringUserBinding],
        signal_strategy_id: str,
    ) -> list[ScoringUserBinding]:
        """Treat an inactive strategy binding as an explicit trading opt-out.

        ``_select_effective_bindings`` prefers strategy-scoped bindings, but when
        a user's strategy-specific binding has been DEACTIVATED the signal falls
        through to their wildcard binding.  That violates the user's explicit
        deactivation.  DB-backed stores expose inactive ids so the wildcard can
        be removed before threshold evaluation.
        """
        selected: list[ScoringUserBinding] = []
        for binding in bindings:
            if binding.strategy_id is not None:
                selected.append(binding)
                continue
            try:
                inactive_ids = self.store.list_inactive_strategy_binding_ids(
                    binding.user_id,
                    signal_strategy_id,
                    binding.broker_account_id,
                )
            except (SQLAlchemyError, OSError):
                # The active-only binding list cannot distinguish "never
                # configured" from "explicitly disabled" when this lookup is
                # unavailable. Suppress the wildcard fail-closed.
                logger.exception(
                    "Inactive-binding lookup failed; wildcard suppressed for "
                    "user=%s account=%s strategy=%s",
                    binding.user_id,
                    binding.broker_account_id,
                    signal_strategy_id,
                )
                continue
            if inactive_ids:
                logger.warning(
                    "Wildcard binding suppressed: user=%s account=%s strategy=%s has INACTIVE "
                    "strategy-specific binding(s) %s; wildcard binding %s will not trade",
                    binding.user_id,
                    binding.broker_account_id,
                    signal_strategy_id,
                    inactive_ids,
                    binding.binding_id,
                )
                continue
            selected.append(binding)
        return selected

    @staticmethod
    def _close_decision(binding: ScoringUserBinding) -> TriggerDecision:
        """Build the audit or execution decision for a risk-reducing close."""
        if not binding.exits_enabled:
            return TriggerDecision(
                user_id=binding.user_id,
                should_execute=False,
                reason="Close signal rejected because exit authority is disabled.",
                binding_id=binding.binding_id,
                broker_account_id=binding.broker_account_id,
                execution_mode=binding.execution_mode,
                sizing_profile=binding.sizing_profile,
                risk_caps=binding.risk_caps,
                allowed_brokers=binding.allowed_brokers,
            )
        return TriggerDecision(
            user_id=binding.user_id,
            should_execute=True,
            reason="Risk-reducing close signal bypassed score threshold",
            binding_id=binding.binding_id,
            broker_account_id=binding.broker_account_id,
            execution_mode=binding.execution_mode,
            sizing_profile=binding.sizing_profile,
            risk_caps=binding.risk_caps,
            allowed_brokers=binding.allowed_brokers,
        )

    @staticmethod
    def _evaluate_binding_thresholds(
        binding: ScoringUserBinding,
        context: _BindingEvaluationContext,
    ) -> BindingEvaluationResult:
        """Evaluate the binding through the canonical three-tier evaluator."""
        return evaluate_binding_thresholds(
            asset_score=Decimal(str(context.score.score)),
            asset_threshold=Decimal(str(binding.asset_score_threshold)),
            sector_score=(
                Decimal(str(context.sector_score.score)) if context.sector_score else None
            ),
            sector_threshold=(
                Decimal(str(binding.sector_score_threshold))
                if binding.sector_score_threshold is not None
                else None
            ),
            market_score=(
                Decimal(str(context.market_score.score)) if context.market_score else None
            ),
            market_threshold=(
                Decimal(str(binding.market_score_threshold))
                if binding.market_score_threshold is not None
                else None
            ),
        )

    @staticmethod
    def _threshold_failure_decision(
        binding: ScoringUserBinding,
        context: _BindingEvaluationContext,
        result: BindingEvaluationResult,
    ) -> TriggerDecision | None:
        """Return the first failed threshold decision, preserving tier order."""
        reason: str | None = None
        if not result.asset_result.passes:
            reason = (
                f"Asset score {context.score.score:.2f} "
                f"(abs {result.asset_result.magnitude:.2f}) "
                f"below threshold {binding.asset_score_threshold}"
            )
        elif binding.sector_score_threshold is not None:
            if context.sector_score is None or result.sector_result is None:
                reason = "Sector score unavailable for configured threshold"
            elif not result.sector_result.passes:
                reason = (
                    f"Sector score {context.sector_score.score:.2f} "
                    f"(abs {result.sector_result.magnitude:.2f}) below threshold "
                    f"{binding.sector_score_threshold}"
                )
        if reason is None and binding.market_score_threshold is not None:
            if context.market_score is None or result.market_result is None:
                reason = "Market score unavailable for configured threshold"
            elif not result.market_result.passes:
                reason = (
                    f"Market score {context.market_score.score:.2f} "
                    f"(abs {result.market_result.magnitude:.2f}) below threshold "
                    f"{binding.market_score_threshold}"
                )
        if reason is None:
            return None
        return TriggerDecision(
            user_id=binding.user_id,
            should_execute=False,
            reason=reason,
            binding_id=binding.binding_id,
            broker_account_id=binding.broker_account_id,
        )

    @staticmethod
    def _direction_conflict_decision(
        binding: ScoringUserBinding,
        context: _BindingEvaluationContext,
        result: BindingEvaluationResult,
    ) -> TriggerDecision | None:
        """Suppress a cross-strategy entry whose ensemble direction conflicts."""
        if not (
            context.normalized_action in ("long", "short")
            and "cross_strategy" in (context.score.metadata or {})
            and result.derived_direction in ("long", "short")
            and result.derived_direction != context.normalized_action
        ):
            return None
        return TriggerDecision(
            user_id=binding.user_id,
            should_execute=False,
            reason=(
                "Cross-strategy ensemble net direction "
                f"({result.derived_direction}) conflicts with the "
                f"{context.normalized_action} entry; suppressed (no consensus)."
            ),
            binding_id=binding.binding_id,
            broker_account_id=binding.broker_account_id,
        )

    def _mode_performance_for_binding(
        self,
        binding: ScoringUserBinding,
        context: _BindingEvaluationContext,
    ) -> list[ModePerformance]:
        """Load account/strategy-scoped performance once per evaluation."""
        account_id = binding.broker_account_id
        strategy_id = binding.strategy_id or context.signal_strategy_id
        if account_id is None or strategy_id is None:
            return []
        key = (account_id, strategy_id)
        if key not in context.mode_perf_cache:
            context.mode_perf_cache[key] = self.store.list_mode_performance(
                context.score.target,
                horizon=context.best_mode_horizon,
                account_id=account_id,
                strategy_id=strategy_id,
            )
        return context.mode_perf_cache[key]

    def _select_binding_execution_mode(
        self,
        binding: ScoringUserBinding,
        context: _BindingEvaluationContext,
        direction: str,
    ) -> tuple[str | None, str]:
        """Resolve fixed or policy-ranked execution mode with a safe fallback."""
        execution_mode = binding.execution_mode
        reason = f"Score threshold met; direction={direction}"
        mode_token = execution_mode.lower() if execution_mode else None
        if mode_token not in {"best", "auto"}:
            return execution_mode, reason

        best_mode = self._rank_mode_performance(
            self._mode_performance_for_binding(binding, context),
            allowed_modes=getattr(binding, "execution_modes_allowed", None),
            policy=getattr(binding, "mode_selection_policy", None),
        )
        if best_mode:
            return (
                best_mode,
                f"Score threshold met; selected best mode ({best_mode}); direction={direction}",
            )

        permitted = list(getattr(binding, "execution_modes_allowed", None) or [])
        execution_mode = getattr(binding, "preferred_mode", None) or (
            permitted[0] if permitted else None
        )
        return (
            execution_mode,
            "Score threshold met; no mode performance for permitted modes; "
            f"using fallback mode={execution_mode}; direction={direction}",
        )

    @staticmethod
    def _autopilot_decision(
        binding: ScoringUserBinding,
        execution_mode: str | None,
        reason: str,
    ) -> TriggerDecision:
        """Build an executable or manual-approval decision after all gates pass."""
        entries_authorized = binding.autopilot and binding.entries_enabled
        if not binding.entries_enabled:
            reason = f"Score threshold met but entry authority is disabled. {reason}"
        elif not binding.autopilot:
            reason = f"Score threshold met but autopilot=false; requires manual approval. {reason}"
        return TriggerDecision(
            user_id=binding.user_id,
            should_execute=entries_authorized,
            reason=reason,
            binding_id=binding.binding_id,
            broker_account_id=binding.broker_account_id,
            execution_mode=execution_mode,
            sizing_profile=binding.sizing_profile,
            risk_caps=binding.risk_caps,
            allowed_brokers=binding.allowed_brokers,
        )

    @staticmethod
    def _binding_is_in_scope(
        binding: ScoringUserBinding,
        context: _BindingEvaluationContext,
    ) -> bool:
        """Apply strategy and asset scoping before any policy decision."""
        strategy_mismatch = (
            binding.strategy_id is not None
            and context.signal_strategy_id is not None
            and binding.strategy_id != context.signal_strategy_id
        )
        return not strategy_mismatch and _binding_matches_asset(
            context.score.target,
            binding.asset_filter,
        )

    @classmethod
    def _pre_threshold_decision(
        cls,
        binding: ScoringUserBinding,
        context: _BindingEvaluationContext,
    ) -> TriggerDecision | None:
        """Handle close bypass and hard scoring abstention before thresholds."""
        if context.normalized_action == "flat":
            return cls._close_decision(binding)
        if context.score.metadata.get("abstained") is not True:
            return None
        return TriggerDecision(
            user_id=binding.user_id,
            should_execute=False,
            reason=(
                "Scoring abstained: "
                f"{context.score.metadata.get('abstain_reason', 'meta_inputs_unavailable')}"
            ),
            binding_id=binding.binding_id,
            broker_account_id=binding.broker_account_id,
            execution_mode=binding.execution_mode,
            sizing_profile=binding.sizing_profile,
            risk_caps=binding.risk_caps,
            allowed_brokers=binding.allowed_brokers,
        )

    def _evaluate_binding(
        self,
        binding: ScoringUserBinding,
        context: _BindingEvaluationContext,
    ) -> TriggerDecision | None:
        """Evaluate one in-scope binding and return its auditable decision."""
        if not self._binding_is_in_scope(binding, context):
            return None
        pre_threshold = self._pre_threshold_decision(binding, context)
        if pre_threshold is not None:
            return pre_threshold
        if binding.sector_filter and (
            context.sector_score is None or context.sector_score.target not in binding.sector_filter
        ):
            return None

        result = self._evaluate_binding_thresholds(binding, context)
        failure = self._threshold_failure_decision(binding, context, result)
        if failure is not None:
            return failure
        conflict = self._direction_conflict_decision(binding, context, result)
        if conflict is not None:
            return conflict
        execution_mode, reason = self._select_binding_execution_mode(
            binding,
            context,
            result.derived_direction,
        )
        return self._autopilot_decision(binding, execution_mode, reason)

    def evaluate_bindings(
        self,
        score: ScoreRecord,
        bindings: Iterable[ScoringUserBinding] | None = None,
        sector_score: ScoreRecord | None = None,
        market_score: ScoreRecord | None = None,
        signal_strategy_id: str | None = None,
        signal_strategy_version: str | None = None,
        signal_action: object | None = None,
        promotion_model_context: PaperPromotionModelContext | None = None,
    ) -> list[TriggerDecision]:
        """
        Evaluate user bindings against scores using 3-tier threshold logic.

        Uses the CANONICAL binding evaluator from lib_strategy.scoring.binding_evaluator.
        All thresholds use MAGNITUDE semantics (abs(score) >= threshold).
        Direction is derived from score sign (>= 0 → long, < 0 → short).

        All configured thresholds must pass (AND logic):
        - asset_score_threshold: Always checked (required)
        - sector_score_threshold: Checked if configured AND sector_score provided
        - market_score_threshold: Checked if configured AND market_score provided

        Args:
            score: Asset-level score to evaluate
            bindings: Optional list of bindings (defaults to all from store)
            sector_score: Optional sector-level score
            market_score: Optional market-level score
            signal_strategy_id: Strategy ID from the triggering signal for
                strategy-scoped binding filtering
            signal_strategy_version: Exact strategy version from the triggering
                signal, required by the cloud-paper promotion boundary
            promotion_model_context: Exact synchronized model configuration and
                instrument-allowlist identity. Ordinary single-signal decisions
                must leave this unset.
            signal_action: Triggering signal action. Risk-reducing CLOSE/flat
                signals bypass entry-score thresholds and are forwarded to
                execution as safe close intents.

        Returns:
            List of TriggerDecision objects.
        """
        bindings = list(bindings) if bindings is not None else self.store.list_bindings()
        bindings = self._scope_bindings_to_paper_promotion(
            bindings,
            score=score,
            signal_strategy_id=signal_strategy_id,
            signal_strategy_version=signal_strategy_version,
            promotion_model_context=promotion_model_context,
        )
        bindings = self._select_and_audit_bindings(bindings, signal_strategy_id)
        normalized_action = (
            normalize_scoring_action(signal_action) if signal_action is not None else None
        )

        # Mode-performance is identical for every binding in this evaluation
        # (same score.target + horizon), so load it at most once and rank in
        # memory per binding — avoids an N+1 of one list_mode_performance query
        # per best/auto binding. ``score.target`` and the derived horizon are
        # constant here; only the per-binding allow-list/policy differ.
        context = _BindingEvaluationContext(
            score=score,
            sector_score=sector_score,
            market_score=market_score,
            signal_strategy_id=signal_strategy_id,
            signal_strategy_version=signal_strategy_version,
            normalized_action=normalized_action,
            best_mode_horizon=self._derive_best_mode_horizon(score),
            mode_perf_cache={},
        )
        decisions = [self._evaluate_binding(binding, context) for binding in bindings]
        return [decision for decision in decisions if decision is not None]

    def select_best_mode(
        self,
        asset: str,
        horizon: str = "1d",
        allowed_modes: list[str] | None = None,
        policy: str | None = None,
        account_id: int | None = None,
        strategy_id: str | None = None,
    ) -> str | None:
        """Select the best execution mode for an asset from mode-performance stats.

        Honours the user's permitted modes and their mode_selection_policy:
        - ``best_return``    -> highest historical total_return
        - ``lowest_risk``    -> lowest historical max_drawdown
        - ``highest_sharpe`` -> highest Sharpe (default), tie-break total_return

        Performance is filtered to ``allowed_modes`` first, so a user who permits
        only e.g. ["spot"] is never assigned options/futures. Returns None when no
        permitted mode has performance data (caller falls back to preferred_mode).
        """
        perf = self.store.list_mode_performance(
            asset,
            horizon=horizon,
            account_id=account_id,
            strategy_id=strategy_id,
        )
        return self._rank_mode_performance(perf, allowed_modes=allowed_modes, policy=policy)

    @staticmethod
    def _rank_mode_performance(
        perf: list[ModePerformance],
        allowed_modes: list[str] | None = None,
        policy: str | None = None,
    ) -> str | None:
        """Rank pre-loaded mode-performance rows by policy and return the best mode.

        Pure (no I/O), so callers that already hold the performance list — e.g.
        the per-binding loop in ``evaluate_bindings``, which shares one list
        across all bindings for the same asset+horizon — can rank in memory
        instead of re-querying.
        """
        if allowed_modes:
            permitted = {m.lower() for m in allowed_modes}
            perf = [p for p in perf if str(p.execution_mode).lower() in permitted]
        if not perf:
            return None

        policy_value = (policy or "highest_sharpe").lower()
        if policy_value == "best_return":
            ranked = sorted(perf, key=lambda p: (p.total_return, p.sharpe), reverse=True)
        elif policy_value == "lowest_risk":
            # Lowest max_drawdown first; tie-break on higher Sharpe.
            ranked = sorted(perf, key=lambda p: (p.max_drawdown, -p.sharpe))
        else:  # highest_sharpe (and any unrecognised / automatic policy)
            ranked = sorted(perf, key=lambda p: (p.sharpe, p.total_return), reverse=True)
        return ranked[0].execution_mode if ranked else None

    @staticmethod
    def _derive_best_mode_horizon(score: ScoreRecord) -> str:
        """Derive the mode-performance horizon key from a score's metadata."""
        meta_horizon = score.metadata.get("horizon")
        if isinstance(meta_horizon, str) and meta_horizon.strip():
            return meta_horizon
        if isinstance(meta_horizon, (int, float)):
            return f"{meta_horizon}d"
        return "1d"
