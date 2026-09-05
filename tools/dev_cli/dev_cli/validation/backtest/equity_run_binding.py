"""Bind one run to its snapshot: provider, strategy, scope, and universe."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dev_cli.validation.backtest.equity_portfolio import PinnedUniverseSelectionPolicy
from dev_cli.validation.backtest.equity_run_inputs import (
    EquityRunError,
    _assert_provider_matches_manifest,
    _assert_strategy_matches_manifest,
)
from dev_cli.validation.backtest.equity_snapshot import StrategyIdentity, ValidationProvider

if TYPE_CHECKING:
    from dev_cli.validation.backtest.equity_portfolio_types import BacktestResult


def _assert_run_matches_manifest(
    manifest: Mapping[str, object],
    *,
    provider: ValidationProvider,
    strategy_dir: Path,
    allow_comparison_portfolio: bool,
) -> bool:
    """Bind provider and strategy to the snapshot; report snapshot ownership."""
    _assert_provider_matches_manifest(provider, manifest)
    return _assert_strategy_matches_manifest(
        strategy_dir,
        manifest,
        allow_comparison_portfolio=allow_comparison_portfolio,
    )


def _stamp_comparison_portfolio_lineage(
    result: BacktestResult,
    manifest: Mapping[str, object],
    *,
    identity: StrategyIdentity,
) -> None:
    """Record that this result is a strategy evaluated on another's snapshot."""
    snapshot_owner = manifest.get("strategy")
    result.policies["comparison_portfolio_strategy_id"] = identity.strategy_id
    result.policies["comparison_portfolio_snapshot_owner_strategy_id"] = str(
        snapshot_owner.get("strategy_id") if isinstance(snapshot_owner, Mapping) else "unknown"
    )
    result.policies["result_claim_scope"] = "labelled_comparison_portfolio"


def _pinned_universe_policy(
    strategy_config: Mapping[str, Any],
) -> PinnedUniverseSelectionPolicy:
    """Build the present-day pinned universe declared by a strategy config."""
    raw = strategy_config.get("universe")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        message = "pinned-universe control requires a non-empty configured universe"
        raise EquityRunError(message)
    return PinnedUniverseSelectionPolicy(symbols=tuple(str(item) for item in raw))
