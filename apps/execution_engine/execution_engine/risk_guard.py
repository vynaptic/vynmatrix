"""Pre-trade risk validation for the execution engine."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from lib_common.config_validation import RiskLimits
from lib_common.logging import get_logger
from lib_common.metrics import counter
from lib_data.market_data import normalize_product_symbol
from lib_strategy.signals.adapters.scoring import (
    EntrySignalRequirementDecision,
    evaluate_entry_signal_requirements,
)
from lib_strategy.signals.signal import Signal, SignalAction

from .brokers.base import (
    BrokerCapabilities,
    PositionValuationError,
    position_gross_notional,
)
from .config import AccountState, ExecutionMode
from .models import (
    CloseQuantityOverride,
    OptionsIntent,
    OrderIntent,
    TargetPositionQuantityOverride,
)
from .risk_breach_store import RiskBreachStore

logger = get_logger(__name__)

_RISK_BLOCKS_TOTAL = counter(
    "vm_execution_risk_blocks_total",
    "Execution risk guard blocks by rule code",
    ("rule_code",),
)


@dataclass
class RiskDecision:
    """Result of a pre-trade risk evaluation."""

    allowed: bool
    rule_code: str | None = None
    message: str | None = None
    severity: str = "block"
    context: dict[str, Any] = field(default_factory=dict)


# The position sizer caps a position's notional to exactly max_position_pct of
# equity, but the risk guard re-estimates the notional from market_data, so price
# granularity / rounding can land it a hair over (e.g. 0.1001 vs a 0.1000 cap) and
# the strict ``>`` check would block ~all entries (N6). A small relative tolerance
# absorbs that sub-percent overshoot without materially loosening the cap.
_CAP_TOLERANCE = 1.005  # allow up to 0.5% over a pct cap before blocking
_ACCOUNT_DRAWDOWN_POLICY = "account_drawdown_mandate_v1"
_QUANTITY_MATCH_TOLERANCE = Decimal("0.00000001")
_SHA256_HEX_LENGTH = 64


def _valid_rebalance_digest(value: str) -> bool:
    return len(value) == _SHA256_HEX_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


def _symbols_match(*symbols: str) -> bool:
    if len(set(symbols)) != 1:
        return False
    try:
        normalized = {normalize_product_symbol(symbol) for symbol in symbols}
    except (TypeError, ValueError):
        return False
    return len(normalized) == 1


def _decimal_matches(
    value: object,
    expected: Decimal,
    *,
    tolerance: Decimal = _QUANTITY_MATCH_TOLERANCE,
) -> bool:
    if isinstance(value, bool):
        return False
    try:
        candidate = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return False
    return candidate.is_finite() and abs(candidate - expected) <= tolerance


def is_typed_target_reduction_request(
    signal: Signal,
    override: TargetPositionQuantityOverride | None,
) -> bool:
    """Return whether an internal target override is an exact CLOSE reduction request."""

    return bool(
        isinstance(override, TargetPositionQuantityOverride)
        and signal.action is SignalAction.CLOSE
        and override.delta_quantity < 0
        and _symbols_match(signal.symbol, override.symbol)
        and _valid_rebalance_digest(override.account_plan_id)
        and _valid_rebalance_digest(override.plan_leg_id)
    )


def is_exact_target_reduction_intent(
    signal: Signal,
    override: TargetPositionQuantityOverride | None,
    intents: Sequence[OrderIntent | OptionsIntent],
) -> bool:
    """Bind a target reduction request to its one exact reduce-only order intent."""

    if not is_typed_target_reduction_request(signal, override) or len(intents) != 1:
        return False
    assert override is not None
    intent = intents[0]
    if not isinstance(intent, OrderIntent) or not _symbols_match(
        signal.symbol,
        override.symbol,
        intent.symbol,
    ):
        return False
    metadata = intent.metadata
    if (
        str(intent.side or "").strip().upper() != "SELL"
        or not _decimal_matches(intent.quantity, abs(override.delta_quantity))
        or not isinstance(metadata, dict)
        or metadata.get("purpose") != "close_position"
        or metadata.get("reduce_only") is not True
        or not _decimal_matches(metadata.get("reference_price"), override.reference_price)
    ):
        return False
    payload = metadata.get("rebalance_target_override")
    if not isinstance(payload, dict):
        return False
    decimal_fields = {
        "target_allocation": override.target_allocation,
        "target_quantity": override.target_quantity,
        "strategy_quantity": override.strategy_quantity,
        "broker_quantity": override.broker_quantity,
        "delta_quantity": override.delta_quantity,
        "projected_broker_quantity": override.broker_quantity + override.delta_quantity,
        "target_weight_drift_fraction": override.target_weight_drift_fraction,
        "reference_price": override.reference_price,
    }
    return bool(
        payload.get("account_plan_id") == override.account_plan_id
        and payload.get("plan_leg_id") == override.plan_leg_id
        and payload.get("broker_observed_at") == override.broker_observed_at.isoformat()
        and payload.get("quote_observed_at") == override.quote_observed_at.isoformat()
        and _decimal_matches(
            payload.get("revalidated_target_quantity"),
            override.target_quantity,
            tolerance=max(
                _QUANTITY_MATCH_TOLERANCE,
                override.target_quantity * override.target_weight_drift_fraction,
            ),
        )
        and all(
            _decimal_matches(payload.get(field), value) for field, value in decimal_fields.items()
        )
    )


class RiskGuard:
    """Evaluate user/broker/strategy risk constraints before broker submission."""

    _LIMIT_FIELDS = (
        "max_position_pct",
        "max_total_exposure_pct",
        "max_daily_loss_pct",
        "max_open_positions",
        "max_drawdown_pct",
        "daily_trade_limit",
    )

    def __init__(
        self,
        *,
        session_factory: Any | None = None,
        breach_store: RiskBreachStore | None = None,
        short_eligible_asset_classes: frozenset[str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._breach_store = breach_store
        # 4th shorting-eligibility dimension (Q2): an allowlist of asset classes
        # that MAY be shorted. Empty/None = no asset-class restriction (default —
        # the other three dimensions still gate), so the spot-only paper soak is
        # byte-identical. Populated = only these classes are short-eligible.
        self._short_eligible_asset_classes = short_eligible_asset_classes or frozenset()

    def evaluate(  # noqa: PLR0911
        self,
        *,
        user_id: str,
        signal: Signal,
        profile: dict[str, Any],
        user_strategy_config: dict[str, Any],
        account_state: AccountState,
        execution_mode: ExecutionMode,
        broker_capabilities: BrokerCapabilities,
        intents: Sequence[OrderIntent | OptionsIntent] | None = None,
        market_data: dict[str, Any] | None = None,
        close_quantity_override: CloseQuantityOverride | None = None,
        target_position_override: TargetPositionQuantityOverride | None = None,
    ) -> RiskDecision:
        limits = self._load_limits(user_id=user_id, profile=profile, config=user_strategy_config)
        risk_reducing = self._is_exact_typed_risk_reduction(
            signal=signal,
            intents=intents,
            close_quantity_override=close_quantity_override,
            target_position_override=target_position_override,
        )

        if signal.action == SignalAction.SHORT:
            short_block = self._evaluate_short_block(
                signal=signal,
                profile=profile,
                user_strategy_config=user_strategy_config,
                broker_capabilities=broker_capabilities,
                execution_mode=execution_mode,
            )
            if short_block is not None:
                return short_block

        portfolio_stop_authorized, portfolio_stop_block = self._portfolio_stop_authority(
            user_id=user_id,
            signal=signal,
            profile=profile,
            user_strategy_config=user_strategy_config,
            account_state=account_state,
        )
        if portfolio_stop_block is not None:
            return portfolio_stop_block
        require_stop_loss = (
            False
            if portfolio_stop_authorized
            else bool(
                user_strategy_config.get(
                    "require_stop_loss", profile.get("require_stop_loss", True)
                )
            )
        )
        entry_requirements = evaluate_entry_signal_requirements(
            signal,
            require_stop_loss=require_stop_loss,
            require_explicit_scoring_inputs=False,
        )
        if not entry_requirements.allowed:
            return self._entry_requirement_block(entry_requirements)

        risk_caps = dict(user_strategy_config.get("risk_caps") or {})
        require_explicit_scoring_inputs = bool(
            user_strategy_config.get(
                "require_explicit_scoring_inputs",
                risk_caps.get(
                    "require_explicit_scoring_inputs",
                    profile.get("require_explicit_scoring_inputs", False),
                ),
            )
        )
        entry_requirements = evaluate_entry_signal_requirements(
            signal,
            require_stop_loss=False,
            require_explicit_scoring_inputs=require_explicit_scoring_inputs,
        )
        if not entry_requirements.allowed:
            return self._entry_requirement_block(entry_requirements)

        if (
            signal.action in {SignalAction.LONG, SignalAction.SHORT}
            and account_state.daily_trades >= limits.daily_trade_limit
        ):
            return self._block(
                rule_code="daily_trade_limit",
                message=(
                    f"Daily trade limit reached: {account_state.daily_trades}/"
                    f"{limits.daily_trade_limit}"
                ),
                context={"daily_trades": account_state.daily_trades},
            )

        if (
            signal.action in {SignalAction.LONG, SignalAction.SHORT, SignalAction.CLOSE}
            and not risk_reducing
            and account_state.open_positions >= limits.max_open_positions
            and not self._has_open_symbol(account_state, signal.symbol)
        ):
            return self._block(
                rule_code="max_open_positions",
                message=(
                    f"Open position limit reached: {account_state.open_positions}/"
                    f"{limits.max_open_positions}"
                ),
                context={"open_positions": account_state.open_positions},
            )

        daily_loss_pct = self._resolve_daily_loss_pct(profile=profile, account_state=account_state)
        if (
            not risk_reducing
            and daily_loss_pct is not None
            and daily_loss_pct > float(limits.max_daily_loss_pct)
        ):
            return self._block(
                rule_code="max_daily_loss_pct",
                message=(
                    f"Daily loss {daily_loss_pct:.4f} exceeds limit "
                    f"{float(limits.max_daily_loss_pct):.4f}"
                ),
                context={"daily_loss_pct": daily_loss_pct},
            )

        drawdown_pct = self._resolve_drawdown_pct(profile=profile, account_state=account_state)
        if (
            not risk_reducing
            and drawdown_pct is not None
            and drawdown_pct > float(limits.max_drawdown_pct)
        ):
            return self._block(
                rule_code="max_drawdown_pct",
                message=(
                    f"Drawdown {drawdown_pct:.4f} exceeds limit "
                    f"{float(limits.max_drawdown_pct):.4f}"
                ),
                context={"drawdown_pct": drawdown_pct},
            )

        if intents and not risk_reducing:
            try:
                incremental_notional = self._estimate_notional(
                    intents,
                    signal,
                    market_data,
                    target_position_override=target_position_override,
                )
                projected_position_notional = self._estimate_projected_position_notional(
                    intents,
                    incremental_notional=incremental_notional,
                    target_position_override=target_position_override,
                )
            except PositionValuationError as exc:
                return self._block(
                    rule_code="projected_position_valuation_unavailable",
                    message=str(exc),
                    context={"valuation_error": str(exc)},
                )
            if projected_position_notional is not None and account_state.equity > 0:
                projected_pct = projected_position_notional / float(account_state.equity)
                if projected_pct > float(limits.max_position_pct) * _CAP_TOLERANCE:
                    return self._block(
                        rule_code="max_position_pct",
                        message=(
                            f"Projected position {projected_pct:.4f} exceeds limit "
                            f"{float(limits.max_position_pct):.4f}"
                        ),
                        context={
                            "projected_notional": projected_position_notional,
                            "projected_position_pct": projected_pct,
                        },
                    )

                assert incremental_notional is not None
                try:
                    total_exposure = self._estimate_existing_exposure(account_state)
                except PositionValuationError as exc:
                    return self._block(
                        rule_code="account_position_valuation_unavailable",
                        message=str(exc),
                        context={"valuation_error": str(exc)},
                    )
                total_exposure += incremental_notional
                total_exposure_pct = total_exposure / float(account_state.equity)
                if total_exposure_pct > float(limits.max_total_exposure_pct) * _CAP_TOLERANCE:
                    return self._block(
                        rule_code="max_total_exposure_pct",
                        message=(
                            f"Projected total exposure {total_exposure_pct:.4f} exceeds limit "
                            f"{float(limits.max_total_exposure_pct):.4f}"
                        ),
                        context={
                            "existing_exposure": total_exposure - incremental_notional,
                            "projected_notional": incremental_notional,
                            "projected_total_exposure": total_exposure,
                            "projected_total_exposure_pct": total_exposure_pct,
                        },
                    )

        return RiskDecision(allowed=True)

    def _portfolio_stop_authority(  # noqa: PLR0911
        self,
        *,
        user_id: str,
        signal: Signal,
        profile: dict[str, Any],
        user_strategy_config: dict[str, Any],
        account_state: AccountState,
    ) -> tuple[bool, RiskDecision | None]:
        """Authorize stop-less portfolio targets only behind a real mandate."""

        raw_policy = signal.metadata.get("portfolio_stop_policy")
        if raw_policy is None:
            return (False, None)
        if signal.action is not SignalAction.LONG:
            # A portfolio policy governs exposure increases. It must never
            # delay a CLOSE/reduction merely because entry authority is absent.
            return (False, None)
        if raw_policy != _ACCOUNT_DRAWDOWN_POLICY:
            return (
                False,
                self._block(
                    rule_code="portfolio_stop_policy_unsupported",
                    message=f"Unsupported portfolio stop policy {raw_policy!r}",
                    context={"portfolio_stop_policy": raw_policy},
                ),
            )
        if user_strategy_config.get("require_stop_loss") is not False:
            return (
                False,
                self._block(
                    rule_code="portfolio_stop_override_required",
                    message=(
                        "Portfolio drawdown policy requires an explicit reviewed "
                        "require_stop_loss=false binding"
                    ),
                    context={"portfolio_stop_policy": raw_policy},
                ),
            )
        mandate_check = getattr(self._breach_store, "has_user_drawdown_mandate", None)
        try:
            mandate_present = bool(mandate_check(user_id=user_id)) if mandate_check else False
        except (SQLAlchemyError, TypeError, ValueError):
            logger.exception("Failed to validate user drawdown mandate", user_id=user_id)
            mandate_present = False
        if not mandate_present:
            return (
                False,
                self._block(
                    rule_code="portfolio_drawdown_mandate_missing",
                    message="Portfolio target requires an effective user-owned drawdown mandate",
                    context={"portfolio_stop_policy": raw_policy},
                ),
            )
        if profile.get("risk_baseline_has_persisted_account_peak") is not True:
            return (
                False,
                self._block(
                    rule_code="portfolio_drawdown_baseline_missing",
                    message=(
                        "Portfolio target requires durable account-owned peak-equity provenance"
                    ),
                    context={
                        "portfolio_stop_policy": raw_policy,
                        "risk_baseline_has_persisted_account_peak": False,
                    },
                ),
            )
        if profile.get("risk_baseline_current_equity_from_broker") is not True:
            return (
                False,
                self._block(
                    rule_code="portfolio_drawdown_baseline_missing",
                    message=(
                        "Portfolio target requires current equity from the "
                        "authoritative broker account"
                    ),
                    context={
                        "portfolio_stop_policy": raw_policy,
                        "risk_baseline_current_equity_from_broker": False,
                    },
                ),
            )
        if self._resolve_drawdown_pct(profile=profile, account_state=account_state) is None:
            return (
                False,
                self._block(
                    rule_code="portfolio_drawdown_baseline_missing",
                    message="Portfolio target requires an account-scoped peak-equity baseline",
                    context={"portfolio_stop_policy": raw_policy},
                ),
            )
        return (True, None)

    @staticmethod
    def _has_open_symbol(account_state: AccountState, symbol: str) -> bool:
        """Return whether an entry changes an existing distinct-position count."""

        target = normalize_product_symbol(symbol)
        for position in getattr(account_state, "positions", []):
            if normalize_product_symbol(str(position.get("symbol") or "")) != target:
                continue
            try:
                quantity = Decimal(str(position.get("qty", position.get("quantity", 0))))
            except (ArithmeticError, TypeError, ValueError):
                continue
            if quantity.is_finite() and quantity != 0:
                return True
        return False

    @staticmethod
    def _is_exact_typed_risk_reduction(  # noqa: PLR0911
        *,
        signal: Signal,
        intents: Sequence[OrderIntent | OptionsIntent] | None,
        close_quantity_override: CloseQuantityOverride | None,
        target_position_override: TargetPositionQuantityOverride | None,
    ) -> bool:
        """Prove one built CLOSE is exactly bound to one internal reduction override."""

        if intents is None or (close_quantity_override is None) == (
            target_position_override is None
        ):
            return False
        if target_position_override is not None:
            return is_exact_target_reduction_intent(
                signal,
                target_position_override,
                intents,
            )
        assert close_quantity_override is not None
        if (
            not isinstance(close_quantity_override, CloseQuantityOverride)
            or signal.action is not SignalAction.CLOSE
            or len(intents) != 1
        ):
            return False
        intent = intents[0]
        if not isinstance(intent, OrderIntent) or not _symbols_match(
            signal.symbol,
            close_quantity_override.symbol,
            intent.symbol,
        ):
            return False
        metadata = intent.metadata
        if (
            str(intent.side or "").strip().upper()
            != ("SELL" if close_quantity_override.broker_quantity > 0 else "BUY")
            or not _decimal_matches(intent.quantity, close_quantity_override.quantity)
            or not isinstance(metadata, dict)
            or metadata.get("purpose") != "close_position"
        ):
            return False
        payload = metadata.get("rebalance_close_override")
        if not isinstance(payload, dict):
            return False
        decimal_fields = {
            "strategy_quantity": close_quantity_override.strategy_quantity,
            "broker_quantity": close_quantity_override.broker_quantity,
            "clamped_quantity": close_quantity_override.quantity,
        }
        return bool(
            payload.get("account_plan_id") == close_quantity_override.account_plan_id
            and payload.get("plan_leg_id") == close_quantity_override.plan_leg_id
            and payload.get("broker_observed_at")
            == close_quantity_override.broker_observed_at.isoformat()
            and all(
                _decimal_matches(payload.get(field), value)
                for field, value in decimal_fields.items()
            )
        )

    def _entry_requirement_block(
        self,
        decision: EntrySignalRequirementDecision,
    ) -> RiskDecision:
        """Translate the shared pure entry result into an observable risk block."""

        if decision.rule_code is None or decision.message is None:
            msg = "blocked entry requirement must identify its rule and message"
            raise RuntimeError(msg)
        return self._block(
            rule_code=decision.rule_code,
            message=decision.message,
            context=dict(decision.context),
        )

    def _block(self, *, rule_code: str, message: str, context: dict[str, Any]) -> RiskDecision:
        logger.warning("Risk block: %s", message, rule_code=rule_code, **context)
        if _RISK_BLOCKS_TOTAL is not None:
            _RISK_BLOCKS_TOTAL.labels(rule_code).inc()
        return RiskDecision(
            allowed=False,
            rule_code=rule_code,
            message=message,
            severity="block",
            context=context,
        )

    def persist(self, *, user_id: str, decision: RiskDecision) -> None:
        if decision.allowed or self._breach_store is None or not decision.rule_code:
            return
        try:
            self._breach_store.record(
                user_id=user_id,
                rule_code=decision.rule_code,
                severity=decision.severity,
                context=decision.context,
            )
        except Exception:
            # Boundary catch: ``_breach_store`` is a persistence-layer adapter
            # (SQLAlchemy / file-backed / no-op). Any storage failure here must
            # not block the execution path that already returned a decision.
            # We surface the traceback and continue.
            logger.exception("Failed to persist risk breach", user_id=user_id)

    def _load_limits(
        self, *, user_id: str, profile: dict[str, Any], config: dict[str, Any]
    ) -> RiskLimits:
        merged: dict[str, Any] = {}
        mandates: list[dict[str, Any]] = []
        if self._breach_store is not None:
            mandates = list(self._breach_store.load_mandates(user_id=user_id))

        profile_limits = dict(profile.get("risk_limits") or {})
        config_limits = dict(config.get("risk_limits") or {})
        profile_caps = dict(profile.get("risk_caps") or {})
        config_caps = dict(config.get("risk_caps") or {})
        merged.update(profile_limits)
        merged.update(config_limits)
        merged.update(profile_caps)
        merged.update(config_caps)

        for snapshot in (
            profile.get("_execution_policy_snapshot"),
            config.get("_execution_policy_snapshot"),
        ):
            if not isinstance(snapshot, dict):
                continue
            merged.update(dict(snapshot.get("risk_caps") or {}))
            snapshot_config = snapshot.get("config")
            if isinstance(snapshot_config, dict):
                merged.update(dict(snapshot_config.get("risk_caps") or {}))

        # Top-level overrides still win.
        for field in (  # noqa: F402
            "max_position_pct",
            "max_total_exposure_pct",
            "max_daily_loss_pct",
            "max_open_positions",
            "max_drawdown_pct",
            "daily_trade_limit",
        ):
            if field in profile:
                merged[field] = profile[field]
            if field in config:
                merged[field] = config[field]

        limits = RiskLimits(**merged)
        if not mandates:
            return limits
        return self._apply_mandate_ceilings(limits=limits, mandates=mandates)

    def _apply_mandate_ceilings(
        self,
        *,
        limits: RiskLimits,
        mandates: Sequence[dict[str, Any]],
    ) -> RiskLimits:
        effective = dict(limits.model_dump(mode="python"))
        for rules in mandates:
            for limit_field in self._LIMIT_FIELDS:
                if limit_field not in rules:
                    continue
                capped = getattr(RiskLimits(**{limit_field: rules[limit_field]}), limit_field)
                current = effective[limit_field]
                effective[limit_field] = min(current, capped)
        return RiskLimits(**effective)

    def _resolve_daily_loss_pct(
        self,
        *,
        profile: dict[str, Any],
        account_state: AccountState,
    ) -> float | None:
        realized_today = profile.get("realized_pnl_today")
        day_start_equity = profile.get("day_start_equity")
        if realized_today is not None and day_start_equity:
            try:
                loss = min(0.0, float(realized_today))
                return abs(loss) / float(day_start_equity) if loss < 0 else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                return None

        if day_start_equity and account_state.equity:
            try:
                start_equity = float(day_start_equity)
                if start_equity > 0 and account_state.equity < start_equity:
                    return (start_equity - float(account_state.equity)) / start_equity
            except (TypeError, ValueError, ZeroDivisionError):
                return None
        return None

    def evaluate_post_trade(
        self,
        *,
        user_id: str,
        profile: dict[str, Any],
        user_strategy_config: dict[str, Any],
        account_state: AccountState,
    ) -> RiskDecision:
        """Validate account exposure after broker submission/fill."""
        limits = self._load_limits(user_id=user_id, profile=profile, config=user_strategy_config)
        if account_state.equity <= 0:
            return RiskDecision(allowed=True)
        try:
            exposure = self._estimate_existing_exposure(account_state)
        except PositionValuationError as exc:
            return self._block(
                rule_code="account_position_valuation_unavailable",
                message=str(exc),
                context={"valuation_error": str(exc)},
            )
        exposure_pct = exposure / float(account_state.equity)
        if exposure_pct > float(limits.max_total_exposure_pct) * _CAP_TOLERANCE:
            return self._block(
                rule_code="post_trade_max_total_exposure_pct",
                message=(
                    f"Post-trade exposure {exposure_pct:.4f} exceeds limit "
                    f"{float(limits.max_total_exposure_pct):.4f}"
                ),
                context={
                    "exposure": exposure,
                    "exposure_pct": exposure_pct,
                    "equity": account_state.equity,
                },
            )
        return RiskDecision(allowed=True)

    def _resolve_drawdown_pct(
        self,
        *,
        profile: dict[str, Any],
        account_state: AccountState,
    ) -> float | None:
        peak_equity = profile.get("peak_equity")
        if not peak_equity or not account_state.equity:
            return None
        try:
            peak = float(peak_equity)
            if peak <= 0 or account_state.equity >= peak:
                return 0.0
            return (peak - float(account_state.equity)) / peak
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    def _estimate_notional(
        self,
        intents: Iterable[OrderIntent | OptionsIntent],
        signal: Signal,
        market_data: dict[str, Any] | None,
        *,
        target_position_override: TargetPositionQuantityOverride | None = None,
    ) -> float | None:
        price: float | None = None
        if target_position_override is not None:
            price = float(target_position_override.reference_price)
        elif market_data:
            raw_price = market_data.get("price")
            if raw_price is not None:
                price = float(raw_price)
        if price is None and signal.entry_price is not None:
            price = float(signal.entry_price)

        notional = Decimal("0")
        for intent in intents:
            metadata = dict(getattr(intent, "metadata", {}) or {})
            purpose = str(metadata.get("purpose") or "").strip().lower()
            # Reduce-only exits do not add exposure. "bracket" is the native OCO exit
            # (stop-loss + take-profit as one order); omitting it here double-counts the
            # position (entry + bracket) and trips the max-position cap at 2x.
            # A close_position intent reaches this estimator only when it failed the
            # exact typed reduction proof, so value it conservatively as new exposure.
            if purpose in {"stop_loss", "take_profit", "bracket"}:
                continue
            if isinstance(intent, OptionsIntent):
                option_exposure = self._estimate_options_exposure(intent, fallback_price=price)
                if option_exposure is None:
                    continue
                notional += self._intent_account_notional(
                    intent,
                    Decimal(str(abs(option_exposure))),
                )
                continue
            if target_position_override is not None:
                if price is None or price <= 0:
                    return None
                notional += self._intent_account_notional(
                    intent,
                    Decimal(str(abs(intent.quantity) * price)),
                )
                continue
            explicit_notional = metadata.get("notional_value")
            if explicit_notional not in (None, ""):
                notional += self._intent_account_notional(
                    intent,
                    self._required_positive_notional(
                        explicit_notional,
                        context=f"intent {intent.symbol}",
                    ),
                )
                continue
            if price is None or price <= 0:
                return None
            notional += self._intent_account_notional(
                intent,
                Decimal(str(abs(intent.quantity) * price)),
            )
        return float(notional)

    @staticmethod
    def _estimate_projected_position_notional(
        intents: Sequence[OrderIntent | OptionsIntent],
        *,
        incremental_notional: float | None,
        target_position_override: TargetPositionQuantityOverride | None,
    ) -> float | None:
        """Value the post-order broker position for a positive portfolio target.

        A portfolio target is strategy-relative, so a broker account may already
        contain manual or differently attributed shares of the same issuer. The
        per-position cap owns the whole account position, while aggregate exposure
        must add only the new order's notional. Keeping these values separate
        prevents a small strategy top-up from hiding an over-limit broker position.
        """

        if target_position_override is None or target_position_override.delta_quantity <= 0:
            return incremental_notional
        if len(intents) != 1 or not isinstance(intents[0], OrderIntent):
            msg = "Target-position entry must produce one valuatable spot order"
            raise PositionValuationError(msg)
        if incremental_notional is None:
            msg = "Target-position entry has no incremental order valuation"
            raise PositionValuationError(msg)
        intent = intents[0]
        projected_quantity = (
            target_position_override.broker_quantity + target_position_override.delta_quantity
        )
        if not projected_quantity.is_finite() or projected_quantity <= 0:
            msg = "Target-position entry produced an invalid broker quantity"
            raise PositionValuationError(msg)
        settlement_notional = projected_quantity * target_position_override.reference_price
        return float(
            RiskGuard._intent_account_notional(
                intent,
                settlement_notional,
            )
        )

    @staticmethod
    def _intent_account_notional(
        intent: OrderIntent | OptionsIntent,
        settlement_notional: Decimal,
    ) -> Decimal:
        context = intent.currency_context
        if context is None:
            return settlement_notional
        return context.settlement_to_account(settlement_notional)

    @staticmethod
    def _estimate_existing_exposure(account_state: AccountState) -> float:
        exposure = Decimal("0")
        # account_state may be a partial snapshot/stub; tolerate a missing
        # positions attribute rather than assuming the full AccountState shape.
        for position in getattr(account_state, "positions", []) or []:
            exposure += position_gross_notional(position)
        return float(exposure)

    @staticmethod
    def _estimate_options_exposure(
        intent: OptionsIntent,
        *,
        fallback_price: float | None,
    ) -> float | None:
        metadata = dict(intent.metadata or {})
        for candidate in (
            intent.max_loss,
            metadata.get("max_loss"),
            metadata.get("net_debit"),
        ):
            if candidate is None:
                continue
            try:
                exposure = abs(float(candidate))
            except (TypeError, ValueError):
                continue
            if exposure > 0:
                return exposure

        underlying_price = intent.underlying_price or fallback_price
        if underlying_price is None or underlying_price <= 0:
            return None
        contracts = abs(float(intent.quantity or 0.0))
        if contracts <= 0:
            return None
        multiplier_raw = metadata.get("contract_multiplier")
        if multiplier_raw in (None, ""):
            msg = f"Options intent {intent.symbol!r} omitted contract_multiplier"
            raise PositionValuationError(msg)
        try:
            multiplier = abs(float(str(multiplier_raw)))
        except (TypeError, ValueError):
            msg = f"Options intent {intent.symbol!r} returned invalid contract_multiplier"
            raise PositionValuationError(msg) from None
        if multiplier <= 0:
            msg = f"Options intent {intent.symbol!r} returned non-positive contract_multiplier"
            raise PositionValuationError(msg)
        return abs(float(underlying_price)) * contracts * multiplier

    @staticmethod
    def _required_positive_notional(value: Any, *, context: str) -> Decimal:
        try:
            notional = Decimal(str(value))
        except (ArithmeticError, TypeError, ValueError) as exc:
            msg = f"{context} returned invalid notional_value"
            raise PositionValuationError(msg) from exc
        if not notional.is_finite() or notional <= 0:
            msg = f"{context} returned non-positive or non-finite notional_value"
            raise PositionValuationError(msg)
        return notional

    def _evaluate_short_block(
        self,
        *,
        signal: Signal,
        profile: dict[str, Any],
        user_strategy_config: dict[str, Any],
        broker_capabilities: BrokerCapabilities,
        execution_mode: ExecutionMode,
    ) -> RiskDecision | None:
        """Capability gate for SHORT signals (Q2): a short is permitted only when
        ALL eligibility dimensions allow it —

        1. user eligibility (``enable_shorting`` on the strategy config / profile),
        2. the execution mode + broker supporting shorts (``_supports_shorting``;
           spot is long-only, derivatives/margin need broker support),
        3. the instrument being shortable (``signal.metadata['instrument_shortable']``;
           only an explicit ``False`` blocks — unknown/absent means no per-instrument
           restriction, so this dimension is additive and non-breaking), and
        4. the strategy asset class being short-eligible (``signal.asset_class`` in
           the configured allowlist; an empty allowlist or an unknown asset class
           imposes no restriction, so this dimension is additive and default-OFF).

        Returns a block ``RiskDecision`` naming the failing dimension in its
        context, or ``None`` when shorting is allowed. The top-level ``rule_code``
        and message are stable ("shorting_disabled" / "SHORT blocked for
        broker=<b>") so existing consumers and audit rows are unaffected; the
        specific dimension is exposed via ``context['short_block_dimension']``.
        """
        user_eligible = bool(
            user_strategy_config.get("enable_shorting", profile.get("enable_shorting", False))
        )
        broker_mode_supports = self._supports_shorting(
            broker_capabilities=broker_capabilities,
            execution_mode=execution_mode,
        )
        instrument_shortable = (signal.metadata or {}).get("instrument_shortable")
        instrument_blocked = instrument_shortable is False
        asset_class_eligible = self._asset_class_short_eligible(signal)

        if (
            user_eligible
            and broker_mode_supports
            and not instrument_blocked
            and asset_class_eligible
        ):
            return None

        if not user_eligible:
            dimension = "user_not_eligible"
        elif not broker_mode_supports:
            dimension = "broker_mode_unsupported"
        elif instrument_blocked:
            dimension = "instrument_not_shortable"
        else:
            dimension = "asset_class_not_shortable"

        return self._block(
            rule_code="shorting_disabled",
            message=f"SHORT blocked for broker={broker_capabilities.broker_type.value}",
            context={
                "signal_id": signal.signal_id,
                "symbol": signal.symbol,
                "broker": broker_capabilities.broker_type.value,
                "execution_mode": execution_mode.value,
                "short_block_dimension": dimension,
            },
        )

    def _supports_shorting(
        self,
        *,
        broker_capabilities: BrokerCapabilities,
        execution_mode: ExecutionMode,
    ) -> bool:
        if execution_mode in {ExecutionMode.PERPETUAL, ExecutionMode.FUTURES}:
            return broker_capabilities.supports_futures or broker_capabilities.supports_perpetual
        if execution_mode == ExecutionMode.MARGIN:
            return broker_capabilities.supports_margin
        return False

    def _asset_class_short_eligible(self, signal: Signal) -> bool:
        """Whether the signal's strategy asset class may be shorted (Q2 dim 4).

        Additive + default-OFF: an empty allowlist imposes no restriction, and an
        unknown/absent ``signal.asset_class`` is never blocked (we don't gate on
        missing data). Only a populated allowlist that excludes a known asset class
        blocks the short.
        """
        allowlist = self._short_eligible_asset_classes
        if not allowlist:
            return True
        asset_class = (signal.asset_class or "").strip().lower()
        if not asset_class:
            return True
        return asset_class in allowlist
