"""Immutable snapshot acquisition and portfolio validation for US equities.

The command surface:

- ``fetch``: acquire raw daily bars and corporate actions through an explicit
  historical-validation provider, preserving every membership-interval
  disposition in an append-only resume ledger and immutable manifest.
- ``compile-official-sessions``: fetch and pin the official ICE/NYSE source
  documents and compile an exact authoritative XNYS session artifact.
- ``materialize-factors``: acquire public SEC evidence and derive immutable,
  snapshot-bound strategy panels.
- ``freeze-trials``: preregister the immutable validation protocol.
- ``run``: verify the content-addressed manifest and every object, build an
  ``EquityDataset`` with the shared adjustment owner, and execute the selected
  production strategy core.

EODHD is accepted only on this offline validation surface.  It is not selected
or imported by a production strategy or forward market-data runtime.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any, cast

from dev_cli.validation.backtest.equity_diagnostics import (
    EquityDiagnosticsResult,
    complete_diagnostics_payload,
    unavailable_diagnostics_payload,
)
from dev_cli.validation.backtest.equity_membership_corrections import (
    MembershipCorrectionAcquisitionConfig,
    MembershipCorrectionError,
    acquire_membership_correction_evidence,
)
from dev_cli.validation.backtest.equity_portfolio import (
    BacktestResult,
    EquityDataset,
    EquityPortfolioBacktest,
    ExplicitRawCorporateActionPolicy,
    PanelExecutionUniversePolicy,
    PortfolioConfig,
    StaticExecutionUniverseCadencePolicy,
    SynchronizedStrategyPanelContextPolicy,
    institutional_equity_execution_cost_policy,
    worst_trades,
)
from dev_cli.validation.backtest.equity_run_binding import (
    _assert_run_matches_manifest,
    _pinned_universe_policy,
    _stamp_comparison_portfolio_lineage,
)
from dev_cli.validation.backtest.equity_run_cli import (
    build_argument_parser,
)
from dev_cli.validation.backtest.equity_run_inputs import (
    _WORST_TRADES_COUNT,
    EquityRunError,
    _git_rev,
    _strategy_identity,
    _write_csv,
    fetch_snapshot,
    load_snapshot,
)
from dev_cli.validation.backtest.equity_snapshot import (
    BenchmarkSpec,
    BenchmarkTotalReturnKind,
    ValidationProvider,
)
from dev_cli.validation.backtest.equity_snapshot_admission import (
    SnapshotFactorClaimScope,
)
from dev_cli.validation.backtest.equity_strategy_panels import (
    STRATEGY_PANEL_MANIFEST_SCHEMA,
    EquityStrategyPanelError,
    compose_strategy_panel_dataset,
)
from dev_cli.validation.backtest.nyse_official_sessions import (
    compile_nyse_official_session_artifact,
)
from dev_cli.validation.evidence import evidence_sha256, store_content_object
from lib_common.runner_utils import build_strategy_core_parameters
from lib_strategy.panels import SynchronizedPanelStrategy
from lib_strategy.signals.pure_strategy import PureSignalStrategy


def _finite_metric_payload(
    values: Mapping[str, float],
) -> tuple[dict[str, float | None], dict[str, str]]:
    normalized: dict[str, float | None] = {}
    dispositions: dict[str, str] = {}
    for name, value in sorted(values.items()):
        if math.isfinite(value):
            normalized[name] = value
            continue
        normalized[name] = None
        dispositions[name] = "positive_infinity" if value > 0.0 else "negative_infinity_or_nan"
    return normalized, dispositions


def write_report(
    out_dir: Path,
    result: BacktestResult,
    config: PortfolioConfig,
    manifest: dict[str, object],
    *,
    diagnostics: EquityDiagnosticsResult | None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    net_metrics, net_metric_dispositions = _finite_metric_payload(result.metrics)
    gross_metrics, gross_metric_dispositions = _finite_metric_payload(result.gross_metrics)
    _write_csv(
        out_dir / "equity_curve.csv",
        ["session", "equity"],
        [[stamp.date().isoformat(), value] for stamp, value in result.equity_curve],
    )
    _write_csv(
        out_dir / "daily_exposure.csv",
        [
            "session",
            "gross_invested_notional",
            "net_invested_notional",
            "gross_exposure_ratio",
            "net_exposure_ratio",
        ],
        [
            [
                observation.timestamp.date().isoformat(),
                observation.gross_invested_notional,
                observation.net_invested_notional,
                observation.gross_exposure_ratio,
                observation.net_exposure_ratio,
            ]
            for observation in result.daily_exposures
        ],
    )
    _write_csv(
        out_dir / "trades.csv",
        [
            "symbol",
            "entry_session",
            "entry_fill",
            "shares",
            "exit_session",
            "exit_fill",
            "exit_reason",
            "pnl",
            "return_pct",
            "holding_days",
        ],
        [
            [
                t.symbol,
                t.entry_session.isoformat(),
                t.entry_fill,
                t.shares,
                t.exit_session.isoformat() if t.exit_session else "",
                t.exit_fill if t.exit_fill is not None else "",
                t.exit_reason or "",
                t.pnl,
                t.return_pct,
                t.holding_sessions,
            ]
            for t in result.trades
        ],
    )
    for period, table in result.period_returns.items():
        _write_csv(
            out_dir / f"returns_{period}.csv",
            ["period", "return_pct"],
            [[k, v] for k, v in sorted(table.items())],
        )
    (out_dir / "rebalances.json").write_text(
        json.dumps(result.rebalance_log, allow_nan=False, indent=2),
        encoding="utf-8",
    )
    summary = {
        "metrics": net_metrics,
        "gross_metrics": gross_metrics,
        "metric_dispositions": {
            "gross": gross_metric_dispositions,
            "net": net_metric_dispositions,
        },
        "benchmark": result.benchmark,
        "policies": result.policies,
        "terminal_liquidation_sensitivity": result.terminal_liquidation_sensitivity,
        "terminal_positions": [position.to_dict() for position in result.terminal_positions],
        "diagnostics": (
            complete_diagnostics_payload(diagnostics)
            if diagnostics is not None
            else unavailable_diagnostics_payload(
                "immutable attribution, trial aggregation, and capacity evidence was not supplied"
            )
        ),
        "worst_trades": [
            {
                "symbol": t.symbol,
                "entry": t.entry_session.isoformat(),
                "exit": t.exit_session.isoformat() if t.exit_session else None,
                "pnl": t.pnl,
                "return_pct": t.return_pct,
                "reason": t.exit_reason,
            }
            for t in worst_trades(result, _WORST_TRADES_COUNT)
        ],
        "config": {
            "eval_start": config.eval_start.isoformat(),
            "eval_end": config.eval_end.isoformat(),
            "initial_capital": config.initial_capital,
            "universe_size": config.universe_size,
            "adv_window": config.adv_window,
            "execution_assumptions": result.execution_assumptions,
        },
        "snapshot": {
            "acquisition_id": manifest.get("acquisition_id"),
            "provider": manifest.get("provider"),
            "period": manifest.get("period"),
            "benchmarks": manifest.get("benchmarks"),
            "configuration_identity": manifest.get("configuration_identity"),
            "code_identity": manifest.get("code_identity"),
        },
        "git_rev_at_run": _git_rev(),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, allow_nan=False, indent=2),
        encoding="utf-8",
    )


def _publish_backtest_result(
    out_dir: Path,
    result: BacktestResult,
    config: PortfolioConfig,
    manifest: dict[str, object],
    *,
    diagnostics: EquityDiagnosticsResult | None,
    result_persistence_gate: Callable[[BacktestResult], None] | None,
) -> None:
    """Publish strict file evidence before committing a successful trial result."""

    write_report(
        out_dir,
        result,
        config,
        manifest,
        diagnostics=diagnostics,
    )
    if result_persistence_gate is not None:
        result_persistence_gate(result)


def _publish_backtest_with_baseline_comparison(
    out_dir: Path,
    result: BacktestResult,
    dataset: EquityDataset,
    config: PortfolioConfig,
    manifest: dict[str, object],
) -> None:
    """Publish one verified portfolio result and its derived evidence."""
    del dataset
    _publish_backtest_result(
        out_dir,
        result,
        config,
        manifest,
        diagnostics=None,
        result_persistence_gate=None,
    )


def _run_non_panel_backtest(
    dataset: EquityDataset,
    strategy_factory: Callable[[], PureSignalStrategy],
    config: PortfolioConfig,
    *,
    strategy_payload: Mapping[str, Any],
    strategy_config: Mapping[str, Any],
    pinned_universe: bool = False,
) -> BacktestResult:
    """Run a non-panel strategy; equity baskets get trial-parity accounting.

    A non-panel equity strategy (the labelled ADV-leaders comparison
    portfolio) must price and account exactly like the registered panel
    trials: the same institutional expected-cost contract and raw
    corporate-action accounting, with the PIT ADV universe sized by the
    strategy's own basket_size. Anything else would make the head-to-head
    comparison asymmetric.
    """
    parameters_payload = strategy_payload.get("parameters")
    asset_class = (
        str(parameters_payload.get("asset_class", ""))
        if isinstance(parameters_payload, Mapping)
        else ""
    )
    if asset_class != "equity":
        return EquityPortfolioBacktest(dataset, strategy_factory, config).run()
    basket_size = int(str(strategy_config.get("basket_size", config.universe_size)))
    parity_config = PortfolioConfig(
        eval_start=config.eval_start,
        eval_end=config.eval_end,
        universe_size=basket_size,
    )
    return EquityPortfolioBacktest(
        dataset,
        strategy_factory,
        parity_config,
        execution_cost_policy=institutional_equity_execution_cost_policy(
            scenario_name="expected",
        ),
        corporate_action_policy=ExplicitRawCorporateActionPolicy(),
        universe_policy=_pinned_universe_policy(strategy_config) if pinned_universe else None,
    ).run()


def _run_panel_backtest(
    dataset: EquityDataset,
    strategy_factory: Callable[[], PureSignalStrategy],
    config: PortfolioConfig,
) -> BacktestResult:
    """Run a synchronized-panel strategy through the shared equity simulator."""

    if not dataset.panel_inputs:
        message = "synchronized-panel strategy requires immutable panel inputs"
        raise EquityRunError(message)
    if any(session < config.eval_start for session in dataset.panel_inputs):
        message = (
            "evaluation start cannot omit an earlier synchronized panel and its portfolio state"
        )
        raise EquityRunError(message)
    in_window = tuple(
        (session, panel_input)
        for session, panel_input in sorted(dataset.panel_inputs.items())
        if config.eval_start <= session <= config.eval_end
    )
    if not in_window:
        message = "synchronized-panel strategy has no panel inputs in the evaluation period"
        raise EquityRunError(message)
    strategy = strategy_factory()
    if not isinstance(strategy, SynchronizedPanelStrategy):  # pragma: no cover - caller contract
        message = "panel backtest requires a synchronized-panel strategy"
        raise EquityRunError(message)
    for session, panel_input in in_window:
        execution_session = strategy.panel_ready_input(panel_input).execution_session.session_date
        if execution_session > config.eval_end:
            message = (
                f"synchronized panel {session} executes after the evaluation end; "
                "extend the window to include its declared execution session"
            )
            raise EquityRunError(message)
    return EquityPortfolioBacktest(
        dataset,
        strategy_factory,
        config,
        universe_policy=PanelExecutionUniversePolicy(),
        cadence_policy=StaticExecutionUniverseCadencePolicy(),
        panel_context_policy=SynchronizedStrategyPanelContextPolicy(),
        execution_cost_policy=institutional_equity_execution_cost_policy(
            scenario_name="expected",
        ),
        corporate_action_policy=ExplicitRawCorporateActionPolicy(),
    ).run()


def run_backtest(
    snapshot_dir: Path,
    manifest_path: Path,
    out_dir: Path,
    *,
    strategy_dir: Path,
    provider: ValidationProvider,
    eval_start: date,
    eval_end: date | None,
    declared_snapshot_scope: SnapshotFactorClaimScope | None = None,
    comparison_portfolio: bool = False,
    pinned_universe: bool = False,
    strategy_panel_manifest: Path | None = None,
) -> BacktestResult:
    """Verify one immutable snapshot and run a signal strategy against it."""
    dataset, manifest = load_snapshot(
        snapshot_dir,
        manifest_path,
        admission_scope=declared_snapshot_scope,
    )
    owns_snapshot = _assert_run_matches_manifest(
        manifest,
        provider=provider,
        strategy_dir=strategy_dir,
        allow_comparison_portfolio=comparison_portfolio,
    )
    resolved_eval_end = eval_end or dataset.sessions[-1]
    if eval_start > resolved_eval_end:
        message = "evaluation start must not be after evaluation end"
        raise EquityRunError(message)
    if eval_start < dataset.sessions[0] or resolved_eval_end > dataset.sessions[-1]:
        message = "evaluation period must be contained by verified snapshot sessions"
        raise EquityRunError(message)
    config = PortfolioConfig(eval_start=eval_start, eval_end=resolved_eval_end)
    core_cls, identity = _strategy_identity(strategy_dir)
    strategy_payload = json.loads((strategy_dir / "config.json").read_text(encoding="utf-8"))
    if not isinstance(strategy_payload, dict):
        message = f"strategy config must be an object: {strategy_dir / 'config.json'}"
        raise EquityRunError(message)
    strategy_config = build_strategy_core_parameters(strategy_payload)

    def _strategy_factory() -> PureSignalStrategy:
        concrete_factory = cast(Callable[..., PureSignalStrategy], core_cls)
        return concrete_factory(config=strategy_config)

    strategy_probe = _strategy_factory()
    if isinstance(strategy_probe, SynchronizedPanelStrategy):
        if strategy_panel_manifest is None:
            message = "synchronized-panel strategy requires --strategy-panel-manifest"
            raise EquityRunError(message)
        if pinned_universe:
            message = "--pinned-universe cannot override a synchronized panel's universe"
            raise EquityRunError(message)
        try:
            dataset = compose_strategy_panel_dataset(
                dataset,
                artifact_root=snapshot_dir,
                manifest_path=strategy_panel_manifest,
                historical_snapshot_manifest_sha256=evidence_sha256(manifest),
                strategy_identity=identity,
                strategy=strategy_probe,
            )
        except EquityStrategyPanelError as exc:
            raise EquityRunError(str(exc)) from exc
        result = _run_panel_backtest(dataset, _strategy_factory, config)
        result.policies["strategy_panel_manifest_schema"] = STRATEGY_PANEL_MANIFEST_SCHEMA
        if dataset.panel_manifest_sha256 is None:  # pragma: no cover - composition contract
            message = "strategy-panel composition omitted its verified manifest identity"
            raise EquityRunError(message)
        result.policies["strategy_panel_manifest_sha256"] = dataset.panel_manifest_sha256
    else:
        if strategy_panel_manifest is not None:
            message = "--strategy-panel-manifest requires a synchronized-panel strategy"
            raise EquityRunError(message)
        result = _run_non_panel_backtest(
            dataset,
            _strategy_factory,
            config,
            strategy_payload=strategy_payload,
            strategy_config=strategy_config,
            pinned_universe=pinned_universe,
        )
    if not owns_snapshot:
        _stamp_comparison_portfolio_lineage(result, manifest, identity=identity)
    _publish_backtest_with_baseline_comparison(out_dir, result, dataset, config, manifest)
    return result


def _execute_cli_run(args: argparse.Namespace) -> BacktestResult:
    declared_scope = getattr(args, "snapshot_claim_scope", None)
    return run_backtest(
        args.snapshot,
        args.manifest,
        args.out,
        strategy_dir=args.strategy,
        provider=ValidationProvider(args.provider),
        eval_start=args.eval_start if args.eval_start is not None else date(2020, 1, 1),
        eval_end=args.eval_end,
        declared_snapshot_scope=(
            SnapshotFactorClaimScope(declared_scope) if declared_scope else None
        ),
        comparison_portfolio=bool(getattr(args, "comparison_portfolio", False)),
        pinned_universe=bool(getattr(args, "pinned_universe", False)),
        strategy_panel_manifest=getattr(args, "strategy_panel_manifest", None),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if args.command == "compile-official-sessions":
        compiled = compile_nyse_official_session_artifact(
            start=args.start,
            end=args.end,
            source_artifact=args.source_artifact,
            source_artifact_sha256=args.source_artifact_sha256,
        )
        stored = store_content_object(
            args.output_root,
            compiled.content,
            suffix=".json",
            role="official_sessions",
            media_type="application/json",
            row_count=compiled.session_count,
            context={
                "coverage_from": compiled.coverage_from.isoformat(),
                "coverage_to": compiled.coverage_to.isoformat(),
                "source_revision": compiled.source_revision,
            },
        )
        print(
            json.dumps(
                {
                    "content_sha256": compiled.content_sha256,
                    "path": str(args.output_root / stored.path),
                    "session_count": compiled.session_count,
                    "source_revision": compiled.source_revision,
                },
                indent=2,
            )
        )
        return 0
    if args.command == "acquire-membership-correction-evidence":
        user_agent = os.environ.get("EDGAR_USER_AGENT", "").strip()
        if not user_agent:
            message = (
                "EDGAR_USER_AGENT is required to acquire reviewed membership correction sources"
            )
            raise EquityRunError(message)
        try:
            frozen = acquire_membership_correction_evidence(
                args.snapshot,
                source_spec_path=args.source_spec,
                config=MembershipCorrectionAcquisitionConfig(user_agent=user_agent),
            )
        except MembershipCorrectionError as exc:
            raise EquityRunError(str(exc)) from exc
        print(
            json.dumps(
                {
                    "artifact_count": len(frozen.artifacts),
                    "manifest": str(frozen.manifest_path),
                    "manifest_sha256": frozen.manifest_sha256,
                },
                indent=2,
            )
        )
        return 0
    if args.command == "fetch":
        acquisition_result = fetch_snapshot(
            args.snapshot,
            start=args.start,
            end=args.end,
            strategy_dir=args.strategy,
            membership_path=args.membership,
            aliases_path=args.aliases,
            benchmark=BenchmarkSpec(
                price_symbol=args.benchmark_price,
                total_return_symbol=args.benchmark_total_return,
                total_return_kind=BenchmarkTotalReturnKind(args.benchmark_total_return_kind),
                volatility_symbol=args.benchmark_volatility,
            ),
            provider=ValidationProvider(args.provider),
            entitlement_scope=args.entitlement_scope,
            dataset_version=args.dataset_version,
            split_adjustment_through=args.split_adjustment_through,
            calendar_mic=args.calendar_mic,
            official_sessions_path=args.official_sessions,
            official_sessions_sha256=args.official_sessions_sha256,
            minimum_session_coverage=args.minimum_session_coverage,
            security_history_sessions=args.security_history_sessions,
            membership_index_symbol=args.membership_index,
            membership_evidence_start=args.membership_evidence_start,
            membership_authority_correction_manifest_path=(
                args.membership_authority_corrections_manifest
            ),
            membership_materialization_manifest_path=(args.membership_materialization_manifest),
        )
        print(
            json.dumps(
                {
                    "manifest": str(acquisition_result.manifest_path),
                    "manifest_sha256": acquisition_result.manifest_sha256,
                    "complete": acquisition_result.complete,
                    "dispositions": dict(acquisition_result.disposition_counts),
                },
                indent=2,
            )
        )
        return 0 if acquisition_result.complete else 2
    backtest_result = _execute_cli_run(args)
    print(
        json.dumps(
            {
                "metrics": backtest_result.metrics,
                "benchmark": backtest_result.benchmark,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
