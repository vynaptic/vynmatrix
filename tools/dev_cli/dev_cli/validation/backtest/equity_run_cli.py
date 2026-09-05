"""Argument contracts for the immutable US-equity validation command."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from dev_cli.validation.backtest.equity_run_inputs import (
    _BENCHMARK_SYMBOL,
    _MIC,
    _PROVIDER_DATASET_VERSION,
    _REGISTERED_SECURITY_HISTORY_SESSIONS,
    _TR_KIND,
    _TR_SYMBOL,
    _VIX_SYMBOL,
)
from dev_cli.validation.backtest.equity_snapshot import (
    BenchmarkTotalReturnKind,
    ValidationProvider,
)
from dev_cli.validation.backtest.equity_snapshot_admission import (
    SnapshotFactorClaimScope,
)


def _add_snapshot_calendar_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--calendar-mic", default=_MIC)
    parser.add_argument(
        "--official-sessions",
        type=Path,
        default=None,
        help=(
            "caller-supplied historical exchange/broker session JSON; omission "
            "uses diagnostic-only exchange_calendars timing"
        ),
    )
    parser.add_argument(
        "--official-sessions-sha256",
        default=None,
        help="expected SHA-256 of --official-sessions",
    )


def _add_official_session_compiler_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    sessions_p = subparsers.add_parser(
        "compile-official-sessions",
        help="compile content-pinned official ICE/NYSE XNYS sessions",
    )
    sessions_p.add_argument("--output-root", type=Path, required=True)
    sessions_p.add_argument("--start", type=date.fromisoformat, default=date(2018, 11, 27))
    sessions_p.add_argument("--end", type=date.fromisoformat, default=date(2026, 1, 6))
    sessions_p.add_argument(
        "--source-artifact",
        type=Path,
        default=None,
        help="reuse an exact prior compiler output containing all pinned ICE source bytes",
    )
    sessions_p.add_argument(
        "--source-artifact-sha256",
        default=None,
        help="required content identity for --source-artifact",
    )


def _add_fetch_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    fetch_p = subparsers.add_parser("fetch")
    fetch_p.add_argument("--snapshot", type=Path, required=True)
    fetch_p.add_argument("--strategy", type=Path, required=True)
    fetch_p.add_argument(
        "--membership",
        type=Path,
        default=None,
        help=(
            "pre-materialized point-in-time membership CSV; when omitted, acquire "
            "licensed EODHD ticker intervals, checkpoint crosschecks, directories, "
            "and ID mappings"
        ),
    )
    fetch_p.add_argument(
        "--membership-materialization-manifest",
        type=Path,
        default=None,
        help=(
            "exact frozen EODHD membership materialization manifest below --snapshot; "
            "resumes without reacquiring membership or identity evidence"
        ),
    )
    fetch_p.add_argument("--aliases", type=Path, default=None)
    fetch_p.add_argument(
        "--membership-index",
        default=_BENCHMARK_SYMBOL,
        help="EODHD .INDX symbol used when --membership is omitted",
    )
    fetch_p.add_argument(
        "--membership-evidence-start",
        type=date.fromisoformat,
        default=date(2018, 1, 1),
        help=(
            "earliest EODHD full-state checkpoint requested to prove null-start "
            "ticker-history incumbents on or before --start"
        ),
    )
    fetch_p.add_argument(
        "--membership-authority-corrections-manifest",
        type=Path,
        default=None,
        help=(
            "frozen reviewed primary-source correction manifest below --snapshot; "
            "valid only when --membership is omitted"
        ),
    )
    fetch_p.add_argument(
        "--provider",
        choices=[provider.value for provider in ValidationProvider],
        required=True,
    )
    fetch_p.add_argument("--entitlement-scope", required=True)
    fetch_p.add_argument("--dataset-version", default=_PROVIDER_DATASET_VERSION)
    _add_snapshot_calendar_arguments(fetch_p)
    fetch_p.add_argument("--benchmark-price", default=_BENCHMARK_SYMBOL)
    fetch_p.add_argument("--benchmark-total-return", default=_TR_SYMBOL)
    fetch_p.add_argument(
        "--benchmark-total-return-kind",
        choices=[kind.value for kind in BenchmarkTotalReturnKind],
        default=_TR_KIND.value,
    )
    fetch_p.add_argument("--benchmark-volatility", default=_VIX_SYMBOL)
    fetch_p.add_argument("--minimum-session-coverage", type=float, default=1.0)
    fetch_p.add_argument(
        "--security-history-sessions",
        type=int,
        default=_REGISTERED_SECURITY_HISTORY_SESSIONS,
        help=(
            "price-only sessions retained before each membership interval for "
            "registered factors; sourced aliases are mandatory and eligibility "
            "never begins early"
        ),
    )
    fetch_p.add_argument("--start", type=date.fromisoformat, default=date(2019, 1, 2))
    fetch_p.add_argument("--end", type=date.fromisoformat, required=True)
    fetch_p.add_argument(
        "--split-adjustment-through",
        type=date.fromisoformat,
        required=True,
        help=(
            "exact UTC acquisition date through which split tails are fetched; "
            "must equal the acquisition date and is not proof of EODHD's undocumented basis"
        ),
    )


def _add_run_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    run_p = subparsers.add_parser("run")
    run_p.add_argument("--snapshot", type=Path, required=True)
    run_p.add_argument("--manifest", type=Path, required=True)
    run_p.add_argument("--strategy", type=Path, required=True)
    run_p.add_argument(
        "--provider",
        choices=[provider.value for provider in ValidationProvider],
        required=True,
    )
    run_p.add_argument("--out", type=Path, required=True)
    run_p.add_argument("--eval-start", type=date.fromisoformat, default=None)
    run_p.add_argument("--eval-end", type=date.fromisoformat, default=None)
    run_p.add_argument(
        "--strategy-panel-manifest",
        type=Path,
        default=None,
        help=(
            "exact content-addressed synchronized-panel manifest below the snapshot; "
            "required only when the selected core implements the panel contract"
        ),
    )
    run_p.add_argument("--artifact-root", type=Path, default=Path(".artifacts"))
    run_p.add_argument("--database-url", default=None)
    run_p.add_argument("--campaign-hash", default=None)
    run_p.add_argument("--trial-sequence", type=int, default=None)
    run_p.add_argument(
        "--snapshot-claim-scope",
        default=None,
        choices=[item.value for item in SnapshotFactorClaimScope],
        help=(
            "declare the snapshot admission scope for an unregistered "
            "comparison run; registered trials take it from the ledger"
        ),
    )
    run_p.add_argument(
        "--comparison-portfolio",
        action="store_true",
        help=(
            "evaluate a strategy that does not own this snapshot as a labelled "
            "comparison portfolio; the result records both strategy identities"
        ),
    )
    run_p.add_argument(
        "--pinned-universe",
        action="store_true",
        help=(
            "contamination control: select from the strategy config's pinned "
            "present-day universe instead of point-in-time ADV; results are "
            "look-ahead contaminated and never decision-grade"
        ),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the single production CLI contract for equity validation."""

    parser = argparse.ArgumentParser(prog="equity_run")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_official_session_compiler_parser(subparsers)
    correction_p = subparsers.add_parser("acquire-membership-correction-evidence")
    correction_p.add_argument("--snapshot", type=Path, required=True)
    correction_p.add_argument("--source-spec", type=Path, required=True)
    _add_fetch_parser(subparsers)
    _add_run_parser(subparsers)
    return parser
