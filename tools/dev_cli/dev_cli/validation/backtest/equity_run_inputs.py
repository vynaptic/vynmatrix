"""Immutable US-equity acquisition, manifest verification, and input loading."""

from __future__ import annotations

import csv
import math
import os
import subprocess
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, cast

from dev_cli.utils.helpers import get_project_root
from dev_cli.validation.backtest.equity_membership_eodhd import (
    EODHDMembershipMaterializationError,
    load_frozen_eodhd_membership_materialization,
    materialize_eodhd_index_membership,
)
from dev_cli.validation.backtest.equity_portfolio import (
    DailyBar,
    EquityCorporateAction,
    EquityCorporateActionKind,
    EquityDataset,
)
from dev_cli.validation.backtest.equity_snapshot import (
    ArtifactRecord,
    BenchmarkSpec,
    BenchmarkTotalReturnKind,
    DailyHistoricalProvider,
    SnapshotAcquisitionRequest,
    SnapshotAcquisitionResult,
    SnapshotValidationError,
    StrategyIdentity,
    ValidationProvider,
    acquire_historical_snapshot,
    build_strategy_identity,
    load_snapshot_manifest,
    manifest_artifacts,
    read_membership_intervals,
    verified_artifact_path,
)
from dev_cli.validation.backtest.equity_snapshot_admission import (
    SnapshotFactorAdmissionError,
    SnapshotFactorClaimScope,
    disposition_registered_panel_exclusion,
    evaluate_snapshot_factor_admission,
)
from dev_cli.validation.backtest.equity_snapshot_identity import (
    HistoricalMarketSessions,
    read_historical_market_sessions,
)
from dev_cli.validation.evidence import (
    ContentAddressedArtifact,
)
from dev_cli.validation.providers.eodhd import EODHDClient, EODHDMarketDataError
from lib_common.hashing import canonical_json_hash, sha256_file
from lib_common.logging import get_logger
from lib_data.adjustments import (
    AdjustmentError,
    AdjustmentEvent,
    apply_adjustments,
    cumulative_factors_asof,
)
from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import PureSignalStrategy

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

_MIC = "XNYS"
_BENCHMARK_SYMBOL = "GSPC.INDX"
_VIX_SYMBOL = "VIX.INDX"
_TR_SYMBOL = "SPY"
_TR_KIND = BenchmarkTotalReturnKind.ADJUSTED_SECURITY
_WORST_TRADES_COUNT = 10
_WEIGHT_BPS_TOTAL = 10_000
_SHA256_HEX_LENGTH = 64
_PROVIDER_PRODUCT = "EODHD End-of-Day Data API"
_PROVIDER_DATASET_VERSION = "unversioned-api-content-addressed"
_DEFAULT_MINIMUM_MEDIAN_DAILY_NOTIONAL = 50_000_000.0
_PRIMARY_BAR_ROLE = "security_bars"
# The production market-factor policy consumes 274 sessions (252-day momentum
# plus the 21-session skip and current observation).  Acquisition registers
# one earlier session as an adjustment anchor so a dividend or split on the
# first factor observation can be applied without inventing a prior close.
_REGISTERED_SECURITY_HISTORY_SESSIONS = 275
# Validation commands run from an installed ``vmdev`` distribution.  Deriving
# the checkout from ``__file__`` therefore points inside site-packages and can
# silently bind the wrong source tree.  The shared CLI resolver requires the
# caller's repository markers and works identically for source and installed
# command entry points.
_REPO_ROOT = get_project_root().resolve()
_MEMBERSHIP_CSV = _REPO_ROOT / "config" / "universe" / "sp500_membership_full.csv"
_ADJUSTMENTS_SOURCE = _REPO_ROOT / "libs" / "python" / "lib_data" / "lib_data" / "adjustments.py"
_EODHD_CLIENT_SOURCE = (
    _REPO_ROOT
    / "libs"
    / "python"
    / "lib_infrastructure"
    / "lib_infrastructure"
    / "market_data"
    / "eodhd_client.py"
)


class EquityRunError(SnapshotValidationError):
    """Raised when the snapshot or run inputs cannot be trusted (fail loud)."""


def read_membership(path: Path = _MEMBERSHIP_CSV) -> dict[str, list[tuple[date, date | None]]]:
    """Return the portfolio harness shape from the validated interval owner."""

    membership: dict[str, list[tuple[date, date | None]]] = {}
    for interval in read_membership_intervals(path):
        membership.setdefault(interval.canonical_symbol, []).append(
            (interval.effective_from, interval.effective_to)
        )
    return membership


def _membership_from_artifact(
    snapshot_dir: Path,
    manifest: Mapping[str, object],
) -> dict[str, list[tuple[date, date | None]]]:
    artifacts = manifest_artifacts(manifest, role="membership")
    if len(artifacts) != 1:
        message = f"snapshot requires exactly one membership artifact, found {len(artifacts)}"
        raise EquityRunError(message)
    return read_membership(verified_artifact_path(snapshot_dir, artifacts[0]))


def _one_artifact(
    manifest: Mapping[str, object],
    *,
    role: str,
) -> ArtifactRecord:
    artifacts = manifest_artifacts(manifest, role=role)
    if len(artifacts) != 1:
        message = f"snapshot requires exactly one {role!r} artifact, found {len(artifacts)}"
        raise EquityRunError(message)
    return artifacts[0]


def _strategy_identity(
    strategy_dir: Path,
) -> tuple[type[PureSignalStrategy], StrategyIdentity]:
    identity = build_strategy_identity(strategy_dir, repo_root=_REPO_ROOT)
    core_cls = load_pure_strategy_core(
        strategy_dir,
        expected_class_name=identity.algorithm_type_name,
    )
    return core_cls, identity


def _assert_strategy_matches_manifest(
    strategy_dir: Path,
    manifest: Mapping[str, object],
    *,
    allow_comparison_portfolio: bool = False,
) -> bool:
    """Bind a run to its snapshot's owning strategy; report a labelled comparison.

    Returns ``True`` when the selected strategy is the snapshot's owner. A
    labelled comparison portfolio deliberately evaluates a different strategy
    on the owner's evidence — that is the only way a head-to-head reads
    identical prices, membership, and costs — and is admitted only when the
    caller opts in explicitly; the result records both identities.
    """
    _core_cls, identity = _strategy_identity(strategy_dir)
    observed = manifest.get("strategy")
    if not isinstance(observed, Mapping):
        message = "snapshot manifest strategy identity is missing"
        raise EquityRunError(message)
    expected = {
        "strategy_id": identity.strategy_id,
        "strategy_version": identity.strategy_version,
        "algorithm_type_name": identity.algorithm_type_name,
        "relative_path": identity.relative_path,
        "config_sha256": identity.config_sha256,
        "source_tree_sha256": identity.source_tree_sha256,
    }
    if dict(observed) == expected:
        return True
    if not allow_comparison_portfolio:
        message = "selected strategy source/config identity differs from the snapshot manifest"
        raise EquityRunError(message)
    return False


def _assert_provider_matches_manifest(
    provider: ValidationProvider,
    manifest: Mapping[str, object],
) -> None:
    provider_manifest = manifest.get("provider")
    if not isinstance(provider_manifest, Mapping):
        message = "snapshot manifest provider identity is missing"
        raise EquityRunError(message)
    if (
        provider_manifest.get("name") != provider.value
        or provider_manifest.get("scope") != "historical_validation_only"
    ):
        message = "selected provider differs from the validation-only snapshot provider"
        raise EquityRunError(message)


def _artifact_context(artifact: ArtifactRecord, field: str) -> str:
    value = artifact.context.get(field)
    if not isinstance(value, str) or not value:
        message = f"artifact {artifact.path} lacks context field {field!r}"
        raise EquityRunError(message)
    return value


def _merge_bar_artifacts(
    snapshot_dir: Path,
    artifacts: Sequence[ArtifactRecord],
    *,
    foreign_member_windows: Sequence[tuple[date, date | None]] = (),
) -> list[tuple[date, float, float, float, float, float | None]]:
    """Merge one security's bar objects across its acquisition windows.

    The membership window (``security_bars``) is the pinned split-adjustment
    basis and owns every session it covers. A registered warm-up prefix
    (``security_history_bars``) was acquired under a different adjustment
    horizon, so the provider can report the same session a cent apart or with
    a different split-adjusted volume; it therefore only fills sessions the
    membership window does not contain. A recycled ticker is different again:
    two companies traded one symbol (ARNC was Arconic Inc, then the 2020
    Arconic Corporation), so sessions owned by a foreign member are clipped.
    Two objects of the same role disagreeing on one session stays fail-closed.
    """
    rows_by_session: dict[date, tuple[date, float, float, float, float, float | None]] = {}
    owning_role: dict[date, str] = {}
    ordered = sorted(artifacts, key=lambda item: item.role != _PRIMARY_BAR_ROLE)
    for artifact in ordered:
        for row in _read_bars_csv(verified_artifact_path(snapshot_dir, artifact)):
            session = row[0]
            if any(start <= session <= (end or session) for start, end in foreign_member_windows):
                continue
            existing = rows_by_session.get(session)
            if existing is None:
                rows_by_session[session] = row
                owning_role[session] = artifact.role
                continue
            if existing == row or owning_role[session] != artifact.role:
                continue
            message = (
                f"duplicate session {session} across snapshot bar objects: "
                f"{existing} vs {row} (artifact {artifact.path})"
            )
            raise EquityRunError(message)
    return [rows_by_session[session] for session in sorted(rows_by_session)]


def _merge_action_artifacts(
    snapshot_dir: Path,
    artifacts: Sequence[ArtifactRecord],
) -> list[AdjustmentEvent]:
    events: dict[tuple[date, str], AdjustmentEvent] = {}
    for artifact in artifacts:
        for event in _read_actions_csv(verified_artifact_path(snapshot_dir, artifact)):
            key = (event.ex_date, event.action_type)
            if key in events and events[key] != event:
                message = f"conflicting {event.action_type} events on {event.ex_date}"
                raise EquityRunError(message)
            events[key] = event
    return [events[key] for key in sorted(events)]


def _git_rev() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# fetch: validation provider -> immutable snapshot
# ---------------------------------------------------------------------------


def _write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _validate_membership_source_selection(
    *,
    membership_path: Path | None,
    aliases_path: Path | None,
    correction_manifest_path: Path | None,
    materialization_manifest_path: Path | None,
) -> None:
    if materialization_manifest_path is None:
        return
    conflicts = [
        flag
        for flag, configured in (
            ("--membership", membership_path is not None),
            ("--aliases", aliases_path is not None),
            (
                "--membership-authority-corrections-manifest",
                correction_manifest_path is not None,
            ),
        )
        if configured
    ]
    if conflicts:
        message = "--membership-materialization-manifest cannot be combined with " + ", ".join(
            conflicts
        )
        raise EquityRunError(message)


def fetch_snapshot(
    snapshot_dir: Path,
    *,
    start: date,
    end: date,
    strategy_dir: Path,
    membership_path: Path | None,
    aliases_path: Path | None,
    benchmark: BenchmarkSpec,
    provider: ValidationProvider,
    entitlement_scope: str,
    dataset_version: str,
    split_adjustment_through: date,
    calendar_mic: str = _MIC,
    official_sessions_path: Path | None = None,
    official_sessions_sha256: str | None = None,
    minimum_session_coverage: float = 1.0,
    security_history_sessions: int = _REGISTERED_SECURITY_HISTORY_SESSIONS,
    membership_index_symbol: str = _BENCHMARK_SYMBOL,
    membership_evidence_start: date = date(2018, 1, 1),
    membership_authority_correction_manifest_path: Path | None = None,
    membership_materialization_manifest_path: Path | None = None,
) -> SnapshotAcquisitionResult:
    """Acquire one validation-only snapshot without shrinking the requested universe."""

    if provider is not ValidationProvider.EODHD:
        message = f"unsupported historical validation provider: {provider.value}"
        raise EquityRunError(message)
    _validate_membership_source_selection(
        membership_path=membership_path,
        aliases_path=aliases_path,
        correction_manifest_path=membership_authority_correction_manifest_path,
        materialization_manifest_path=membership_materialization_manifest_path,
    )
    entitlement_owner_user_id = os.environ.get("SP500_RESEARCH_OWNER_USER_ID", "").strip()
    if not entitlement_owner_user_id:
        message = (
            "SP500_RESEARCH_OWNER_USER_ID must name the exact platform user who owns "
            "the personal EODHD research entitlement"
        )
        raise EquityRunError(message)
    membership_evidence: tuple[ContentAddressedArtifact, ...] = ()
    membership_lineage: Mapping[str, object] | None = None
    permanent_identity_complete: bool | None = None
    membership_authority_complete: bool | None = None
    resolved_membership = membership_path
    resolved_aliases = aliases_path
    if membership_materialization_manifest_path is not None:
        try:
            membership = load_frozen_eodhd_membership_materialization(
                snapshot_dir,
                membership_materialization_manifest_path,
                expected_dataset_version=dataset_version,
                expected_entitlement_scope=entitlement_scope,
                expected_entitlement_owner_user_id=entitlement_owner_user_id,
                expected_index_symbol=membership_index_symbol,
                expected_evidence_start=membership_evidence_start,
                expected_requested_start=start,
                expected_requested_end=end,
            )
        except EODHDMembershipMaterializationError as exc:
            raise EquityRunError(str(exc)) from exc
        resolved_membership = membership.membership_path
        resolved_aliases = membership.aliases_path
        membership_evidence = membership.evidence_artifacts
        membership_lineage = membership.lineage
        permanent_identity_complete = membership.permanent_identity_complete
        membership_authority_complete = membership.membership_authority_complete
        logger.info(
            "resuming frozen membership materialization: manifest=%s hash=%s",
            membership.manifest_path,
            membership.manifest_sha256,
        )
    api_token = os.environ.get("EODHD_API_TOKEN", "")
    if not api_token:
        msg = "EODHD_API_TOKEN must be set for historical validation fetch"
        raise EquityRunError(msg)
    client = EODHDClient(api_token=api_token)
    try:
        if resolved_membership is None:
            if aliases_path is not None:
                message = "--aliases cannot be combined with automatic EODHD membership"
                raise EquityRunError(message)
            try:
                membership = materialize_eodhd_index_membership(
                    snapshot_dir,
                    provider=client,
                    index_symbol=membership_index_symbol,
                    evidence_start=membership_evidence_start,
                    requested_start=start,
                    requested_end=end,
                    dataset_version=dataset_version,
                    entitlement_scope=entitlement_scope,
                    entitlement_owner_user_id=entitlement_owner_user_id,
                    authority_correction_manifest_path=(
                        membership_authority_correction_manifest_path
                    ),
                )
            except EODHDMembershipMaterializationError as exc:
                raise EquityRunError(str(exc)) from exc
            resolved_membership = membership.membership_path
            resolved_aliases = membership.aliases_path
            membership_evidence = membership.evidence_artifacts
            membership_lineage = membership.lineage
            permanent_identity_complete = membership.permanent_identity_complete
            membership_authority_complete = membership.membership_authority_complete
            logger.info(
                "frozen membership materialization: manifest=%s hash=%s",
                membership.manifest_path,
                membership.manifest_sha256,
            )
        elif membership_authority_correction_manifest_path is not None:
            message = (
                "--membership-authority-corrections-manifest requires automatic "
                "EODHD membership acquisition"
            )
            raise EquityRunError(message)
        assert resolved_membership is not None
        _core_cls, strategy_identity = _strategy_identity(strategy_dir)
        request = SnapshotAcquisitionRequest(
            provider=provider,
            provider_product=_PROVIDER_PRODUCT,
            dataset_version=dataset_version,
            entitlement_scope=entitlement_scope,
            entitlement_owner_user_id=entitlement_owner_user_id,
            start=start,
            end=end,
            calendar_mic=calendar_mic,
            membership_path=resolved_membership,
            aliases_path=resolved_aliases,
            benchmark=benchmark,
            strategy=strategy_identity,
            git_revision=_git_rev(),
            adjustment_version=sha256_file(_ADJUSTMENTS_SOURCE),
            split_adjustment_through=split_adjustment_through,
            # EODHD documents the field as split-adjusted but does not publish
            # the adjustment horizon/revision or integer-rounding convention.
            # A provider response or separately frozen authority is required
            # before this may become true.
            split_adjustment_basis_complete=False,
            official_sessions_path=official_sessions_path,
            official_sessions_sha256=official_sessions_sha256,
            security_history_sessions=security_history_sessions,
            minimum_session_coverage=minimum_session_coverage,
            membership_evidence_artifacts=membership_evidence,
            membership_lineage=membership_lineage,
            permanent_identity_complete=permanent_identity_complete,
            membership_authority_complete=membership_authority_complete,
        )
        result = acquire_historical_snapshot(
            snapshot_dir,
            request=request,
            provider=cast(DailyHistoricalProvider, client),
            provider_errors=(EODHDMarketDataError, SnapshotValidationError),
            repo_root=_REPO_ROOT,
            acquisition_code_paths=(
                Path(__file__),
                Path(__file__).with_name("equity_snapshot.py"),
                Path(__file__).with_name("equity_snapshot_adjustments.py"),
                Path(__file__).with_name("equity_snapshot_identity.py"),
                Path(__file__).with_name("equity_snapshot_admission.py"),
                Path(__file__).with_name("equity_membership_eodhd.py"),
                Path(__file__).with_name("equity_membership_eodhd_source.py"),
                Path(__file__).with_name("equity_membership_bundle.py"),
                Path(__file__).with_name("equity_membership_corrections.py"),
                Path(__file__).parents[1] / "providers" / "eodhd.py",
                _ADJUSTMENTS_SOURCE,
                _EODHD_CLIENT_SOURCE,
            ),
        )
    finally:
        client.close()
    logger.info(
        "historical snapshot finalized: manifest=%s complete=%s dispositions=%s",
        result.manifest_sha256,
        result.complete,
        dict(result.disposition_counts),
    )
    return result


def _read_bars_csv(path: Path) -> list[tuple[date, float, float, float, float, float | None]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        expected_fields = (
            "session",
            "open",
            "high",
            "low",
            "close",
            "split_adjusted_volume",
        )
        if tuple(reader.fieldnames or ()) != expected_fields:
            message = (
                "equity bar artifact must declare raw OHLC and split_adjusted_volume explicitly"
            )
            raise EquityRunError(message)
        return [
            (
                date.fromisoformat(row["session"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                (
                    float(row["split_adjusted_volume"])
                    if row["split_adjusted_volume"] not in ("", "None")
                    else None
                ),
            )
            for row in reader
        ]


def _read_actions_csv(path: Path) -> list[AdjustmentEvent]:
    if not path.exists():
        return []
    events: list[AdjustmentEvent] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            events.append(_adjustment_event(row))
    return events


def _adjustment_event(row: Mapping[str, str]) -> AdjustmentEvent:
    return AdjustmentEvent(
        ex_date=date.fromisoformat(row["ex_date"]),
        action_type=row["action_type"],
        ratio=Decimal(row["ratio"]) if row["ratio"] else None,
        amount=Decimal(row["amount"]) if row["amount"] else None,
    )


def _lineaged_security_actions(
    snapshot_dir: Path,
    *,
    symbol: str,
    artifacts: Sequence[ArtifactRecord],
) -> tuple[list[AdjustmentEvent], list[EquityCorporateAction]]:
    rows: dict[str, tuple[dict[str, str], set[str]]] = {}
    event_identity: dict[tuple[str, str], str] = {}
    for artifact in artifacts:
        path = verified_artifact_path(snapshot_dir, artifact)
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                canonical_row = {
                    "action_type": row["action_type"],
                    "amount": row["amount"],
                    "currency": row["currency"],
                    "ex_date": row["ex_date"],
                    "ratio": row["ratio"],
                }
                canonical_identity = canonical_json_hash(
                    {"canonical_symbol": symbol, "row": canonical_row}
                )
                event_key = (canonical_row["action_type"], canonical_row["ex_date"])
                existing_identity = event_identity.setdefault(event_key, canonical_identity)
                if existing_identity != canonical_identity:
                    message = f"conflicting corporate-action rows for {symbol}/{event_key[1]}"
                    raise EquityRunError(message)
                stored_row, artifact_sha256s = rows.setdefault(
                    canonical_identity,
                    (canonical_row, set()),
                )
                if stored_row != canonical_row:
                    message = f"corporate-action identity collision for {symbol}"
                    raise EquityRunError(message)
                artifact_sha256s.add(artifact.sha256)
    pairs: list[tuple[AdjustmentEvent, EquityCorporateAction]] = []
    for canonical_identity in sorted(rows):
        canonical_row, artifact_sha256s = rows[canonical_identity]
        row_digest = canonical_json_hash(
            {
                "artifact_sha256s": sorted(artifact_sha256s),
                "canonical_symbol": symbol,
                "row": canonical_row,
            }
        )
        adjustment = _adjustment_event(canonical_row)
        if adjustment.action_type == "split":
            if adjustment.ratio is None:
                message = f"split action for {symbol} has no ratio"
                raise EquityRunError(message)
            kind = EquityCorporateActionKind.SPLIT
            value = float(adjustment.ratio)
            currency = None
        else:
            if adjustment.amount is None:
                message = f"dividend action for {symbol} has no amount"
                raise EquityRunError(message)
            kind = EquityCorporateActionKind.CASH_DIVIDEND
            value = float(adjustment.amount)
            currency = canonical_row["currency"].strip().upper()
            if currency != "USD":
                message = (
                    f"dividend action for {symbol} requires USD currency; "
                    f"observed {currency or 'missing'}"
                )
                raise EquityRunError(message)
        pairs.append(
            (
                adjustment,
                EquityCorporateAction(
                    symbol=symbol,
                    effective_session=adjustment.ex_date,
                    kind=kind,
                    value=value,
                    source_observation_id=row_digest,
                    currency=currency,
                ),
            )
        )
    paired = sorted(
        pairs,
        key=lambda pair: (
            pair[0].ex_date,
            pair[0].action_type,
            pair[1].source_observation_id,
        ),
    )
    return [pair[0] for pair in paired], [pair[1] for pair in paired]


def _adjusted_daily_bars(
    source: list[tuple[date, float, float, float, float, float | None]],
    events: list[AdjustmentEvent],
) -> list[DailyBar]:
    closes_by_date = {bar[0]: bar[4] for bar in source}
    split_factors: list[float] = []
    for bar in source:
        cumulative = cumulative_factors_asof(
            events,
            closes_by_date,
            bar_date=bar[0],
        )
        if not math.isfinite(cumulative.volume) or cumulative.volume <= 0.0:
            message = f"cumulative split volume factor is invalid for {bar[0]}"
            raise EquityRunError(message)
        split_factor = 1.0 / cumulative.volume
        split_factors.append(split_factor)
    price_only: list[tuple[date, float, float, float, float, float | None]] = [
        (*bar[:5], None) for bar in source
    ]
    adjusted = apply_adjustments(price_only, events)
    return [
        DailyBar(
            session=adj[0],
            open=adj[1],
            high=adj[2],
            low=adj[3],
            close=adj[4],
            raw_open=orig[1],
            raw_high=orig[2],
            raw_low=orig[3],
            raw_close=orig[4],
            split_adjusted_open=orig[1] * split_factor,
            split_adjusted_high=orig[2] * split_factor,
            split_adjusted_low=orig[3] * split_factor,
            split_adjusted_close=orig[4] * split_factor,
            split_adjusted_volume=source_bar[5],
            split_adjustment_factor=split_factor,
        )
        for source_bar, orig, adj, split_factor in zip(
            source,
            price_only,
            adjusted,
            split_factors,
            strict=True,
        )
    ]


def _manifest_period(
    manifest: Mapping[str, object],
) -> tuple[date, date, str]:
    period = manifest.get("period")
    if not isinstance(period, Mapping):
        message = "snapshot manifest period is missing"
        raise EquityRunError(message)
    calendar = manifest.get("calendar")
    if not isinstance(calendar, Mapping) or not isinstance(calendar.get("mic"), str):
        message = "snapshot manifest calendar identity is missing"
        raise EquityRunError(message)
    return (
        date.fromisoformat(str(period["requested_from"])),
        date.fromisoformat(str(period["requested_to"])),
        str(calendar["mic"]),
    )


def _total_return_series(
    snapshot_dir: Path,
    manifest: Mapping[str, object],
) -> dict[date, float]:
    benchmarks = manifest.get("benchmarks")
    if not isinstance(benchmarks, Mapping):
        message = "snapshot manifest benchmarks are missing"
        raise EquityRunError(message)
    total_return = benchmarks.get("total_return")
    if not isinstance(total_return, Mapping):
        message = "snapshot manifest total-return benchmark is missing"
        raise EquityRunError(message)
    kind = total_return.get("kind")
    if kind == BenchmarkTotalReturnKind.INDEX_LEVEL.value:
        rows = _read_bars_csv(
            verified_artifact_path(
                snapshot_dir,
                _one_artifact(manifest, role="benchmark_total_return_bars"),
            )
        )
        return {row[0]: row[4] for row in rows}
    if kind != BenchmarkTotalReturnKind.ADJUSTED_SECURITY.value:
        message = f"unsupported total-return benchmark kind: {kind!r}"
        raise EquityRunError(message)
    rows = _read_bars_csv(
        verified_artifact_path(
            snapshot_dir,
            _one_artifact(manifest, role="benchmark_total_return_bars"),
        )
    )
    events = _merge_action_artifacts(
        snapshot_dir,
        (
            *manifest_artifacts(manifest, role="benchmark_total_return_splits"),
            *manifest_artifacts(manifest, role="benchmark_total_return_dividends"),
        ),
    )
    return {bar.session: bar.close for bar in _adjusted_daily_bars(rows, events)}


def _benchmark_identity(
    manifest: Mapping[str, object],
) -> tuple[str, BenchmarkTotalReturnKind]:
    benchmarks = manifest.get("benchmarks")
    if not isinstance(benchmarks, Mapping):
        message = "snapshot manifest benchmarks are missing"
        raise EquityRunError(message)
    total_return = benchmarks.get("total_return")
    if not isinstance(total_return, Mapping):
        message = "snapshot manifest total-return benchmark is missing"
        raise EquityRunError(message)
    symbol = total_return.get("symbol")
    kind = total_return.get("kind")
    if not isinstance(symbol, str) or not symbol.strip():
        message = "snapshot total-return benchmark symbol is missing"
        raise EquityRunError(message)
    try:
        return symbol, BenchmarkTotalReturnKind(str(kind))
    except ValueError as exc:
        message = f"unsupported total-return benchmark kind: {kind!r}"
        raise EquityRunError(message) from exc


def _official_session_schedule(
    snapshot_dir: Path,
    manifest: Mapping[str, object],
    *,
    start: date,
    end: date,
    calendar_mic: str,
) -> HistoricalMarketSessions:
    artifact = _one_artifact(manifest, role="official_sessions")
    path = verified_artifact_path(snapshot_dir, artifact)
    calendar = manifest.get("calendar")
    if not isinstance(calendar, Mapping):
        message = "snapshot manifest calendar identity is missing"
        raise EquityRunError(message)
    if calendar.get("artifact_sha256") != artifact.sha256:
        message = "snapshot calendar artifact identity differs"
        raise EquityRunError(message)
    try:
        schedule = read_historical_market_sessions(
            path,
            expected_content_sha256=artifact.sha256,
            expected_venue=calendar_mic,
            expected_from=start,
            expected_to=end,
            require_authoritative=False,
        )
    except SnapshotValidationError as exc:
        message = f"market-session artifact is invalid: {exc}"
        raise EquityRunError(message) from exc
    if (
        calendar.get("authority") != schedule.authority_payload()
        or calendar.get("confirmatory_eligible") is not schedule.confirmatory_eligible
        or calendar.get("coverage_complete") is not schedule.coverage_complete
    ):
        message = "snapshot calendar authority differs from its content"
        raise EquityRunError(message)
    return schedule


def _official_session_axis(
    snapshot_dir: Path,
    manifest: Mapping[str, object],
    *,
    start: date,
    end: date,
    calendar_mic: str,
) -> tuple[date, ...]:
    return _official_session_schedule(
        snapshot_dir,
        manifest,
        start=start,
        end=end,
        calendar_mic=calendar_mic,
    ).session_dates


def _require_exact_benchmark_sessions(
    *,
    label: str,
    observed: Sequence[date],
    expected: Sequence[date],
) -> None:
    observed_tuple = tuple(observed)
    expected_tuple = tuple(expected)
    if observed_tuple == expected_tuple:
        return
    missing = sorted(set(expected_tuple) - set(observed_tuple))
    extra = sorted(set(observed_tuple) - set(expected_tuple))
    message = (
        f"{label} does not cover the complete official-session axis: "
        f"missing={[item.isoformat() for item in missing]}, "
        f"extra={[item.isoformat() for item in extra]}"
    )
    raise EquityRunError(message)


def _security_series(
    snapshot_dir: Path,
    manifest: Mapping[str, object],
) -> tuple[
    dict[str, list[DailyBar]],
    dict[str, list[date]],
    dict[str, list[EquityCorporateAction]],
]:
    raw_dispositions = manifest.get("dispositions")
    registered_exclusion_symbols = {
        str(item.get("canonical_symbol"))
        for item in (raw_dispositions if isinstance(raw_dispositions, Sequence) else ())
        if isinstance(item, Mapping) and disposition_registered_panel_exclusion(item)
    }
    bars_by_symbol: dict[str, list[ArtifactRecord]] = defaultdict(list)
    split_by_symbol: dict[str, list[ArtifactRecord]] = defaultdict(list)
    dividend_by_symbol: dict[str, list[ArtifactRecord]] = defaultdict(list)
    for role in ("security_bars", "security_history_bars"):
        for artifact in manifest_artifacts(manifest, role=role):
            bars_by_symbol[_artifact_context(artifact, "canonical_symbol")].append(artifact)
    for role in ("security_splits", "security_history_splits"):
        for artifact in manifest_artifacts(manifest, role=role):
            split_by_symbol[_artifact_context(artifact, "canonical_symbol")].append(artifact)
    for role in ("security_dividends", "security_history_dividends"):
        for artifact in manifest_artifacts(manifest, role=role):
            dividend_by_symbol[_artifact_context(artifact, "canonical_symbol")].append(artifact)

    bars: dict[str, list[DailyBar]] = {}
    corporate_actions: dict[str, list[EquityCorporateAction]] = {}
    for symbol in sorted(bars_by_symbol):
        raw = _merge_bar_artifacts(snapshot_dir, bars_by_symbol[symbol])
        events, audit_rows = _lineaged_security_actions(
            snapshot_dir,
            symbol=symbol,
            artifacts=(*split_by_symbol[symbol], *dividend_by_symbol[symbol]),
        )
        try:
            bars[symbol] = _adjusted_daily_bars(raw, events)
        except AdjustmentError as exc:
            if symbol in registered_exclusion_symbols:
                # An owner-registered panel exclusion (e.g. a dividend on the
                # window's first session with no adjustment anchor). It drops
                # from the dataset with lineage instead of failing the load;
                # the panel never ranks it, so no factor value can change.
                logger.warning(
                    "Dropping registered-exclusion security %s from the snapshot dataset: %s",
                    symbol,
                    exc,
                )
                continue
            message = f"corporate-action adjustment failed for {symbol}: {exc}"
            raise EquityRunError(message) from exc
        corporate_actions[symbol] = audit_rows
    # Filing/factor evidence is acquired through its point-in-time boundary;
    # this price/action snapshot never fabricates earnings events.
    return bars, {symbol: [] for symbol in bars}, corporate_actions


def load_snapshot(
    snapshot_dir: Path,
    manifest_path: Path,
    *,
    admission_scope: SnapshotFactorClaimScope | None = None,
) -> tuple[EquityDataset, dict[str, object]]:
    """Verify and materialize the immutable objects selected by one exact manifest."""

    manifest = load_snapshot_manifest(snapshot_dir, manifest_path)
    scoped_admission_allowed = False
    if admission_scope is not None:
        try:
            evaluate_snapshot_factor_admission(manifest).decision(admission_scope)
        except SnapshotFactorAdmissionError as exc:
            raise EquityRunError(str(exc)) from exc
        scoped_admission_allowed = True
    if manifest.get("complete") is not True and not scoped_admission_allowed:
        message = "incomplete historical snapshot is ineligible for portfolio validation"
        raise EquityRunError(message)
    membership = _membership_from_artifact(snapshot_dir, manifest)
    benchmark_rows = _read_bars_csv(
        verified_artifact_path(
            snapshot_dir,
            _one_artifact(manifest, role="benchmark_price_bars"),
        )
    )
    vix_rows = _read_bars_csv(
        verified_artifact_path(
            snapshot_dir,
            _one_artifact(manifest, role="benchmark_volatility_bars"),
        )
    )
    start, end, calendar_mic = _manifest_period(manifest)
    official_schedule = _official_session_schedule(
        snapshot_dir,
        manifest,
        start=start,
        end=end,
        calendar_mic=calendar_mic,
    )
    sessions = list(official_schedule.session_dates)
    _require_exact_benchmark_sessions(
        label="benchmark price",
        observed=[row[0] for row in benchmark_rows],
        expected=sessions,
    )
    _require_exact_benchmark_sessions(
        label="benchmark volatility",
        observed=[row[0] for row in vix_rows],
        expected=sessions,
    )
    benchmark_tr = _total_return_series(snapshot_dir, manifest)
    _require_exact_benchmark_sessions(
        label="benchmark total return",
        observed=tuple(benchmark_tr),
        expected=sessions,
    )
    benchmark_symbol, benchmark_kind = _benchmark_identity(manifest)
    bars, earnings, corporate_actions = _security_series(snapshot_dir, manifest)
    corporate_actions = _executable_corporate_actions(corporate_actions, sessions=sessions)

    dataset = EquityDataset(
        sessions=sessions,
        bars=bars,
        benchmark_price={row[0]: row[4] for row in benchmark_rows},
        benchmark_total_return=benchmark_tr,
        vix={row[0]: row[4] for row in vix_rows},
        earnings=earnings,
        membership=membership,
        benchmark_total_return_kind=benchmark_kind.value,
        benchmark_total_return_symbol=benchmark_symbol,
        official_sessions={item.session_date: item for item in official_schedule.sessions},
        corporate_actions=corporate_actions,
    )
    return dataset, manifest


def _executable_corporate_actions(
    corporate_actions: dict[str, list[EquityCorporateAction]],
    *,
    sessions: Sequence[date],
) -> dict[str, list[EquityCorporateAction]]:
    """Drop cash dividends dated on a closed day, with logged lineage.

    An ex-date always falls on a trading session, so a vendor row dated on a
    weekend or an unscheduled closure is a data artifact (observed: IRM
    2024-03-17, a Sunday; ODFL 2018-12-05, the market-wide day of mourning).
    Simulators index actions by session, so such a row is already unreachable
    and silently ignored; this makes the disposition explicit and identical
    for every consumer. Price-adjustment events keep the vendor ex-date, and
    splits are never dropped because they rescale the whole price history.
    """
    axis = set(sessions)
    if not axis:
        return corporate_actions
    axis_start = min(axis)
    axis_end = max(axis)
    executable: dict[str, list[EquityCorporateAction]] = {}
    for symbol, actions in corporate_actions.items():
        kept: list[EquityCorporateAction] = []
        for action in actions:
            unreachable = (
                action.kind is EquityCorporateActionKind.CASH_DIVIDEND
                and axis_start <= action.effective_session <= axis_end
                and action.effective_session not in axis
            )
            if unreachable:
                logger.warning(
                    "Dropping %s cash dividend dated %s: not an official trading session",
                    symbol,
                    action.effective_session.isoformat(),
                )
                continue
            kept.append(action)
        executable[symbol] = kept
    return executable


def _month_end_sessions(
    sessions: Sequence[date],
    *,
    start: date,
    end: date,
) -> tuple[date, ...]:
    last_by_month: dict[tuple[int, int], date] = {}
    for session in sessions:
        if start <= session <= end:
            last_by_month[(session.year, session.month)] = session
    return tuple(last_by_month[key] for key in sorted(last_by_month))


def _decision_sessions(
    sessions: Sequence[date],
    *,
    start: date,
    end: date,
    offset: int,
) -> tuple[date, ...]:
    if offset not in {-1, 0, 1}:
        message = "decision session offset must be one of -1, 0, or 1"
        raise EquityRunError(message)
    canonical = tuple(sorted(set(sessions)))
    indices = {session: index for index, session in enumerate(canonical)}
    shifted: list[date] = []
    for month_end in _month_end_sessions(canonical, start=start, end=end):
        index = indices[month_end] + offset
        if index < 0 or index >= len(canonical):
            continue
        session = canonical[index]
        if start <= session <= end:
            shifted.append(session)
    return tuple(shifted)
