"""Account- and market-state lookups for the execution engine.

Extracted from ``apps/execution_engine/execution_engine/engine.py`` as the
first step of the Phase-3 ExecutionEngine decomposition. This module owns the
read-side of execution: fetching account info from a broker (with profile
fallback) and quoting symbols from the market-data provider.

The class is intentionally narrow — it does not mutate engine state, does not
emit events, and does not touch persistence beyond the daily-trade-count
counter. ``ExecutionEngine`` keeps the same private method names
(``_get_account_state``, ``_get_market_data``, ``_account_from_profile``,
``_get_daily_trade_count``) as thin delegates so existing tests that patch
those names keep working.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from lib_common.config_validation import normalize_broker_code
from lib_common.logging import get_logger
from lib_common.retries import retry_async

from .brokers.base import BrokerAdapter
from .config import AccountState, BrokerType

logger = get_logger(__name__)
_MAX_CURRENCY_CODE_LENGTH = 10


class StateProvider:
    """Read-only adapter over broker + market-data lookups.

    Responsibilities:
        * Fetch live account state from a ``BrokerAdapter`` with retries.
        * Derive a fallback ``AccountState`` from the user profile when the
          broker call fails or no broker is wired.
        * Fetch a market quote for a symbol via the market-data provider,
          returning an empty dict on any failure (callers fall back to signal
          prices).

    Args:
        execution_log_store: Optional store exposing ``count_daily_trades``.
            When ``None`` the daily-trade counter falls back to the
            ``profile['daily_trades']`` value.
        market_data_provider: Optional object exposing
            ``async get_quote(symbol, *, user_id, environment)``.
    """

    def __init__(
        self,
        *,
        execution_log_store: Any | None = None,
        market_data_provider: Any | None = None,
    ) -> None:
        self._execution_log_store = execution_log_store
        self._market_data_provider = market_data_provider

    # ------------------------------------------------------------------
    # Account state
    # ------------------------------------------------------------------

    async def get_account_state(
        self,
        *,
        user_id: str,
        broker: BrokerAdapter,
        profile: dict[str, Any] | None = None,
        allow_profile_fallback: bool = True,
    ) -> AccountState:
        """Fetch broker account state with optional profile-cache fallback."""
        broker_type = self._broker_type_from_profile(profile)
        try:
            account_info = await retry_async(
                lambda: broker.get_account_info(),
                retries=2,
                base_delay=0.5,
                max_delay=5.0,
                operation_name="broker_account_info",
                log=logger,
            )
            daily_trades = self.get_daily_trade_count(user_id=user_id, profile=profile)
            state = AccountState(
                broker=account_info.broker,
                equity=account_info.equity,
                available_cash=account_info.available_balance,
                margin_used=account_info.margin_used,
                unrealized_pnl=account_info.unrealized_pnl,
                realized_pnl=account_info.realized_pnl,
                open_positions=len(account_info.positions),
                daily_trades=daily_trades,
                account_id=account_info.account_id,
                currency=account_info.currency,
                positions=[
                    {
                        "symbol": pos.symbol,
                        "side": pos.side,
                        "quantity": pos.quantity,
                        "entry_price": pos.entry_price,
                        "current_price": pos.current_price,
                        "unrealized_pnl": pos.unrealized_pnl,
                        "realized_pnl": pos.realized_pnl,
                        "margin_used": pos.margin_used,
                        "liquidation_price": pos.liquidation_price,
                        "quantity_unit": pos.quantity_unit,
                        "contract_multiplier": pos.contract_multiplier,
                        "leverage": pos.leverage,
                        "contract_type": pos.contract_type,
                        "gross_notional": pos.gross_notional,
                        "notional_currency": pos.notional_currency,
                    }
                    for pos in account_info.positions
                ],
                balances=list(account_info.balances),
                fetched_at=self._normalize_fetched_at(account_info.fetched_at),
                source="broker",
            )
            self._validate_account_state(state)
            return state  # noqa: TRY300 - exception boundary includes validation
        except Exception:
            if not allow_profile_fallback:
                msg = "Live mode blocked: broker account state unavailable"
                logger.exception(msg)
                raise RuntimeError(msg) from None
            # Boundary catch: paper execution may use only an explicit, fresh
            # profile cache. Missing capital or currency blocks sizing.
            logger.exception(
                "Error fetching account state from broker; validating profile account cache"
            )
            return self.account_from_profile(profile, broker_type)

    def get_daily_trade_count(
        self,
        *,
        user_id: str,
        profile: dict[str, Any] | None = None,
    ) -> int:
        """Return today's trade count for ``user_id`` (DB-backed when wired)."""
        fallback = int((profile or {}).get("daily_trades", 0) or 0)
        if self._execution_log_store is None:
            return fallback
        try:
            return int(self._execution_log_store.count_daily_trades(user_id=user_id))
        except Exception:
            # Boundary catch: persistence failure must not block execution
            # — fall back to the cached profile counter.
            logger.exception("Failed to load daily trade count", user_id=user_id)
            return fallback

    @staticmethod
    def account_from_profile(
        profile: dict[str, Any] | None,
        broker_type: BrokerType | None = None,
    ) -> AccountState:
        """Validate an explicit cached account state without financial defaults."""
        if not profile:
            msg = "Profile account cache is unavailable"
            raise RuntimeError(msg)

        equity = StateProvider._required_float(profile, "equity", positive=True)
        cash_key = "available_cash" if "available_cash" in profile else "cash"
        available = StateProvider._required_float(profile, cash_key, nonnegative=True)
        margin_used = StateProvider._required_float(profile, "margin_used", nonnegative=True)
        unrealized_raw = profile.get("unrealized_pnl")
        unrealized = float(unrealized_raw) if unrealized_raw is not None else None
        realized = profile.get("realized_pnl")
        positions = list(profile.get("positions") or [])
        open_positions = int(profile.get("open_positions", len(positions)))
        daily_trades = int(profile.get("daily_trades", 0))

        if broker_type is None:
            broker_type = StateProvider._broker_type_from_profile(profile)

        route = dict(profile.get("_broker_route_snapshot") or {})
        raw_account_id = route.get("broker_account_id", profile.get("broker_account_id"))
        if raw_account_id is None or isinstance(raw_account_id, bool):
            msg = "Profile account cache requires broker_account_id"
            raise RuntimeError(msg)
        try:
            account_id = int(raw_account_id)
        except (TypeError, ValueError) as exc:
            msg = "Profile account cache broker_account_id must be a positive integer"
            raise RuntimeError(msg) from exc
        if account_id <= 0:
            msg = "Profile account cache broker_account_id must be a positive integer"
            raise RuntimeError(msg)

        currency = str(profile.get("currency") or profile.get("base_ccy") or "").strip().upper()
        if not currency or len(currency) > _MAX_CURRENCY_CODE_LENGTH or not currency.isalnum():
            msg = "Profile account cache requires an explicit valid base currency"
            raise RuntimeError(msg)

        state = AccountState(
            broker=broker_type,
            equity=equity,
            available_cash=available,
            margin_used=margin_used,
            unrealized_pnl=unrealized,
            realized_pnl=(float(realized) if realized is not None else None),
            open_positions=open_positions,
            daily_trades=daily_trades,
            account_id=str(account_id),
            currency=currency,
            positions=positions,
            fetched_at=StateProvider._profile_fetched_at(profile),
            source="profile_cache",
        )
        StateProvider._validate_account_state(state)
        return state

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def get_market_data(
        self,
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> dict[str, Any]:
        """Fetch a market quote for ``symbol``, returning ``{}`` on any failure."""
        provider = self._market_data_provider
        if provider is None:
            return {}
        try:
            result = await retry_async(
                lambda: provider.get_quote(
                    symbol,
                    user_id=user_id,
                    environment=environment,
                ),
                retries=2,
                base_delay=0.5,
                max_delay=5.0,
                operation_name="market_data_fetch",
                log=logger,
            )
            return dict(result) if result else {}
        except Exception:
            # Boundary catch: market-data failure must not block execution.
            # The order builder will fall back to the signal's prices.
            logger.exception(
                "Error fetching market data for %s",
                symbol,
                user_id=user_id,
                environment=environment,
            )
            return {}

    @staticmethod
    def _normalize_fetched_at(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _profile_fetched_at(profile: dict[str, Any]) -> datetime:
        for key in ("account_state_fetched_at", "last_synced_at", "fetched_at"):
            raw = profile.get(key)
            if isinstance(raw, datetime):
                return StateProvider._normalize_fetched_at(raw)
            if isinstance(raw, str):
                try:
                    parsed = datetime.fromisoformat(raw)
                except ValueError:
                    continue
                return StateProvider._normalize_fetched_at(parsed)
        msg = "Profile account cache requires an explicit fetched timestamp"
        raise RuntimeError(msg)

    @staticmethod
    def _broker_type_from_profile(profile: dict[str, Any] | None) -> BrokerType:
        raw_broker = (profile or {}).get("broker")
        if not isinstance(raw_broker, str) or not raw_broker.strip():
            msg = "Account-state resolution requires an explicit broker"
            raise RuntimeError(msg)
        try:
            return BrokerType(normalize_broker_code(raw_broker))
        except ValueError as exc:
            msg = f"Account-state resolution received unsupported broker {raw_broker!r}"
            raise RuntimeError(msg) from exc

    @staticmethod
    def _required_float(
        profile: dict[str, Any],
        key: str,
        *,
        positive: bool = False,
        nonnegative: bool = False,
    ) -> float:
        if key not in profile or profile[key] is None or isinstance(profile[key], bool):
            msg = f"Profile account cache requires {key}"
            raise RuntimeError(msg)
        try:
            value = float(profile[key])
        except (TypeError, ValueError) as exc:
            msg = f"Profile account cache {key} must be numeric"
            raise RuntimeError(msg) from exc
        if not math.isfinite(value):
            msg = f"Profile account cache {key} must be finite"
            raise RuntimeError(msg)
        if positive and value <= 0:
            msg = f"Profile account cache {key} must be positive"
            raise RuntimeError(msg)
        if nonnegative and value < 0:
            msg = f"Profile account cache {key} cannot be negative"
            raise RuntimeError(msg)
        return value

    @staticmethod
    def _validate_account_state(state: AccountState) -> None:
        for field_name, value in (
            ("equity", state.equity),
            ("available_cash", state.available_cash),
            ("margin_used", state.margin_used),
        ):
            if not math.isfinite(float(value)) or float(value) < 0:
                msg = f"Broker account state has invalid {field_name}"
                raise RuntimeError(msg)
        if state.equity <= 0:
            msg = "Broker account state equity must be positive"
            raise RuntimeError(msg)
        currency = str(state.currency or "").strip().upper()
        if not currency or len(currency) > _MAX_CURRENCY_CODE_LENGTH or not currency.isalnum():
            msg = "Broker account state requires an explicit valid currency"
            raise RuntimeError(msg)
        if not str(state.account_id or "").strip():
            msg = "Broker account state requires an explicit account_id"
            raise RuntimeError(msg)
        if state.source not in {"broker", "profile_cache", "unattributed"}:
            msg = "Broker account state has an invalid source"
            raise RuntimeError(msg)
