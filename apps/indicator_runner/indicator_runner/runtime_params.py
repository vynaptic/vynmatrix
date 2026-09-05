"""Single source of truth for strategy runtime parameters and identity.

The worker builder (``signal_worker.build_worker``), the durable runtime
identity, and the supervisor's operational-status reader must agree
byte-for-byte on the universe/source/timeframe resolution. Before this module
each surface carried its own copy of the defaults, so one drifted literal
could silently trade — or report health for — the wrong universe.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lib_strategy.signals.pure_strategy import ModelStateContractError, PureSignalStrategy

from .panel_binding import PanelRuntimeBinding, panel_outbox_ordering_key

DEFAULT_UNIVERSE = "BTCUSDC,ETHUSDC,SOLUSDC"
DEFAULT_SOURCE = "coinbase_live"
DEFAULT_TIMEFRAME = "1m"
DEFAULT_CONSOLIDATION_MINUTES = 15
DEFAULT_BOOTSTRAP_BARS = 500
DEFAULT_ASSET_CLASS = "crypto"
DEFAULT_MAX_PANEL_AGE_DAYS = 40
_SECONDS_PER_DAY = 24 * 60 * 60


@dataclass(frozen=True)
class StrategyRuntimeParams:
    """Resolved market-data/runtime parameters for one strategy config."""

    universe: str
    source: str
    timeframe: str
    consolidation_minutes: int
    bootstrap_bars: int
    asset_class: str
    max_panel_age_days: int

    @property
    def max_panel_age_seconds(self) -> int:
        """Return the configured synchronized-panel freshness bound."""

        return self.max_panel_age_days * _SECONDS_PER_DAY

    @classmethod
    def from_config(
        cls,
        parameters: Mapping[str, Any],
        market_data: Mapping[str, Any],
    ) -> StrategyRuntimeParams:
        """Resolve the runtime parameters exactly once per strategy config."""
        # The schema allows the universe as either a comma string (crypto
        # canary) or a JSON array (equity basket); normalize both to the
        # canonical comma string parse_strategy_symbols consumes.
        raw_universe = parameters.get("universe", DEFAULT_UNIVERSE)
        if isinstance(raw_universe, (list, tuple)):
            universe = ",".join(str(symbol).strip() for symbol in raw_universe)
        else:
            universe = str(raw_universe)
        max_panel_age_days = parameters.get(
            "max_panel_age_days",
            DEFAULT_MAX_PANEL_AGE_DAYS,
        )
        if (
            isinstance(max_panel_age_days, bool)
            or not isinstance(max_panel_age_days, int)
            or max_panel_age_days <= 0
        ):
            msg = "max_panel_age_days must be a positive integer"
            raise ValueError(msg)
        return cls(
            universe=universe,
            source=str(market_data.get("source", DEFAULT_SOURCE)),
            timeframe=str(market_data.get("timeframe", DEFAULT_TIMEFRAME)),
            consolidation_minutes=int(
                market_data.get("consolidation_minutes", DEFAULT_CONSOLIDATION_MINUTES)
            ),
            bootstrap_bars=int(market_data.get("bootstrap_bars", DEFAULT_BOOTSTRAP_BARS)),
            asset_class=str(parameters.get("asset_class", DEFAULT_ASSET_CLASS)),
            max_panel_age_days=max_panel_age_days,
        )


@dataclass(frozen=True)
class StrategyRuntimeIdentity:
    """Immutable active runtime identity checked before state restoration."""

    worker_id: str
    strategy_id: str
    strategy_version: str
    symbols: tuple[str, ...]
    source: str
    timeframe: str
    consolidation_minutes: int
    config_fingerprint: str
    panel_binding: PanelRuntimeBinding | None = None

    @property
    def ordering_key(self) -> str:
        """Return the owner-isolated transactional-outbox partition."""

        if self.panel_binding is None:
            return self.worker_id
        return panel_outbox_ordering_key(self.worker_id, self.panel_binding)

    def require_panel_binding(self) -> PanelRuntimeBinding:
        """Return the configured panel fence or fail the panel-only call site."""

        if self.panel_binding is None:
            message = "Runtime identity has no synchronized-panel binding"
            raise ModelStateContractError(message)
        return self.panel_binding

    @classmethod
    def from_strategy(
        cls,
        *,
        worker_id: str,
        strategy: PureSignalStrategy,
        symbols: Sequence[str],
        source: str,
        timeframe: str,
        consolidation_minutes: int,
        panel_capable: bool = False,
        panel_binding: PanelRuntimeBinding | None = None,
    ) -> StrategyRuntimeIdentity:
        """Build the durable identity from one validated strategy runtime."""

        strategy_version = str(strategy.config.get("strategy_version") or "").strip()
        if not strategy_version:
            msg = f"Strategy {strategy.strategy_id!r} has no runtime strategy_version"
            raise ModelStateContractError(msg)
        normalized_symbols = tuple(
            sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        )
        if panel_capable and normalized_symbols:
            msg = f"Panel strategy {strategy.strategy_id!r} cannot use static runtime symbols"
            raise ModelStateContractError(msg)
        if not normalized_symbols and not panel_capable:
            msg = f"Strategy {strategy.strategy_id!r} has no runtime symbols"
            raise ModelStateContractError(msg)
        if panel_capable and panel_binding is None:
            msg = f"Panel strategy {strategy.strategy_id!r} has no runtime owner/scope binding"
            raise ModelStateContractError(msg)
        if not panel_capable and panel_binding is not None:
            msg = f"Per-symbol strategy {strategy.strategy_id!r} cannot use a panel binding"
            raise ModelStateContractError(msg)
        try:
            config_json = json.dumps(
                strategy.config,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            msg = f"Strategy {strategy.strategy_id!r} configuration is not JSON-safe"
            raise ModelStateContractError(msg) from exc
        return cls(
            worker_id=worker_id,
            strategy_id=strategy.strategy_id,
            strategy_version=strategy_version,
            symbols=normalized_symbols,
            source=source,
            timeframe=timeframe,
            consolidation_minutes=consolidation_minutes,
            config_fingerprint=hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
            panel_binding=panel_binding,
        )


__all__ = [
    "DEFAULT_ASSET_CLASS",
    "DEFAULT_BOOTSTRAP_BARS",
    "DEFAULT_CONSOLIDATION_MINUTES",
    "DEFAULT_MAX_PANEL_AGE_DAYS",
    "DEFAULT_SOURCE",
    "DEFAULT_TIMEFRAME",
    "DEFAULT_UNIVERSE",
    "StrategyRuntimeIdentity",
    "StrategyRuntimeParams",
]
