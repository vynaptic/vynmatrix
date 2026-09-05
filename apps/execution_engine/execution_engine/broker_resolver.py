"""Broker selection and connection caching for the execution engine.

Extracted from ``apps/execution_engine/execution_engine/engine.py`` as the
second step of the Phase-3 ExecutionEngine decomposition. This module owns the
"given a request, return a usable BrokerAdapter" responsibility:

* ``resolve_allowed_brokers`` reads the profile + strategy config to compute
  the list of brokers the user is permitted to route through.
* ``resolve_broker_type`` picks the concrete ``BrokerType`` for the chosen
  execution mode (spot vs options, crypto vs equity, with the spot/options
  broker overrides), and validates every broker/mode pair against the canonical
  capability matrix.
* ``get_broker`` returns a connected ``BrokerAdapter``, caching one instance
  per tenant, linked account, broker, environment, credential reference, and
  account currency. The cache is intentionally owned here.

``ExecutionEngine`` retains only the broker-connection delegate patched by its
tests; route selection calls this collaborator directly.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, Protocol

from lib_common.asset_classes import normalize_optional_asset_class
from lib_common.config_validation import normalize_broker_code
from lib_common.logging import get_logger
from lib_infrastructure.brokers.capabilities import get_broker_capabilities

from .broker_bridge import BrokerBridge
from .brokers.base import BrokerAdapter
from .brokers.paper import PaperBroker
from .config import BrokerType, ExecutionMode
from .metrics.fx_rates import FXRateProvider, normalize_currency
from .paper_ledger_recovery import PaperLedgerSeed, recover_paper_ledger

logger = get_logger(__name__)


class BrokerCapabilityError(Exception):
    """A (broker, execution-mode) combination cannot be routed in live.

    Raised instead of silently downgrading a live order to the paper broker
    (which would give the user a fake fill for a trade they believed was live).
    The caller turns this into a structured blocked ExecutionResult.
    """


class PaperAccountStateError(RuntimeError):
    """Persisted paper account state cannot be rehydrated safely."""


class PaperLedgerSeeder(Protocol):
    """Rebuild one owned paper account from canonical fill truth."""

    def __call__(
        self,
        *,
        user_id: str,
        broker_code: str,
        environment: str,
        account_id: int,
        account_currency: str,
        configured_equity: float | None,
        configured_cash: float | None,
    ) -> PaperLedgerSeed: ...


class BrokerResolver:
    """Resolves the broker code/type/connection for an execution request."""

    def __init__(
        self,
        ledger_seeder: PaperLedgerSeeder | None = None,
        fx_rate_provider: FXRateProvider | None = None,
        *,
        use_local_paper_broker: bool = False,
        paper_slippage_pct: float = 0.001,
        paper_commission_pct: float = 0.001,
        paper_fill_committer: Callable[..., None] | None = None,
    ) -> None:
        self._broker_cache: dict[str, BrokerAdapter] = {}
        self._paper_rehydration_failures: set[str] = set()
        self._account_authority_versions: dict[tuple[str, int], str] = {}
        # The engine injects one canonical-ledger loader so cash and positions
        # cannot be seeded from projections observed at different crash points.
        # Isolated tests may omit it only with explicit paper capital.
        self._ledger_seeder = ledger_seeder
        self._fx_rate_provider = fx_rate_provider
        self._use_local_paper_broker = use_local_paper_broker
        self._paper_slippage_pct = paper_slippage_pct
        self._paper_commission_pct = paper_commission_pct
        self._paper_fill_committer = paper_fill_committer

    # ------------------------------------------------------------------
    # Allowed brokers
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_allowed_brokers(
        profile: dict[str, Any] | None,
        user_strategy_config: dict[str, Any],
    ) -> list[str]:
        """Return the list of broker codes the user may route through."""
        profile = profile or {}
        allowed = user_strategy_config.get("allowed_brokers")
        if not allowed:
            allowed = profile.get("linked_brokers") or profile.get("allowed_brokers")
        if not allowed:
            return []
        if isinstance(allowed, str):
            return [normalize_broker_code(allowed)]
        return [normalize_broker_code(item) for item in allowed if item]

    # ------------------------------------------------------------------
    # Broker-type selection
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_broker_type(
        exec_mode: ExecutionMode,
        profile: dict[str, Any],
        user_strategy_config: dict[str, Any],
        asset_class: str | None,
        environment: str = "paper",
    ) -> BrokerType:
        """Select the concrete broker for ``exec_mode`` + asset class.

        An unknown broker always raises BrokerCapabilityError (→ blocked result)
        rather than silently changing the account route. In ``live``, every
        unsupported execution mode is blocked; paper/backtest may use the
        explicit local paper adapter only for a known unsupported capability.
        """
        is_live = environment == "live"
        config = user_strategy_config or {}
        profile = profile or {}
        asset_class_val = normalize_optional_asset_class(
            asset_class or profile.get("asset_class"),
            field_name="broker-routing asset class",
        )

        broker_override = config.get("broker") or profile.get("broker")
        if ExecutionMode.is_options(exec_mode):
            if asset_class_val == "crypto":
                broker_override = (
                    config.get("options_broker_crypto")
                    or config.get("options_broker")
                    or profile.get("options_broker_crypto")
                    or profile.get("options_broker")
                    or broker_override
                )
            else:
                broker_override = (
                    config.get("options_broker_equity")
                    or config.get("options_broker")
                    or profile.get("options_broker_equity")
                    or profile.get("options_broker")
                    or broker_override
                )
        else:
            broker_override = (
                config.get("spot_broker") or profile.get("spot_broker") or broker_override
            )

        broker_value = normalize_broker_code(broker_override or "paper")
        try:
            resolved = BrokerType(broker_value)
        except ValueError:
            msg = f"Unknown broker '{broker_value}' for {environment} execution"
            raise BrokerCapabilityError(msg) from None

        # NOTIFY_ONLY records a qualified decision and never submits an order.
        # Preserve the configured route for attribution, but do not require a
        # venue-native order mode or fill-retrieval certification for a path
        # that cannot touch the broker.
        if exec_mode == ExecutionMode.NOTIFY_ONLY:
            return resolved

        # Validate every mode through the single broker-capability matrix rather
        # than maintaining route-specific broker allowlists.
        caps = get_broker_capabilities(resolved.value)
        broker_supports_mode = caps is not None and caps.supports_mode(exec_mode.value)
        if resolved != BrokerType.PAPER and not broker_supports_mode:
            if is_live:
                msg = (
                    f"Execution mode '{exec_mode.value}' resolved to broker "
                    f"'{resolved.value}' which does not support that mode. "
                    "Configure a compatible broker explicitly."
                )
                raise BrokerCapabilityError(msg)
            logger.warning(
                "Execution mode '%s' resolved to broker '%s' which does not support "
                "that mode; falling back to paper. Configure a compatible broker "
                "explicitly to fix.",
                exec_mode.value,
                resolved.value,
            )
            return BrokerType.PAPER

        if (
            is_live
            and resolved != BrokerType.PAPER
            and caps is not None
            and not caps.supports_exact_fill_retrieval
        ):
            msg = (
                f"Broker '{resolved.value}' is not live-certifiable: its official "
                "API integration cannot persist exact fill ID, timestamp, fee amount, "
                "and fee currency. Use paper until that complete contract is available."
            )
            raise BrokerCapabilityError(msg)
        if (
            is_live
            and resolved != BrokerType.PAPER
            and caps is not None
            and not caps.live_certification_implemented
        ):
            msg = (
                f"Broker '{resolved.value}' is not live-certifiable: exact-fill "
                "support alone is insufficient and its broker-specific authenticated "
                "certification workflow has not been implemented and verified. "
                "Use paper until that workflow is complete."
            )
            raise BrokerCapabilityError(msg)

        return resolved

    # ------------------------------------------------------------------
    # Broker connection / caching
    # ------------------------------------------------------------------

    async def get_broker(
        self,
        broker_type: BrokerType,
        environment: str,
        credential_ref: str,
        credentials: dict[str, str] | None = None,
        *,
        user_id: str,
        broker_account_id: int,
        account_currency: str,
        settlement_currency: str,
        paper_initial_equity: float | None = None,
        paper_initial_cash: float | None = None,
    ) -> BrokerAdapter | None:
        """Return a connected broker adapter scoped to one linked account.

        Tenant, account, credential, and base currency are all part of the cache
        key. Remote broker bridges additionally include the routed settlement
        currency because balance-derived position symbols must use the exact
        product quote. Local paper books intentionally span settlement
        currencies within one linked account.
        """
        normalized_user_id = str(user_id).strip()
        if not normalized_user_id:
            msg = "Broker resolution requires an explicit user_id"
            raise ValueError(msg)
        if (
            isinstance(broker_account_id, bool)
            or not isinstance(broker_account_id, int)
            or broker_account_id <= 0
        ):
            msg = "Broker resolution requires a positive broker_account_id"
            raise ValueError(msg)
        normalized_environment = str(environment).strip().lower()
        if normalized_environment not in {"paper", "live"}:
            msg = f"Broker resolution received unsupported environment {environment!r}"
            raise ValueError(msg)
        normalized_credential_ref = str(credential_ref).strip()
        if not normalized_credential_ref:
            msg = "Broker resolution requires an explicit credential_ref"
            raise ValueError(msg)
        normalized_currency = normalize_currency(account_currency)
        normalized_settlement = normalize_currency(settlement_currency)
        use_local_paper_broker = normalized_environment == "paper" and self._use_local_paper_broker
        is_local_paper = broker_type == BrokerType.PAPER or use_local_paper_broker
        settlement_cache_key = "" if is_local_paper else f":{normalized_settlement}"
        cache_key = (
            f"{normalized_user_id}:{broker_account_id}:{broker_type.value}:"
            f"{normalized_environment}:{normalized_credential_ref}:{normalized_currency}"
            f"{settlement_cache_key}"
        )

        if cache_key in self._broker_cache:
            return self._broker_cache[cache_key]

        if is_local_paper:
            try:
                paper = await self._build_paper_broker(
                    user_id=normalized_user_id,
                    broker_type=broker_type,
                    environment=normalized_environment,
                    account_id=broker_account_id,
                    account_currency=normalized_currency,
                    configured_equity=paper_initial_equity,
                    configured_cash=paper_initial_cash,
                )
            except Exception as exc:
                self._paper_rehydration_failures.add(cache_key)
                logger.exception(
                    "Paper account rehydration failed; broker resolution blocked",
                    user_id=normalized_user_id,
                    broker=broker_type.value,
                    broker_account_id=broker_account_id,
                    environment=normalized_environment,
                )
                msg = (
                    f"Paper account {broker_account_id} for user "
                    f"{normalized_user_id} could not be rehydrated"
                )
                raise PaperAccountStateError(msg) from exc
            self._paper_rehydration_failures.discard(cache_key)
            self._broker_cache[cache_key] = paper
            return paper

        broker = BrokerBridge(
            broker_type=broker_type,
            environment=normalized_environment,
            credential_ref=normalized_credential_ref,
            credentials=credentials,
            account_currency=normalized_currency,
            settlement_currency=normalized_settlement,
            fx_rate_provider=self._fx_rate_provider,
        )
        self._broker_cache[cache_key] = broker
        return broker

    async def _build_paper_broker(
        self,
        *,
        user_id: str,
        broker_type: BrokerType,
        environment: str,
        account_id: int,
        account_currency: str,
        configured_equity: float | None,
        configured_cash: float | None,
    ) -> PaperBroker:
        """Construct and verify one resolver-owned local paper account.

        Cash, realized P&L, and positions come from one canonical fill replay
        before the adapter enters the cache. ``execution_metrics`` and
        ``positions`` are downstream projections and are never restart inputs.
        """
        ledger_loader = self._ledger_seeder
        if ledger_loader is None:
            state = recover_paper_ledger(
                None,
                None,
                user_id=user_id,
                broker_code=broker_type.value,
                environment=environment,
                account_id=account_id,
                account_currency=account_currency,
                configured_equity=configured_equity,
                configured_cash=configured_cash,
            )
        else:
            state = ledger_loader(
                user_id=user_id,
                broker_code=broker_type.value,
                environment=environment,
                account_id=account_id,
                account_currency=account_currency,
                configured_equity=configured_equity,
                configured_cash=configured_cash,
            )
        if state.currency != account_currency:
            msg = (
                f"Paper account state currency {state.currency} conflicts with "
                f"routed currency {account_currency}"
            )
            raise ValueError(msg)

        paper = PaperBroker(
            account_id=str(account_id),
            starting_balance=state.initial_equity,
            available_cash=state.cash_balance,
            currency=state.currency,
            realized_pnl=state.realized_pnl,
            slippage_pct=self._paper_slippage_pct,
            commission_pct=self._paper_commission_pct,
            durable_fill_committer=self._paper_fill_committer,
        )
        positions = state.broker_positions()
        if positions:
            paper.load_positions(positions, adjust_cash=False)

        account_info = await paper.get_account_info()
        if not math.isclose(
            account_info.equity,
            float(state.current_equity),
            rel_tol=1e-9,
            abs_tol=0.01,
        ):
            msg = (
                f"Rehydrated paper equity {account_info.equity:.8f} conflicts "
                f"with canonical fill equity {state.current_equity}"
            )
            raise ValueError(msg)

        logger.info(
            "Rehydrated resolver-owned paper broker",
            user_id=user_id,
            broker=broker_type.value,
            broker_account_id=account_id,
            environment=environment,
            position_count=len(positions),
            canonical_execution_count=state.execution_count,
            last_canonical_execution_id=state.last_execution_id,
            state_source=(
                "canonical_execution_ledger"
                if state.execution_count
                else "linked_account_initial_capital"
            ),
        )
        return paper

    def is_paper_rehydration_healthy(self) -> bool:
        """Return false after any paper route failed to load its persisted book."""
        return not self._paper_rehydration_failures

    async def ensure_current_authority(
        self,
        *,
        user_id: str,
        broker_account_id: int,
        credential_version: str,
    ) -> bool:
        """Evict an adapter when the durable credential generation changes."""
        key = (str(user_id), int(broker_account_id))
        previous = self._account_authority_versions.get(key)
        changed = previous is not None and previous != credential_version
        if changed:
            await self.evict_account(
                user_id=user_id,
                broker_account_id=broker_account_id,
                clear_authority_version=False,
            )
        self._account_authority_versions[key] = credential_version
        return changed

    async def evict_account(
        self,
        *,
        user_id: str,
        broker_account_id: int,
        clear_authority_version: bool = True,
    ) -> int:
        """Disconnect and remove every cached route for one owned account."""
        normalized_user_id = str(user_id).strip()
        account_id = int(broker_account_id)
        prefix = f"{normalized_user_id}:{account_id}:"
        cache_keys = [key for key in self._broker_cache if key.startswith(prefix)]
        brokers = [self._broker_cache.pop(key) for key in cache_keys]
        self._paper_rehydration_failures.difference_update(cache_keys)
        if clear_authority_version:
            self._account_authority_versions.pop((normalized_user_id, account_id), None)
        for broker in brokers:
            try:
                await broker.disconnect()
            except (OSError, RuntimeError):
                logger.exception(
                    "Failed to disconnect evicted broker adapter",
                    user_id=normalized_user_id,
                    broker_account_id=account_id,
                )
        if cache_keys:
            logger.info(
                "Evicted cached broker adapters after authority change",
                user_id=normalized_user_id,
                broker_account_id=account_id,
                adapter_count=len(cache_keys),
            )
        return len(cache_keys)

    # ------------------------------------------------------------------
    # Cache access (used by reconciliation context restoration)
    # ------------------------------------------------------------------

    @property
    def cache(self) -> dict[str, BrokerAdapter]:
        """Expose the internal cache for callers that need to inspect or wipe it."""
        return self._broker_cache
