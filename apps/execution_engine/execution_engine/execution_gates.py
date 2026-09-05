"""Pre-trade freshness and live-policy gates for execution dispatch."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lib_common.asset_classes import REFERENCE_ONLY_ASSET_CLASSES
from lib_common.config_validation import ExecutionFreshnessConfig
from lib_common.logging import get_logger
from lib_strategy.signals.signal import Signal, SignalAction

from .config import AccountState
from .controls import TradingHaltStore
from .market_sessions import SqlMarketSessionProvider
from .reconciliation_tracker import ReconciliationTracker

logger = get_logger(__name__)

MIN_PAPER_SOAK_DAYS = 14
MAX_FUTURE_TIMESTAMP_SKEW_SECONDS = 5.0


class ExecutionGatekeeper:
    """Evaluate execution admission rules without routing or submitting orders."""

    def __init__(
        self,
        *,
        allow_live: bool,
        risk_guard_enabled: bool,
        freshness: ExecutionFreshnessConfig,
        require_market_data_for_live: bool,
        sandbox_certification_marker: Path,
        trading_halt_store: TradingHaltStore,
        reconciliation_tracker: ReconciliationTracker,
        market_session_provider: SqlMarketSessionProvider | None,
    ) -> None:
        self._allow_live = allow_live
        self._risk_guard_enabled = risk_guard_enabled
        self._max_signal_age_seconds = freshness.max_signal_age_seconds
        self._max_market_data_age_seconds = freshness.max_market_data_age_seconds
        self._max_delayed_paper_market_data_age_seconds = (
            freshness.max_delayed_paper_market_data_age_seconds
        )
        self._max_account_state_age_seconds = freshness.max_account_state_age_seconds
        self._require_market_data_for_live = require_market_data_for_live
        self._sandbox_certification_marker = sandbox_certification_marker
        self._trading_halt_store = trading_halt_store
        self._reconciliation_tracker = reconciliation_tracker
        self._market_session_provider = market_session_provider

    def check_market_session(
        self,
        *,
        signal: Signal,
        environment: str,
        trace_ctx: dict[str, Any],
        allow_historical_replay: bool,
    ) -> tuple[str, str] | None:
        """Admit exposure only inside a fresh authoritative market session."""
        should_evaluate, preflight_result = self._market_session_preflight(
            signal=signal,
            environment=environment,
            trace_ctx=trace_ctx,
            allow_historical_replay=allow_historical_replay,
        )
        if not should_evaluate:
            return preflight_result
        return self._evaluate_market_session(signal=signal, trace_ctx=trace_ctx)

    def check_rebalance_execution_session(
        self,
        *,
        signal: Signal,
        expected_open_at: datetime,
        expected_content_sha256: str,
        trace_ctx: dict[str, Any],
    ) -> tuple[str, str] | None:
        """Require current authoritative content to match a frozen plan session."""
        if self._market_session_provider is None:
            msg = "Portfolio rebalance blocked: market-session provider unavailable"
            logger.warning(msg, block_reason="market_session_unavailable", **trace_ctx)
            return ("market_session_unavailable", msg)
        decision = self._market_session_provider.evaluate_pinned_rebalance_session(
            signal,
            expected_open_at=expected_open_at,
            expected_content_sha256=expected_content_sha256,
        )
        if decision.is_open:
            logger.debug(
                decision.message,
                market_session_disposition=decision.reason,
                market_calendar=decision.calendar_code,
                market_session_provider=decision.provider,
                **trace_ctx,
            )
            return None
        logger.warning(
            decision.message,
            block_reason=decision.reason,
            market_calendar=decision.calendar_code,
            market_session_provider=decision.provider,
            **trace_ctx,
        )
        return (decision.reason, decision.message)

    def check_instrument_tradability(
        self,
        *,
        signal: Signal,
        environment: str,
        trace_ctx: dict[str, Any],
    ) -> tuple[str, str] | None:
        """Block reference-only instruments before any order is constructed."""
        if environment not in {"paper", "live"}:
            return None
        if self._market_session_provider is None:
            if signal.asset_class in REFERENCE_ONLY_ASSET_CLASSES:
                msg = (
                    "Execution blocked: reference-only index requires a concrete "
                    "futures/options instrument"
                )
                logger.warning(msg, block_reason="instrument_not_tradable", **trace_ctx)
                return ("instrument_not_tradable", msg)
            return None
        decision = self._market_session_provider.evaluate_tradability(signal)
        if decision.is_open:
            return None
        logger.warning(
            decision.message,
            block_reason=decision.reason,
            **trace_ctx,
        )
        return (decision.reason, decision.message)

    @staticmethod
    def _market_session_preflight(
        *,
        signal: Signal,
        environment: str,
        trace_ctx: dict[str, Any],
        allow_historical_replay: bool,
    ) -> tuple[bool, tuple[str, str] | None]:
        if environment not in {"paper", "live"}:
            return (False, None)
        if allow_historical_replay:
            if environment == "paper":
                return (False, None)
            msg = "Live mode blocked: historical replay may execute only in paper"
            logger.warning(
                msg,
                block_reason="live_historical_replay_forbidden",
                **trace_ctx,
            )
            return (False, ("live_historical_replay_forbidden", msg))
        if signal.action == SignalAction.CLOSE:
            logger.info(
                "CLOSE signal allowed through market-session gate (reduce-only risk reduction)",
                market_session_disposition="allowed_reduce_only",
                **trace_ctx,
            )
            return (False, None)
        return (True, None)

    def _evaluate_market_session(
        self,
        *,
        signal: Signal,
        trace_ctx: dict[str, Any],
    ) -> tuple[str, str] | None:
        if self._market_session_provider is None:
            # Direct database-free engines are limited to isolated tests and
            # backtests. The deployed service refuses to start without a DB and
            # therefore always evaluates the persisted instrument policy.
            if str(signal.asset_class or "").lower() == "crypto":
                return None
            msg = "Execution blocked: authoritative market-session provider unavailable"
            logger.warning(msg, block_reason="market_session_unavailable", **trace_ctx)
            return ("market_session_unavailable", msg)

        decision = self._market_session_provider.evaluate(signal)
        if decision.is_open:
            logger.debug(
                decision.message,
                market_session_disposition=decision.reason,
                market_calendar=decision.calendar_code,
                market_session_provider=decision.provider,
                **trace_ctx,
            )
            return None
        logger.warning(
            decision.message,
            block_reason=decision.reason,
            market_calendar=decision.calendar_code,
            market_session_provider=decision.provider,
            **trace_ctx,
        )
        return (decision.reason, decision.message)

    def load_sandbox_certification_marker(self) -> dict[str, Any]:
        """Load and validate the evidence required before live execution."""
        if not self._sandbox_certification_marker.exists():
            msg = f"Expected {self._sandbox_certification_marker}"
            raise FileNotFoundError(msg)
        try:
            payload = json.loads(self._sandbox_certification_marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            msg = f"Invalid sandbox certification marker: {exc}"
            raise ValueError(msg) from exc
        if not isinstance(payload, dict):
            msg = "Sandbox certification marker must be a JSON object"
            raise ValueError(msg)  # noqa: TRY004 - decoded JSON has the wrong semantic type
        if payload.get("status") != "passed":
            msg = "Sandbox certification marker status must be 'passed'"
            raise ValueError(msg)
        required_fields = (
            "commit",
            "symbols",
            "paper_window_days",
            "operator",
            "sandbox_smoke_evidence",
            "paper_soak_evidence",
            "reconciliation_summary",
        )
        missing = [field for field in required_fields if not payload.get(field)]
        if missing:
            msg = f"Sandbox certification marker missing required fields: {', '.join(missing)}"
            raise ValueError(msg)
        if int(payload.get("paper_window_days", 0)) < MIN_PAPER_SOAK_DAYS:
            msg = "Sandbox certification marker requires at least 14 paper soak days"
            raise ValueError(msg)
        if int(payload.get("duplicate_submission_count", 0) or 0) != 0:
            msg = "Sandbox certification marker requires zero duplicate submissions"
            raise ValueError(msg)
        return payload

    def check_signal_freshness(
        self,
        *,
        signal: Signal,
        environment: str,
        trace_ctx: dict[str, Any],
    ) -> str | None:
        """Block stale entry signals in paper/live while allowing reduce-only exits."""
        if environment not in {"paper", "live"}:
            return None
        environment_label = environment.capitalize()
        now = datetime.now(tz=UTC)
        expiry_error = self._check_signal_expiry(
            signal=signal,
            environment=environment,
            environment_label=environment_label,
            now=now,
            trace_ctx=trace_ctx,
        )
        if expiry_error is not None:
            return expiry_error
        return self._check_signal_timestamp_freshness(
            signal=signal,
            environment=environment,
            environment_label=environment_label,
            now=now,
            trace_ctx=trace_ctx,
        )

    @staticmethod
    def _check_signal_expiry(
        *,
        signal: Signal,
        environment: str,
        environment_label: str,
        now: datetime,
        trace_ctx: dict[str, Any],
    ) -> str | None:
        expires_at = getattr(signal, "expires_at", None)
        if expires_at is None:
            return None
        if not isinstance(expires_at, datetime):
            msg = f"{environment_label} mode blocked: signal expiry is invalid"
            logger.warning(msg, block_reason="signal_expiry_invalid", **trace_ctx)
            return msg
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if now < expires_at.astimezone(UTC):
            return None
        if signal.action == SignalAction.CLOSE:
            logger.warning(
                "Expired CLOSE signal allowed through %s freshness gate (reduce-only exemption)",
                environment,
                block_reason="expired_close_exempt",
                **trace_ctx,
            )
            return None
        msg = (
            f"{environment_label} mode blocked: signal expired at "
            f"{expires_at.astimezone(UTC).isoformat()}"
        )
        logger.warning(msg, block_reason="expired_signal", **trace_ctx)
        return msg

    def _check_signal_timestamp_freshness(
        self,
        *,
        signal: Signal,
        environment: str,
        environment_label: str,
        now: datetime,
        trace_ctx: dict[str, Any],
    ) -> str | None:
        timestamp = getattr(signal, "timestamp", None)
        if timestamp is None or not isinstance(timestamp, datetime):
            msg = f"{environment_label} mode blocked: signal timestamp unavailable"
            logger.warning(msg, block_reason="signal_timestamp_missing", **trace_ctx)
            return msg
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        age_seconds = (now - timestamp.astimezone(UTC)).total_seconds()
        if age_seconds < -MAX_FUTURE_TIMESTAMP_SKEW_SECONDS:
            msg = (
                f"{environment_label} mode blocked: signal timestamp is "
                f"{-age_seconds:.1f}s in the future"
            )
            logger.warning(msg, block_reason="signal_timestamp_future", **trace_ctx)
            return msg
        if age_seconds <= self._max_signal_age_seconds:
            return None
        if signal.action == SignalAction.CLOSE:
            logger.warning(
                "Stale CLOSE signal allowed through %s freshness gate: "
                "age %.1fs exceeds %ss (reduce-only exemption)",
                environment,
                age_seconds,
                self._max_signal_age_seconds,
                block_reason="stale_close_exempt",
                **trace_ctx,
            )
            return None
        msg = (
            f"{environment_label} mode blocked: signal age {age_seconds:.1f}s exceeds "
            f"{self._max_signal_age_seconds}s"
        )
        logger.warning(msg, block_reason="stale_signal", **trace_ctx)
        return msg

    def check_account_state_freshness(
        self,
        *,
        account_state: AccountState,
        environment: str,
        trace_ctx: dict[str, Any],
        require_fresh: bool = False,
    ) -> tuple[str, str] | None:
        """Return a retryable block for stale live or explicitly required state."""
        if environment != "live" and not require_fresh:
            return None
        mode = "Live mode" if environment == "live" else "Paper funding"
        timestamp = getattr(account_state, "fetched_at", None)
        if timestamp is None or not isinstance(timestamp, datetime):
            msg = f"{mode} blocked: account state timestamp unavailable"
            logger.warning(msg, block_reason="account_state_timestamp_missing", **trace_ctx)
            return ("account_state_timestamp_missing", msg)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        age_seconds = (datetime.now(tz=UTC) - timestamp.astimezone(UTC)).total_seconds()
        if age_seconds < -MAX_FUTURE_TIMESTAMP_SKEW_SECONDS:
            msg = f"{mode} blocked: account state timestamp is {-age_seconds:.1f}s in the future"
            logger.warning(msg, block_reason="account_state_timestamp_future", **trace_ctx)
            return ("account_state_timestamp_future", msg)
        if age_seconds > self._max_account_state_age_seconds:
            msg = (
                f"{mode} blocked: account state age {age_seconds:.1f}s exceeds "
                f"{self._max_account_state_age_seconds}s"
            )
            logger.warning(msg, block_reason="account_state_stale", **trace_ctx)
            return ("account_state_stale", msg)
        return None

    def check_market_data_freshness(  # noqa: PLR0911 - explicit fail-closed guards
        self,
        *,
        market_data: dict[str, Any],
        environment: str,
        trace_ctx: dict[str, Any],
    ) -> tuple[str, str] | None:
        """Reject stale quotes and prevent paper-only evidence from reaching live."""
        authority = str(market_data.get("execution_authority") or "shared")
        if authority == "paper_only" and environment != "paper":
            msg = "Live mode blocked: paper-only delayed market data is not live authority"
            logger.warning(msg, block_reason="paper_only_market_data_live", **trace_ctx)
            return ("paper_only_market_data_live", msg)
        is_delayed_paper = authority == "paper_only" and environment == "paper"
        if environment != "live" and not is_delayed_paper:
            return None
        if not market_data and (self._require_market_data_for_live or is_delayed_paper):
            msg = "Live mode blocked: market data unavailable"
            logger.warning(msg, block_reason="market_data_missing", **trace_ctx)
            return ("market_data_missing", msg)
        if not market_data:
            return None
        price = market_data.get("price")
        if (self._require_market_data_for_live or is_delayed_paper) and price is None:
            msg = "Live mode blocked: market data price unavailable"
            logger.warning(msg, block_reason="market_data_price_missing", **trace_ctx)
            return ("market_data_price_missing", msg)
        timestamp = self.market_data_timestamp(market_data)
        if timestamp is None:
            msg = "Live mode blocked: market data timestamp unavailable"
            logger.warning(msg, block_reason="market_data_timestamp_missing", **trace_ctx)
            return ("market_data_timestamp_missing", msg)
        age_seconds = (datetime.now(tz=UTC) - timestamp).total_seconds()
        if age_seconds < -MAX_FUTURE_TIMESTAMP_SKEW_SECONDS:
            msg = f"Live mode blocked: market data timestamp is {-age_seconds:.1f}s in the future"
            logger.warning(msg, block_reason="market_data_timestamp_future", **trace_ctx)
            return ("market_data_timestamp_future", msg)
        max_age_seconds = (
            self._max_delayed_paper_market_data_age_seconds
            if is_delayed_paper
            else self._max_market_data_age_seconds
        )
        if age_seconds > max_age_seconds:
            msg = (
                f"{'Paper' if is_delayed_paper else 'Live'} mode blocked: market data age "
                f"{age_seconds:.1f}s exceeds {max_age_seconds}s"
            )
            logger.warning(msg, block_reason="stale_market_data", **trace_ctx)
            return ("stale_market_data", msg)
        return None

    def check_rebalance_market_data_freshness(  # noqa: PLR0911 - fail-closed quote guards
        self,
        *,
        market_data: dict[str, Any],
        expected_open_at: datetime,
        environment: str,
        trace_ctx: dict[str, Any],
    ) -> tuple[str, str] | None:
        """Require an actionable current-session quote for portfolio targets."""

        authority = str(market_data.get("execution_authority") or "shared")
        if authority == "paper_only" and environment != "paper":
            msg = "Portfolio rebalance blocked: paper-only delayed quote is not live authority"
            logger.warning(
                msg,
                block_reason="rebalance_paper_only_market_data_live",
                **trace_ctx,
            )
            return ("rebalance_paper_only_market_data_live", msg)
        if not market_data:
            msg = "Portfolio rebalance blocked: current-session market data unavailable"
            logger.warning(msg, block_reason="rebalance_market_data_missing", **trace_ctx)
            return ("rebalance_market_data_missing", msg)
        raw_price = market_data.get("price")
        try:
            price = float(raw_price) if raw_price is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0
        if not math.isfinite(price) or price <= 0:
            msg = "Portfolio rebalance blocked: quote price is not finite and positive"
            logger.warning(msg, block_reason="rebalance_market_data_price_invalid", **trace_ctx)
            return ("rebalance_market_data_price_invalid", msg)
        timestamp = self.market_data_timestamp(market_data)
        if timestamp is None:
            msg = "Portfolio rebalance blocked: quote timestamp unavailable"
            logger.warning(
                msg,
                block_reason="rebalance_market_data_timestamp_missing",
                **trace_ctx,
            )
            return ("rebalance_market_data_timestamp_missing", msg)
        session_open = (
            expected_open_at.replace(tzinfo=UTC)
            if expected_open_at.tzinfo is None
            else expected_open_at.astimezone(UTC)
        )
        if timestamp < session_open:
            msg = "Portfolio rebalance blocked: quote predates the pinned execution session"
            logger.warning(
                msg,
                block_reason="rebalance_market_data_before_session",
                quote_timestamp=timestamp.isoformat(),
                execution_session_open=session_open.isoformat(),
                **trace_ctx,
            )
            return ("rebalance_market_data_before_session", msg)
        age_seconds = (datetime.now(tz=UTC) - timestamp).total_seconds()
        if age_seconds < -MAX_FUTURE_TIMESTAMP_SKEW_SECONDS:
            msg = (
                f"Portfolio rebalance blocked: quote timestamp is {-age_seconds:.1f}s in the future"
            )
            logger.warning(
                msg,
                block_reason="rebalance_market_data_timestamp_future",
                **trace_ctx,
            )
            return ("rebalance_market_data_timestamp_future", msg)
        max_age_seconds = (
            self._max_delayed_paper_market_data_age_seconds
            if authority == "paper_only" and environment == "paper"
            else self._max_market_data_age_seconds
        )
        if age_seconds > max_age_seconds:
            msg = (
                f"Portfolio rebalance blocked: quote age {age_seconds:.1f}s exceeds "
                f"{max_age_seconds}s"
            )
            logger.warning(msg, block_reason="rebalance_market_data_stale", **trace_ctx)
            return ("rebalance_market_data_stale", msg)
        return None

    @staticmethod
    def market_data_timestamp(market_data: dict[str, Any]) -> datetime | None:
        for key in (
            "timestamp",
            "ts",
            "as_of",
            "source_ts",
            "price_ts",
            "updated_at",
            "fetched_at",
        ):
            raw = market_data.get(key)
            if raw is None:
                continue
            if isinstance(raw, datetime):
                timestamp = raw
            elif isinstance(raw, str):
                try:
                    timestamp = datetime.fromisoformat(raw)
                except ValueError:
                    continue
            else:
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return timestamp.astimezone(UTC)
        return None

    def check_live_mode(  # noqa: PLR0911 - explicit fail-closed guards
        self,
        *,
        environment: str,
        mode: str,
        profile: dict[str, Any],
        user_strategy_config: dict[str, Any],
        trace_ctx: dict[str, Any],
    ) -> str | None:
        """Validate every live-trading precondition in fail-closed order."""
        halt_error = self._check_trading_halt(environment=environment, trace_ctx=trace_ctx)
        if halt_error is not None:
            return halt_error
        if environment == "live" and not self._allow_live:
            msg = (
                "Live mode blocked: EXECUTION_ENGINE_ALLOW_LIVE=false. "
                "Set EXECUTION_ENGINE_ALLOW_LIVE=true to enable live trading."
            )
            logger.warning(msg, block_reason="allow_live_disabled", **trace_ctx)
            return msg
        if environment == "live" and mode != "live":
            msg = (
                "Live mode blocked: broker route targets the LIVE environment but "
                f"the engine execution mode is '{mode}'. Live submission requires "
                "mode=live AND EXECUTION_ENGINE_ALLOW_LIVE=true AND a live broker "
                "route; refusing to submit live or silently downgrade to paper."
            )
            logger.warning(msg, block_reason="live_route_mode_mismatch", **trace_ctx)
            return msg
        if environment == "live" and not (
            profile.get("live_enabled") or user_strategy_config.get("live_enabled")
        ):
            msg = (
                "Live mode blocked: user profile live_enabled=false. "
                "Enable live_enabled in user profile or strategy config."
            )
            logger.warning(msg, block_reason="live_not_enabled_for_user", **trace_ctx)
            return msg
        if environment == "live" and not self._risk_guard_enabled:
            msg = "Live mode blocked: risk guard must remain enabled"
            logger.warning(msg, block_reason="risk_guard_disabled", **trace_ctx)
            return msg
        if environment == "live":
            try:
                self.load_sandbox_certification_marker()
            except (FileNotFoundError, TypeError, ValueError) as exc:
                msg = f"Live mode blocked: sandbox certification marker missing or invalid. {exc}"
                logger.warning(
                    msg,
                    block_reason="sandbox_certification_missing",
                    **trace_ctx,
                )
                return msg
        if environment == "live" and not self._reconciliation_tracker.is_healthy():
            msg = "Live mode blocked: reconciliation worker is not healthy"
            logger.warning(
                msg,
                block_reason="reconciliation_unhealthy",
                reconciliation_status=self._reconciliation_tracker.status(),
                **trace_ctx,
            )
            return msg
        return None

    def _check_trading_halt(
        self,
        *,
        environment: str,
        trace_ctx: dict[str, Any],
    ) -> str | None:
        if environment != "live":
            return None
        state = self._trading_halt_store.get_state()
        if not state.enabled:
            return None
        msg = f"Live mode blocked: global trading halt is enabled ({state.reason or 'no reason'})"
        logger.warning(msg, block_reason="global_trading_halt", **trace_ctx)
        return msg

    @staticmethod
    def check_score_thresholds(
        *,
        score_ctx: dict[str, Any],
        user_strategy_config: dict[str, Any],
        trace_ctx: dict[str, Any],
    ) -> str | None:
        """Check optional per-strategy asset and sector score thresholds."""
        min_score = user_strategy_config.get("min_score")
        min_sector_score = user_strategy_config.get("min_sector_score")
        asset_score = score_ctx.get("asset_score")
        sector_score = score_ctx.get("sector_score")
        if min_score is not None and asset_score is not None and asset_score < float(min_score):
            msg = f"Score {asset_score} below threshold {min_score}"
            logger.info(msg, **trace_ctx)
            return msg
        if (
            min_sector_score is not None
            and sector_score is not None
            and sector_score < float(min_sector_score)
        ):
            msg = f"Sector score {sector_score} below threshold {min_sector_score}"
            logger.info(msg, **trace_ctx)
            return msg
        return None


__all__ = ["ExecutionGatekeeper"]
