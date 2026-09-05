"""Immutable historical-snapshot artifact validation and serialization.

This internal boundary validates provider rows and writes content-addressed
reference evidence. Public acquisition contracts, state transitions, and
orchestration remain in ``equity_snapshot``.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dev_cli.validation.backtest.equity_snapshot_identity import (
    HistoricalAlias,
    HistoricalMarketSessions,
    MembershipInterval,
    ProviderSegment,
    SnapshotValidationError,
    build_diagnostic_market_sessions,
    membership_source_authority_summary,
    read_historical_market_sessions,
)
from dev_cli.validation.evidence import (
    ContentAddressedArtifact as ArtifactRecord,
)
from dev_cli.validation.evidence import (
    file_sha256 as sha256_file,
)
from dev_cli.validation.evidence import (
    store_content_object as _store_object,
)
from dev_cli.validation.evidence import (
    verified_content_path as verified_artifact_path,
)

if TYPE_CHECKING:
    from dev_cli.validation.backtest.equity_snapshot import (
        BenchmarkTotalReturnKind,
        DailyHistoricalProvider,
        SnapshotAcquisitionRequest,
        _ComponentOutcome,
    )

_LEDGER_SCHEMA = "vynmatrix.equity-acquisition-ledger.v1"


_LEDGER_FILENAME = "acquisition_ledger.jsonl"


_CSV_MEDIA_TYPE = "text/csv; charset=utf-8"


_JSON_MEDIA_TYPE = "application/json"


_ONE_DAY = "ONE_DAY"


def _validate_split_adjustment_request(
    *,
    start: date,
    end: date,
    split_adjustment_through: date,
    split_adjustment_basis_complete: object,
) -> None:
    if start > end:
        message = "historical acquisition start must not be after end"
        raise SnapshotValidationError(message)
    if split_adjustment_through < end:
        message = "split_adjustment_through cannot precede the last requested bar"
        raise SnapshotValidationError(message)
    if not isinstance(split_adjustment_basis_complete, bool):
        message = "split_adjustment_basis_complete must be boolean"
        raise SnapshotValidationError(message)


def _csv_content(header: Sequence[str], rows: Sequence[Sequence[object]]) -> bytes:
    sink = io.StringIO(newline="")
    writer = csv.writer(sink, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return sink.getvalue().encode("utf-8")


def _validated_candle_rows(
    provider: DailyHistoricalProvider,
    *,
    provider_symbol: str,
    start: date,
    end: date,
) -> tuple[Sequence[str], Sequence[Sequence[object]]]:
    raw_rows = provider.fetch_candle_rows(
        product_id=provider_symbol,
        instr_id=0,
        start_time=datetime.combine(start, time.min, tzinfo=UTC),
        end_time=datetime.combine(end, time.max, tzinfo=UTC),
        granularity=_ONE_DAY,
    )
    rows: list[list[object]] = []
    previous: date | None = None
    for raw in sorted(raw_rows, key=lambda item: item.ts):
        session = raw.ts.date()
        values = (float(raw.open), float(raw.high), float(raw.low), float(raw.close))
        if session < start or session > end:
            message = f"provider returned {provider_symbol} bar outside requested period"
            raise SnapshotValidationError(message)
        if previous is not None and session <= previous:
            message = f"provider returned duplicate or unordered {provider_symbol} sessions"
            raise SnapshotValidationError(message)
        if (
            not all(math.isfinite(value) and value > 0.0 for value in values)
            or values[1] < max(values[0], values[3])
            or values[2] > min(values[0], values[3])
        ):
            message = f"provider returned invalid OHLC for {provider_symbol} on {session}"
            raise SnapshotValidationError(message)
        split_adjusted_volume = None if raw.volume is None else float(raw.volume)
        if split_adjusted_volume is not None and (
            not math.isfinite(split_adjusted_volume) or split_adjusted_volume < 0.0
        ):
            message = (
                "provider returned invalid split-adjusted volume for "
                f"{provider_symbol} on {session}"
            )
            raise SnapshotValidationError(message)
        rows.append(
            [
                session.isoformat(),
                *values,
                "" if split_adjusted_volume is None else split_adjusted_volume,
            ]
        )
        previous = session
    return (
        "session",
        "open",
        "high",
        "low",
        "close",
        "split_adjusted_volume",
    ), rows


def _validated_split_rows(
    provider: DailyHistoricalProvider,
    *,
    provider_symbol: str,
    start: date,
    end: date,
) -> tuple[Sequence[str], Sequence[Sequence[object]]]:
    records = provider.fetch_splits(product_id=provider_symbol, start=start, end=end)
    rows: list[list[object]] = []
    for record in sorted(records, key=lambda item: item.ex_date):
        if record.ex_date < start or record.ex_date > end:
            message = f"provider returned {provider_symbol} split outside requested period"
            raise SnapshotValidationError(message)
        ratio = str(record.ratio)
        rows.append(["split", record.ex_date.isoformat(), ratio, "", ""])
    return ("action_type", "ex_date", "ratio", "amount", "currency"), rows


def _validated_dividend_rows(
    provider: DailyHistoricalProvider,
    *,
    provider_symbol: str,
    start: date,
    end: date,
) -> tuple[Sequence[str], Sequence[Sequence[object]]]:
    records = provider.fetch_dividends(product_id=provider_symbol, start=start, end=end)
    rows: list[list[object]] = []
    for record in sorted(records, key=lambda item: item.ex_date):
        if record.ex_date < start or record.ex_date > end:
            message = f"provider returned {provider_symbol} dividend outside requested period"
            raise SnapshotValidationError(message)
        rows.append(
            [
                "dividend",
                record.ex_date.isoformat(),
                "",
                str(record.amount),
                record.currency or "",
            ]
        )
    return ("action_type", "ex_date", "ratio", "amount", "currency"), rows


def _component_context(
    *,
    interval: MembershipInterval,
    segment: ProviderSegment,
) -> dict[str, Any]:
    return {
        "interval_id": interval.interval_id,
        "security_id": interval.security_id,
        "canonical_symbol": interval.canonical_symbol,
        "membership_source_ref": interval.source_ref,
        "provider_symbol": segment.provider_symbol,
        "requested_from": segment.start.isoformat(),
        "requested_to": segment.end.isoformat(),
        "alias_id": segment.alias_id,
        "alias_source_ref": segment.alias_source_ref,
    }


def _benchmark_context(
    role: str,
    symbol: str,
    request: SnapshotAcquisitionRequest,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "benchmark_role": role,
        "provider_symbol": symbol,
        "requested_from": request.start.isoformat(),
        "requested_to": request.end.isoformat(),
    }
    if role == "total_return":
        context["benchmark_kind"] = request.benchmark.total_return_kind.value
    return context


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unknown"


def _path_identity(path: Path, *, repo_root: Path) -> dict[str, str]:
    resolved = path.resolve()
    try:
        rendered = str(resolved.relative_to(repo_root.resolve()))
    except ValueError:
        rendered = str(resolved)
    return {"path": rendered, "sha256": sha256_file(resolved)}


@dataclass(frozen=True)
class _ReferenceArtifacts:
    membership: ArtifactRecord
    membership_evidence: tuple[ArtifactRecord, ...]
    aliases: ArtifactRecord | None
    calendar: ArtifactRecord
    market_sessions: HistoricalMarketSessions
    membership_source_authority: Mapping[str, object]


@dataclass(frozen=True)
class _BenchmarkOutcomes:
    price: _ComponentOutcome
    volatility: _ComponentOutcome
    total_return: _ComponentOutcome
    total_return_symbol: str
    total_return_kind: BenchmarkTotalReturnKind
    actions: tuple[_ComponentOutcome, ...]
    all_outcomes: tuple[_ComponentOutcome, ...]


def _market_sessions_for_request(
    request: SnapshotAcquisitionRequest,
) -> HistoricalMarketSessions:
    if request.official_sessions_path is None:
        return build_diagnostic_market_sessions(
            venue=request.calendar_mic,
            start=request.start,
            end=request.end,
            library_version=_package_version("exchange-calendars"),
        )
    assert request.official_sessions_sha256 is not None
    return read_historical_market_sessions(
        request.official_sessions_path,
        expected_content_sha256=request.official_sessions_sha256,
        expected_venue=request.calendar_mic,
        expected_from=request.start,
        expected_to=request.end,
    )


def _store_reference_artifacts(
    snapshot_dir: Path,
    *,
    request: SnapshotAcquisitionRequest,
    intervals: Sequence[MembershipInterval],
    aliases: Sequence[HistoricalAlias],
    market_sessions: HistoricalMarketSessions,
    repo_root: Path,
) -> _ReferenceArtifacts:
    for artifact in request.membership_evidence_artifacts:
        try:
            verified_artifact_path(snapshot_dir, artifact)
        except (OSError, TypeError, ValueError) as exc:
            message = f"membership evidence artifact differs: {artifact.path}"
            raise SnapshotValidationError(message) from exc
    membership_header = next(
        csv.reader(io.StringIO(request.membership_path.read_text(encoding="utf-8")))
    )
    membership_authority = membership_source_authority_summary(intervals)
    confirmatory_membership = bool(
        membership_authority["confirmatory_eligible"]
        and request.membership_authority_complete is not False
    )
    membership = _store_object(
        snapshot_dir,
        request.membership_path.read_bytes(),
        suffix=request.membership_path.suffix or ".csv",
        role="membership",
        media_type=_CSV_MEDIA_TYPE,
        row_count=len(intervals),
        context={
            "source_path": _path_identity(request.membership_path, repo_root=repo_root)["path"],
            "identity_scheme": (
                "source_backed_permanent_security_id"
                if request.permanent_identity_complete is True
                else (
                    "mixed_or_unresolved_security_identity"
                    if request.permanent_identity_complete is False
                    else (
                        "explicit_security_id"
                        if "security_id" in membership_header
                        else "membership_canonical_symbol_fallback"
                    )
                )
            ),
            "membership_lineage": (
                dict(request.membership_lineage) if request.membership_lineage is not None else None
            ),
            "permanent_identity_complete": request.permanent_identity_complete,
            "membership_authority_complete": request.membership_authority_complete,
            "limitation": (
                None
                if confirmatory_membership
                else (
                    "untyped or reconstructed membership input; diagnostic only "
                    "and not represented as official S&P constituent history"
                )
            ),
            "source_authority": membership_authority,
        },
    )
    alias_artifact = None
    if request.aliases_path is not None:
        alias_artifact = _store_object(
            snapshot_dir,
            request.aliases_path.read_bytes(),
            suffix=request.aliases_path.suffix or ".csv",
            role="historical_aliases",
            media_type=_CSV_MEDIA_TYPE,
            row_count=len(aliases),
            context={
                "source_path": _path_identity(request.aliases_path, repo_root=repo_root)["path"],
                "provider": request.provider.value,
            },
        )
    calendar = _store_object(
        snapshot_dir,
        market_sessions.content,
        suffix=".json",
        role="official_sessions",
        media_type=_JSON_MEDIA_TYPE,
        row_count=len(market_sessions.sessions),
        context={
            "authority": market_sessions.authority_payload(),
            "coverage_complete": market_sessions.coverage_complete,
            "coverage_from": market_sessions.coverage_from.isoformat(),
            "coverage_to": market_sessions.coverage_to.isoformat(),
            "limitation": (
                None
                if market_sessions.confirmatory_eligible
                else (
                    "exchange_calendars-generated session timing is diagnostic-only; "
                    "it is not an authoritative exchange or broker artifact"
                )
            ),
            "mic": request.calendar_mic,
            "session_timing": "stored aware UTC opens_at and closes_at",
        },
    )
    return _ReferenceArtifacts(
        membership=membership,
        membership_evidence=request.membership_evidence_artifacts,
        aliases=alias_artifact,
        calendar=calendar,
        market_sessions=market_sessions,
        membership_source_authority=membership_authority,
    )


def _alias_manifest_rows(aliases: Sequence[HistoricalAlias]) -> list[dict[str, Any]]:
    return [
        {
            "security_id": alias.security_id,
            "canonical_symbol": alias.canonical_symbol,
            "provider": alias.provider,
            "provider_symbol": alias.provider_symbol,
            "effective_from": alias.effective_from.isoformat(),
            "effective_to": alias.effective_to.isoformat() if alias.effective_to else None,
            "source_ref": alias.source_ref,
        }
        for alias in aliases
    ]


def _provider_segment_dispositions(
    outcomes: Sequence[_ComponentOutcome],
) -> list[dict[str, Any]]:
    """Return canonical price-request segments without action-tail duplication."""

    segments = {
        (
            str(outcome.context["requested_from"]),
            str(outcome.context["requested_to"]),
            str(outcome.context["provider_symbol"]),
            bool(outcome.context.get("history_only", False)),
            (
                str(outcome.context["alias_id"])
                if outcome.context.get("alias_id") is not None
                else None
            ),
            (
                str(outcome.context["alias_source_ref"])
                if outcome.context.get("alias_source_ref") is not None
                else None
            ),
        )
        for outcome in outcomes
        if outcome.component.endswith("_bars")
    }
    return [
        {
            "requested_from": requested_from,
            "requested_to": requested_to,
            "provider_symbol": provider_symbol,
            "history_only": history_only,
            "alias_id": alias_id,
            "alias_source_ref": alias_source_ref,
        }
        for (
            requested_from,
            requested_to,
            provider_symbol,
            history_only,
            alias_id,
            alias_source_ref,
        ) in sorted(segments)
    ]
