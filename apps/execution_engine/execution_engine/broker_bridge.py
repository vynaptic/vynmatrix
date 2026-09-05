"""Bridge between execution-engine order intents and infrastructure broker adapters."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

from execution_engine.brokers.base import (
    AccountInfo,
    BrokerAdapter,
    BrokerCapabilities,
    BrokerFill,
    BrokerOrderResult,
    OrderStatus,
    Position,
)
from execution_engine.config import BrokerType, ExecutionMode
from execution_engine.models import OptionsIntent, OrderIntent, client_order_id_for
from lib_common.logging import get_logger
from lib_infrastructure.brokers.capabilities import BROKER_SPECS
from lib_infrastructure.brokers.factory import (
    BrokerFactory,
    register_default_adapters,
)
from lib_infrastructure.brokers.ports import IBrokerAdapter
from lib_infrastructure.brokers.secrets import ISecretsProvider, create_secrets_provider
from lib_strategy.types import (
    AccountBalance,
    BrokerExecutionMethod,
    BrokerOrderIntent,
    PositionQuantityUnit,
)
from lib_strategy.types import (
    BrokerFill as BrokerFillWire,
)

from .metrics.fx_rates import (
    FXConverter,
    FXRateProvider,
    FXRateUnavailableError,
    normalize_currency,
)

logger = get_logger(__name__)

# Derived from the single BROKER_SPECS source of truth (in lib_infrastructure)
# so this BrokerType -> factory_code map can never drift from the adapter
# registration codes. ``BrokerType(spec.code)`` is safe: every spec code is a
# canonical BrokerType value.
_FACTORY_BROKER_CODES: dict[BrokerType, str] = {
    BrokerType(spec.code): spec.factory_code for spec in BROKER_SPECS
}

_SUPPORTED_POSITION_NOTIONAL_TYPES = {
    "spot",
    "linear",
    "vanilla",
    "broker_mark_value",
    "broker_notional",
}


class BrokerValuationError(RuntimeError):
    """Broker account data cannot support a safe execution valuation."""


@dataclass(frozen=True)
class _PositionValuation:
    position: Position
    gross_notional: Decimal
    unrealized_pnl: Decimal | None
    realized_pnl: Decimal | None


def _finite_decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        msg = f"Invalid monetary value: {value!r}"
        raise ValueError(msg) from exc
    if not parsed.is_finite():
        msg = f"Invalid monetary value: {value!r}"
        raise ValueError(msg)
    return parsed


def _symbol_pair_currency(symbol: str, *, base: bool) -> str | None:
    for separator in ("/", "-", "_"):
        if separator not in symbol:
            continue
        parts = [part.strip().upper() for part in symbol.split(separator) if part.strip()]
        if len(parts) != 2:  # noqa: PLR2004
            continue
        candidate = parts[0] if base else parts[1]
        if 2 <= len(candidate) <= 5 and candidate.isalnum():  # noqa: PLR2004
            return candidate
    return None


def _position_asset_currency(raw: Mapping[str, Any]) -> str | None:
    candidate = (
        raw.get("asset_currency")
        or raw.get("base_currency")
        or raw.get("currency")
        or _symbol_pair_currency(str(raw.get("symbol") or ""), base=True)
    )
    if not candidate:
        return None
    return normalize_currency(str(candidate))


def _position_price_currency(raw: Mapping[str, Any], symbol: str) -> str | None:
    candidate = (
        raw.get("quote_currency")
        or raw.get("settlement_currency")
        or _symbol_pair_currency(symbol, base=False)
        or raw.get("currency")
    )
    if not candidate:
        return None
    return normalize_currency(str(candidate))


def _position_pnl_currency(raw: Mapping[str, Any]) -> str | None:
    candidate = raw.get("pnl_currency") or raw.get("settlement_currency") or raw.get("currency")
    if not candidate:
        return None
    return normalize_currency(str(candidate))


class MutableSecretsProvider(ISecretsProvider):
    """Secrets provider that supports inline credentials with env fallback."""

    def __init__(self, fallback: ISecretsProvider | None = None) -> None:
        # Default to the configured backend (SECRETS_BACKEND: env|db|composite).
        # Env stays the default so local dev is unchanged; production sets
        # SECRETS_BACKEND=db (+ SECRETS_MASTER_KEYS) for per-user encrypted secrets
        # resolved from Postgres — no per-user env vars, no redeploy to onboard.
        self._fallback = fallback or create_secrets_provider()
        self._inline: dict[str, str] = {}

    def set_inline_credentials(self, secret_ref: str, credentials: dict[str, str]) -> None:
        self._inline[secret_ref] = json.dumps(
            {
                "api_key": credentials.get("api_key", ""),
                "api_secret": credentials.get("api_secret", ""),
                "passphrase": credentials.get("passphrase"),
                **(
                    {
                        k: v
                        for k, v in credentials.items()
                        if k not in {"api_key", "api_secret", "passphrase"}
                    }
                ),
            }
        )

    async def get_secret(self, secret_ref: str) -> str | None:
        if secret_ref in self._inline:
            return self._inline[secret_ref]
        secret = await self._fallback.get_secret(secret_ref)
        return None if secret is None else str(secret)


def create_execution_broker_factory(secrets_provider: ISecretsProvider) -> BrokerFactory:
    factory = BrokerFactory(secrets_provider)
    register_default_adapters(factory)
    return factory


def resolve_credential_ref(
    *,
    broker_type: BrokerType,
    profile: dict[str, Any],
    user_strategy_config: dict[str, Any],
    environment: str,
    broker_account_id: int | None = None,
) -> str:
    broker_code = broker_type.value
    # Resolve strictly from the ROUTED broker account: with two accounts at the
    # same broker (e.g. paper + live), the old broker-code-keyed summary entry
    # preferred the live account and systematically returned the OTHER
    # account's secret, which validate_current_authority then rejected —
    # blocking every execution on that route. An explicit strategy-config
    # override still wins over the account pointer.
    account_snapshot: dict[str, Any] = {}
    if broker_account_id is not None:
        account_snapshot = (profile.get("accounts") or {}).get(str(broker_account_id)) or {}
    resolved = user_strategy_config.get("credential_ref") or account_snapshot.get("credential_ref")
    if resolved:
        return str(resolved)
    if environment == "paper":
        # Paper trading needs no real secret; a deterministic non-secret ref is fine.
        return f"{broker_code}-{environment}"
    # Live with no per-account credential resolved: do NOT fabricate a shared,
    # guessable reference (it would collide users and risk trading on the wrong
    # account). Return an unresolvable sentinel so the secrets lookup fails and the
    # engine produces a structured blocked result instead of silently sharing creds.
    logger.warning(
        "No per-account credential_ref resolved for live execution; blocking order",
        broker=broker_code,
        environment=environment,
    )
    return f"__unresolved__:{broker_code}:{environment}"


class BrokerBridge(BrokerAdapter):
    """BrokerAdapter implementation backed by the infrastructure broker factory."""

    def __init__(
        self,
        *,
        broker_type: BrokerType,
        environment: str,
        credential_ref: str,
        credentials: dict[str, str] | None = None,
        account_currency: str,
        settlement_currency: str,
        fx_rate_provider: FXRateProvider | None = None,
    ) -> None:
        super().__init__(sandbox=environment == "paper")
        self._broker_type = broker_type
        self._environment = environment
        self._credential_ref = credential_ref
        self._account_currency = normalize_currency(account_currency)
        self._settlement_currency = normalize_currency(settlement_currency)
        self._fx_converter = FXConverter(fx_rate_provider) if fx_rate_provider is not None else None
        self._inline_credentials = credentials or {}
        self._adapter: IBrokerAdapter | None = None
        self._secrets_provider = MutableSecretsProvider()
        self._factory = create_execution_broker_factory(self._secrets_provider)

        if self._inline_credentials:
            self._secrets_provider.set_inline_credentials(
                self._credential_ref,
                self._inline_credentials,
            )

    @property
    def capabilities(self) -> BrokerCapabilities:
        infra_caps = getattr(self._adapter, "capabilities", {}) or {}
        execution_methods = set(infra_caps.get("execution_methods", []))
        return BrokerCapabilities(
            broker_type=self._broker_type,
            supports_spot=BrokerExecutionMethod.SPOT.value in execution_methods,
            supports_margin=BrokerExecutionMethod.MARGIN.value in execution_methods,
            supports_futures=BrokerExecutionMethod.FUTURES.value in execution_methods,
            supports_perpetual=BrokerExecutionMethod.PERPETUAL.value in execution_methods,
            supports_options=(
                BrokerExecutionMethod.OPTIONS_SINGLE.value in execution_methods
                or BrokerExecutionMethod.OPTIONS_STRATEGY.value in execution_methods
            ),
            supports_multi_leg=bool(infra_caps.get("multi_leg_orders")),
            supports_exact_fill_retrieval=bool(infra_caps.get("exact_fill_retrieval")),
            supported_assets=list(infra_caps.get("asset_classes", ["crypto"])),
            max_leverage=float(infra_caps.get("max_leverage", 1.0) or 1.0),
            has_paper_trading=bool(
                (infra_caps.get("paper_trading") or {}).get("type") or self._environment == "paper"
            ),
            paper_base_url=(infra_caps.get("paper_trading") or {}).get("base_url"),
        )

    async def connect(self) -> bool:
        broker_code = _FACTORY_BROKER_CODES.get(self._broker_type)
        if broker_code is None:
            return False
        self._adapter = await self._factory.get_adapter(
            broker_code=broker_code,
            credential_ref=self._credential_ref,
            environment=self._environment,
        )
        if self._adapter is not None and self._broker_type == BrokerType.COINBASE:
            configure_settlement = getattr(
                self._adapter,
                "configure_settlement_currency",
                None,
            )
            if not callable(configure_settlement):
                msg = "Coinbase adapter cannot accept account-scoped settlement currency"
                raise BrokerValuationError(msg)
            configure_settlement(self._settlement_currency)
        self._connected = bool(self._adapter is not None)
        return self._connected

    async def disconnect(self) -> None:
        if self._adapter is not None:
            broker_code = _FACTORY_BROKER_CODES.get(self._broker_type)
            if broker_code is not None:
                await self._factory.disconnect_adapter(
                    broker_code=broker_code,
                    credential_ref=self._credential_ref,
                    environment=self._environment,
                )
            self._adapter = None
        self._connected = False

    async def get_account_info(self) -> AccountInfo:
        adapter = await self._require_adapter()
        snapshot = await adapter.get_account_snapshot()
        positions_raw = snapshot["positions"]
        observed_at = datetime.now(tz=UTC)
        valuations = self._normalize_positions(positions_raw, observed_at=observed_at)
        gross_exposure = sum(
            (valuation.gross_notional for valuation in valuations),
            Decimal("0"),
        )
        equity, available_cash, margin_used = self._account_values(
            snapshot,
            gross_exposure=gross_exposure,
            observed_at=observed_at,
        )

        return AccountInfo(
            broker=self._broker_type,
            account_id=self._credential_ref,
            equity=float(equity),
            available_balance=float(available_cash),
            margin_used=float(margin_used),
            unrealized_pnl=self._complete_pnl(valuation.unrealized_pnl for valuation in valuations),
            realized_pnl=self._complete_pnl(valuation.realized_pnl for valuation in valuations),
            positions=[valuation.position for valuation in valuations],
            balances=self._account_balances(snapshot),
            currency=self._account_currency,
        )

    async def _require_adapter(self) -> IBrokerAdapter:
        if self._adapter is None:
            await self.connect()
        if self._adapter is None:
            msg = f"Broker adapter unavailable for {self._broker_type.value}"
            raise ConnectionError(msg)
        return self._adapter

    def _normalize_positions(
        self,
        positions: Sequence[Mapping[str, Any]],
        *,
        observed_at: datetime,
        normalize_to_account: bool = True,
    ) -> list[_PositionValuation]:
        normalized: list[_PositionValuation] = []
        for raw in positions:
            valuation = self._normalize_position(
                raw,
                observed_at=observed_at,
                normalize_to_account=normalize_to_account,
            )
            if valuation is not None:
                normalized.append(valuation)
        return normalized

    def _normalize_position(
        self,
        raw: Mapping[str, Any],
        *,
        observed_at: datetime,
        normalize_to_account: bool,
    ) -> _PositionValuation | None:
        quantity = _finite_decimal(raw.get("quantity") or 0)
        if quantity == 0:
            return None
        symbol, side, quantity_unit, notional_type = self._position_identity(raw, quantity)
        current_price, entry_price, price_currency = self._position_prices(
            raw,
            symbol=symbol,
            observed_at=observed_at,
            normalize_to_account=normalize_to_account,
        )
        pnl_currency = _position_pnl_currency(raw) or price_currency
        require_pnl = notional_type != "spot"
        unrealized = self._position_money(
            raw,
            field="unrealized_pnl",
            symbol=symbol,
            currency=pnl_currency,
            observed_at=observed_at,
            required=require_pnl,
            normalize_to_account=normalize_to_account,
        )
        realized = self._position_money(
            raw,
            field="realized_pnl",
            symbol=symbol,
            currency=pnl_currency,
            observed_at=observed_at,
            required=require_pnl,
            normalize_to_account=normalize_to_account,
        )
        margin = self._position_money(
            raw,
            field="margin_used",
            symbol=symbol,
            currency=pnl_currency,
            observed_at=observed_at,
            required=False,
            default=Decimal("0"),
            normalize_to_account=normalize_to_account,
        )
        gross_notional, contract_multiplier, notional_currency = self._position_notional(
            raw,
            symbol=symbol,
            quantity=quantity,
            quantity_unit=quantity_unit,
            current_price=raw.get("current_price") or raw.get("mark_price"),
            price_currency=price_currency,
            observed_at=observed_at,
            normalize_to_account=normalize_to_account,
        )
        liquidation_price = self._optional_price(
            raw.get("liquidation_price"),
            currency=price_currency,
            observed_at=observed_at,
            normalize_to_account=normalize_to_account,
        )
        return _PositionValuation(
            position=Position(
                symbol=symbol,
                side=side,
                quantity=float(abs(quantity)),
                entry_price=float(entry_price),
                current_price=float(current_price),
                unrealized_pnl=None if unrealized is None else float(unrealized),
                realized_pnl=None if realized is None else float(realized),
                margin_used=None if not margin else float(margin),
                liquidation_price=(None if liquidation_price is None else float(liquidation_price)),
                quantity_unit=quantity_unit,
                contract_multiplier=(
                    None if contract_multiplier is None else float(contract_multiplier)
                ),
                gross_notional=float(gross_notional),
                notional_currency=notional_currency,
            ),
            gross_notional=gross_notional,
            unrealized_pnl=unrealized,
            realized_pnl=realized,
        )

    @staticmethod
    def _position_identity(
        raw: Mapping[str, Any],
        quantity: Decimal,
    ) -> tuple[str, str, PositionQuantityUnit, str]:
        symbol = str(raw.get("symbol") or "").strip()
        if not symbol:
            msg = "Broker position omitted its symbol"
            raise BrokerValuationError(msg)
        side_raw = str(raw.get("side") or "").strip().lower()
        if side_raw not in {"long", "short", "buy", "sell"}:
            msg = f"Broker position {symbol!r} returned unsupported side {side_raw!r}"
            raise BrokerValuationError(msg)
        if quantity < 0 and side_raw not in {"short", "sell"}:
            msg = f"Broker position {symbol!r} has inconsistent quantity and side"
            raise BrokerValuationError(msg)
        quantity_unit_raw = str(raw.get("quantity_unit") or "").strip().lower()
        if quantity_unit_raw not in {"asset", "contracts", "quote_notional"}:
            msg = f"Broker position {symbol!r} omitted a canonical quantity_unit"
            raise BrokerValuationError(msg)
        quantity_unit = cast(PositionQuantityUnit, quantity_unit_raw)
        notional_type = str(raw.get("notional_type") or "").strip().lower()
        if notional_type not in _SUPPORTED_POSITION_NOTIONAL_TYPES:
            msg = f"Broker position {symbol!r} has unsupported notional_type {notional_type!r}"
            raise BrokerValuationError(msg)
        if raw.get("is_quanto") not in (None, False):
            msg = f"Broker position {symbol!r} uses unsupported quanto valuation"
            raise BrokerValuationError(msg)
        side = "SHORT" if side_raw in {"short", "sell"} else "LONG"
        return symbol, side, quantity_unit, notional_type

    def _position_prices(
        self,
        raw: Mapping[str, Any],
        *,
        symbol: str,
        observed_at: datetime,
        normalize_to_account: bool,
    ) -> tuple[Decimal, Decimal, str]:
        current_original = _finite_decimal(raw.get("current_price") or raw.get("mark_price") or 0)
        if current_original <= 0:
            msg = f"Broker position {symbol!r} omitted a positive current price"
            raise BrokerValuationError(msg)
        entry_original = _finite_decimal(
            raw.get("entry_price") or raw.get("average_price") or raw.get("avg_price") or 0
        )
        if entry_original <= 0:
            msg = f"Broker position {symbol!r} omitted a positive entry price"
            raise BrokerValuationError(msg)
        price_currency = _position_price_currency(raw, symbol)
        if price_currency is None:
            msg = f"Broker position {symbol!r} omitted its price currency"
            raise BrokerValuationError(msg)
        return (
            self._position_value(
                current_original,
                from_currency=price_currency,
                as_of=observed_at,
                normalize_to_account=normalize_to_account,
            ),
            self._position_value(
                entry_original,
                from_currency=price_currency,
                as_of=observed_at,
                normalize_to_account=normalize_to_account,
            ),
            price_currency,
        )

    def _position_money(
        self,
        raw: Mapping[str, Any],
        *,
        field: str,
        symbol: str,
        currency: str,
        observed_at: datetime,
        required: bool,
        default: Decimal | None = None,
        normalize_to_account: bool,
    ) -> Decimal | None:
        value = raw.get(field)
        if value in (None, ""):
            if required:
                msg = f"Broker derivative position {symbol!r} omitted {field}"
                raise BrokerValuationError(msg)
            return default
        return self._position_value(
            _finite_decimal(value),
            from_currency=currency,
            as_of=observed_at,
            normalize_to_account=normalize_to_account,
        )

    def _position_notional(
        self,
        raw: Mapping[str, Any],
        *,
        symbol: str,
        quantity: Decimal,
        quantity_unit: PositionQuantityUnit,
        current_price: Any,
        price_currency: str,
        observed_at: datetime,
        normalize_to_account: bool,
    ) -> tuple[Decimal, Decimal | None, str]:
        gross_raw = raw.get("gross_notional")
        if gross_raw in (None, ""):
            if quantity_unit != "asset":
                msg = f"Broker non-asset position {symbol!r} omitted gross_notional"
                raise BrokerValuationError(msg)
            gross_original = abs(quantity) * _finite_decimal(current_price)
        else:
            gross_original = _finite_decimal(gross_raw)
        if gross_original <= 0:
            msg = f"Broker position {symbol!r} returned non-positive gross_notional"
            raise BrokerValuationError(msg)
        notional_currency = normalize_currency(str(raw.get("notional_currency") or price_currency))
        gross_value = self._position_value(
            gross_original,
            from_currency=notional_currency,
            as_of=observed_at,
            normalize_to_account=normalize_to_account,
        )
        multiplier_raw = raw.get("contract_multiplier")
        if multiplier_raw in (None, ""):
            return (
                gross_value,
                None,
                self._account_currency if normalize_to_account else notional_currency,
            )
        multiplier = _finite_decimal(multiplier_raw)
        if multiplier <= 0:
            msg = f"Broker position {symbol!r} returned invalid contract_multiplier"
            raise BrokerValuationError(msg)
        return (
            gross_value,
            multiplier,
            self._account_currency if normalize_to_account else notional_currency,
        )

    def _optional_price(
        self,
        value: Any,
        *,
        currency: str,
        observed_at: datetime,
        normalize_to_account: bool,
    ) -> Decimal | None:
        if value in (None, ""):
            return None
        converted = self._position_value(
            _finite_decimal(value),
            from_currency=currency,
            as_of=observed_at,
            normalize_to_account=normalize_to_account,
        )
        if converted <= 0:
            msg = "Broker position returned invalid liquidation_price"
            raise BrokerValuationError(msg)
        return converted

    def _position_value(
        self,
        value: Decimal,
        *,
        from_currency: str,
        as_of: datetime,
        normalize_to_account: bool,
    ) -> Decimal:
        if not normalize_to_account:
            return value
        return self._convert_money(
            value,
            from_currency=from_currency,
            as_of=as_of,
        )

    def _account_values(
        self,
        snapshot: Mapping[str, Any],
        *,
        gross_exposure: Decimal,
        observed_at: datetime,
    ) -> tuple[Decimal, Decimal, Decimal]:
        equity_source = snapshot.get("equity_source")
        if equity_source == "broker":
            equity_amounts = snapshot.get("equity") or []
            available_amounts = snapshot.get("available_cash") or []
            if not equity_amounts or not available_amounts:
                msg = "Broker snapshot omitted authoritative account equity"
                raise BrokerValuationError(msg)
            return (
                self._sum_money(equity_amounts, observed_at=observed_at),
                self._sum_money(available_amounts, observed_at=observed_at),
                self._sum_money(snapshot.get("margin_used") or [], observed_at=observed_at),
            )
        if equity_source == "spot_holdings":
            equity_cash, available_cash = self._spot_cash_values(
                snapshot,
                observed_at=observed_at,
            )
            return equity_cash + gross_exposure, available_cash, Decimal("0")
        msg = (
            f"{self._broker_type.value} does not expose authoritative account equity; "
            "live risk evaluation is blocked"
        )
        raise BrokerValuationError(msg)

    @staticmethod
    def _account_balances(snapshot: Mapping[str, Any]) -> list[AccountBalance]:
        """Preserve authoritative per-currency balances without FX conversion."""
        balances: list[AccountBalance] = []
        for raw in snapshot.get("balances") or []:
            currency = normalize_currency(str(raw.get("currency") or ""))
            total = _finite_decimal(raw.get("total"))
            available = _finite_decimal(raw.get("available"))
            locked = _finite_decimal(raw.get("locked"))
            balances.append(
                AccountBalance(
                    currency=currency,
                    total=str(total),
                    available=str(available),
                    locked=str(locked),
                )
            )
        return balances

    def _spot_cash_values(
        self,
        snapshot: Mapping[str, Any],
        *,
        observed_at: datetime,
    ) -> tuple[Decimal, Decimal]:
        positions = snapshot.get("positions") or []
        if any(
            str(raw.get("notional_type") or "").lower() != "spot"
            or raw.get("quantity_unit") != "asset"
            for raw in positions
        ):
            msg = "Holdings-based equity reconstruction received a non-spot position"
            raise BrokerValuationError(msg)
        position_assets = {
            currency for raw in positions if (currency := _position_asset_currency(raw)) is not None
        }
        equity_cash = Decimal("0")
        available_cash = Decimal("0")
        for balance in snapshot.get("balances") or []:
            currency = normalize_currency(str(balance.get("currency") or ""))
            if currency in position_assets:
                continue
            equity_cash += self._convert_money(
                _finite_decimal(balance.get("total") or 0),
                from_currency=currency,
                as_of=observed_at,
            )
            available_cash += self._convert_money(
                _finite_decimal(balance.get("available") or 0),
                from_currency=currency,
                as_of=observed_at,
            )
        return equity_cash, available_cash

    @staticmethod
    def _complete_pnl(values: Iterable[Decimal | None]) -> float | None:
        observed = list(values)
        if any(value is None for value in observed):
            return None
        return float(sum((value for value in observed if value is not None), Decimal("0")))

    def _sum_money(
        self,
        amounts: Sequence[Mapping[str, Any]],
        *,
        observed_at: datetime,
    ) -> Decimal:
        """Convert and sum explicit broker monetary components."""
        total = Decimal("0")
        for amount in amounts:
            currency = normalize_currency(str(amount.get("currency") or ""))
            value = _finite_decimal(amount.get("value"))
            total += self._convert_money(
                value,
                from_currency=currency,
                as_of=observed_at,
            )
        return total

    def _convert_money(
        self,
        value: Decimal,
        *,
        from_currency: str,
        as_of: datetime,
    ) -> Decimal:
        source = normalize_currency(from_currency)
        if source == self._account_currency or value == 0:
            return value
        if self._fx_converter is None:
            msg = (
                f"Broker account valuation requires an observed {source}/"
                f"{self._account_currency} FX rate"
            )
            raise FXRateUnavailableError(msg)
        return self._fx_converter.convert(
            value,
            from_currency=source,
            to_currency=self._account_currency,
            as_of=as_of,
        )

    async def get_positions(self) -> list[Position]:
        adapter = await self._require_adapter()
        raw_positions = await adapter.get_positions()
        valuations = self._normalize_positions(
            raw_positions,
            observed_at=datetime.now(tz=UTC),
            normalize_to_account=False,
        )
        return [valuation.position for valuation in valuations]

    async def submit_order(self, intent: OrderIntent) -> BrokerOrderResult:
        if self._adapter is None:
            await self.connect()
        if self._adapter is None:
            return BrokerOrderResult.rejected("Broker adapter unavailable")

        broker_intent = to_broker_order_intent(intent)
        result = await self._adapter.place_order(broker_intent)
        return from_broker_order_result(result)

    async def submit_options_order(self, intent: OptionsIntent) -> BrokerOrderResult:
        if self._adapter is None:
            await self.connect()
        if self._adapter is None:
            return BrokerOrderResult.rejected("Broker adapter unavailable")

        legs = [to_broker_order_intent(intent)]
        if intent.legs:
            legs = [to_broker_option_leg_intent(intent, leg) for leg in intent.legs]
        result = await self._adapter.place_multi_leg_order(legs)
        return from_broker_order_result(result)

    async def cancel_order(self, order_id: str) -> bool:
        if self._adapter is None:
            await self.connect()
        if self._adapter is None:
            return False
        return bool(await self._adapter.cancel_order(order_id))

    async def get_order_status(self, order_id: str) -> BrokerOrderResult:
        if self._adapter is None:
            await self.connect()
        if self._adapter is None:
            return BrokerOrderResult.rejected("Broker adapter unavailable")
        result = await self._adapter.get_order_status(order_id)
        return from_broker_order_result(result)

    async def get_fills(self, order_id: str) -> list[BrokerFill]:
        """Retrieve and explicitly convert exact infrastructure fill records."""
        adapter = await self._require_adapter()
        rows = await adapter.get_fills(order_id)
        return [from_broker_fill(row) for row in rows]

    async def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if self._adapter is None:
            await self.connect()
        if self._adapter is None:
            return []
        orders = await self._adapter.get_open_orders(symbol)
        return [dict(order) for order in orders if isinstance(order, dict)]


def to_broker_order_intent(intent: OrderIntent) -> BrokerOrderIntent:
    order_type = intent.order_type.lower()
    metadata = dict(intent.metadata or {})
    payload: BrokerOrderIntent = {
        "symbol": str(metadata.get("broker_symbol") or intent.symbol),
        "side": intent.side.lower(),
        "quantity": str(intent.quantity),
        "order_type": order_type,
        "execution_method": _execution_method_for_broker(intent),
        "time_in_force": intent.time_in_force.lower(),
        "client_order_id": client_order_id_for(metadata),
    }
    if intent.limit_price is not None:
        payload["limit_price"] = str(intent.limit_price)
    if intent.stop_price is not None:
        payload["stop_price"] = str(intent.stop_price)
    if metadata.get("option_type") is not None:
        payload["option_type"] = str(metadata["option_type"])
    if metadata.get("strike") is not None:
        payload["strike"] = str(metadata["strike"])
    if metadata.get("expiry") is not None:
        payload["expiry"] = str(metadata["expiry"])
    if metadata.get("exchange") is not None:
        payload["exchange"] = str(metadata["exchange"])
    if metadata.get("broker_instrument_id") is not None:
        payload["broker_instrument_id"] = str(metadata["broker_instrument_id"])
    if metadata.get("broker_instrument_type") is not None:
        payload["broker_instrument_type"] = str(metadata["broker_instrument_type"])
    if metadata.get("lot_size") is not None:
        payload["lot_size"] = str(metadata["lot_size"])
    if metadata.get("to_open_close") is not None:
        to_open_close = str(metadata["to_open_close"])
        if to_open_close not in {"to_open", "to_close"}:
            msg = "to_open_close metadata must be 'to_open' or 'to_close'"
            raise ValueError(msg)
        payload["to_open_close"] = cast(
            Literal["to_open", "to_close"],
            to_open_close,
        )
    if order_type == "stop":
        payload["order_type"] = "stop_limit"
        payload.setdefault("limit_price", payload.get("stop_price", "0"))
    return payload


def to_broker_option_leg_intent(intent: OptionsIntent, leg: Any) -> BrokerOrderIntent:
    """Translate an option leg to a broker-facing order payload."""
    metadata = dict(intent.metadata or {})
    leg_order = OrderIntent(
        broker_code=intent.broker_code,
        symbol=intent.symbol,
        side=str(getattr(leg, "side", "BUY")),
        quantity=float(getattr(leg, "quantity", 0)),
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        limit_price=intent.limit_price,
        stop_price=intent.stop_price,
        metadata={
            **metadata,
            "option_type": getattr(leg, "option_type", None),
            "strike": getattr(leg, "strike", None),
            "expiry": getattr(leg, "expiry", None),
        },
    )
    return to_broker_order_intent(leg_order)


def from_broker_order_result(result: Mapping[str, Any]) -> BrokerOrderResult:
    status_raw = str(result.get("status", "pending")).lower()
    status_map = {
        "new": OrderStatus.SUBMITTED,
        "submitted": OrderStatus.SUBMITTED,
        "open": OrderStatus.WORKING,
        "working": OrderStatus.WORKING,
        "pending": OrderStatus.PENDING,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "partial_fill": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "done": OrderStatus.FILLED,
        "cancelled": OrderStatus.CANCELLED,
        "canceled": OrderStatus.CANCELLED,
        "rejected": OrderStatus.REJECTED,
        "expired": OrderStatus.EXPIRED,
    }
    try:
        status = status_map[status_raw]
    except KeyError as exc:
        msg = f"Unsupported broker order status {status_raw!r}"
        raise ValueError(msg) from exc
    filled_quantity = float(result.get("filled_quantity") or 0.0)
    filled_price = _optional_float(result.get("filled_price"))
    commission = _optional_decimal(result.get("commission"), default=Decimal("0"))
    timestamp_raw = result.get("timestamp")
    timestamp = datetime.now(tz=UTC)
    if timestamp_raw:
        try:
            timestamp = datetime.fromisoformat(str(timestamp_raw))
        except (TypeError, ValueError) as exc:
            msg = f"Invalid broker order timestamp {timestamp_raw!r}"
            raise ValueError(msg) from exc
        if timestamp.tzinfo is None:
            msg = f"Broker order timestamp must include a timezone: {timestamp_raw!r}"
            raise ValueError(msg)
        timestamp = timestamp.astimezone(UTC)
    return BrokerOrderResult(
        success=bool(result.get("success")),
        order_id=cast(str | None, result.get("broker_order_id")),
        status=status,
        filled_quantity=filled_quantity,
        filled_price=filled_price,
        commission=commission or Decimal("0"),
        commission_currency=cast(str | None, result.get("commission_currency")),
        timestamp=timestamp,
        error_message=cast(str | None, result.get("error_message")),
        error_code=cast(str | None, result.get("error_code")),
        raw_response=result.get("raw_response"),
    )


def from_broker_fill(result: BrokerFillWire | Mapping[str, Any]) -> BrokerFill:
    """Convert the decimal-preserving broker wire fill into the app DTO."""
    timestamp_raw = result.get("timestamp")
    if timestamp_raw in (None, ""):
        msg = "Broker fill omitted timestamp"
        raise ValueError(msg)
    try:
        timestamp = datetime.fromisoformat(str(timestamp_raw))
    except (TypeError, ValueError) as exc:
        msg = f"Invalid broker fill timestamp {timestamp_raw!r}"
        raise ValueError(msg) from exc
    if timestamp.tzinfo is None:
        msg = f"Broker fill timestamp must include a timezone: {timestamp_raw!r}"
        raise ValueError(msg)
    return BrokerFill(
        trade_id=str(result.get("trade_id") or ""),
        order_id=str(result.get("broker_order_id") or ""),
        symbol=str(result.get("symbol") or ""),
        side=str(result.get("side") or ""),
        quantity=_required_decimal(result.get("quantity"), field="quantity"),
        price=_required_decimal(result.get("price"), field="price"),
        commission=_required_decimal(
            result.get("fee_amount"),
            field="fee_amount",
            positive=False,
        ),
        commission_currency=str(result.get("fee_currency") or ""),
        timestamp=timestamp,
        venue=str(result.get("venue") or ""),
        raw_response=cast(dict[str, Any] | None, result.get("raw_response")),
    )


def _optional_float(value: Any, *, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    return float(value)


def _optional_decimal(
    value: Any,
    *,
    default: Decimal | None = None,
) -> Decimal | None:
    if value in (None, ""):
        return default
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        msg = f"Invalid broker commission: {value!r}"
        raise ValueError(msg)
    return parsed


def _required_decimal(
    value: Any,
    *,
    field: str,
    positive: bool = True,
) -> Decimal:
    if value in (None, ""):
        msg = f"Broker fill omitted {field}"
        raise ValueError(msg)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        msg = f"Broker fill has invalid {field}: {value!r}"
        raise ValueError(msg) from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        msg = f"Broker fill has invalid {field}: {value!r}"
        raise ValueError(msg)
    return parsed


def _execution_method_for_broker(intent: OrderIntent) -> str:
    mode = str((intent.metadata or {}).get("execution_mode", "spot")).lower()
    if mode == ExecutionMode.MARGIN.value:
        return str(BrokerExecutionMethod.MARGIN.value)
    if mode == ExecutionMode.PERPETUAL.value:
        return str(BrokerExecutionMethod.PERPETUAL.value)
    if mode == ExecutionMode.FUTURES.value:
        return str(BrokerExecutionMethod.FUTURES.value)
    if mode == ExecutionMode.OPTIONS_SINGLE.value:
        return str(BrokerExecutionMethod.OPTIONS_SINGLE.value)
    if mode in {
        ExecutionMode.BULL_CALL.value,
        ExecutionMode.BEAR_PUT.value,
        ExecutionMode.BULL_PUT.value,
        ExecutionMode.BEAR_CALL.value,
        ExecutionMode.IRON_CONDOR.value,
        ExecutionMode.STRADDLE.value,
        ExecutionMode.STRANGLE.value,
    }:
        return str(BrokerExecutionMethod.OPTIONS_STRATEGY.value)
    return str(BrokerExecutionMethod.SPOT.value)
