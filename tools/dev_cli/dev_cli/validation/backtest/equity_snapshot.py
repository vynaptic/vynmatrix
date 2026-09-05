"""Provider-neutral, immutable acquisition owned only by the offline validation CLI."""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from dev_cli.validation.backtest.equity_snapshot_adjustments import (
    read_bar_sessions,
    unanchored_dividend_dates,
)
from dev_cli.validation.backtest.equity_snapshot_admission import (
    evaluate_snapshot_factor_admission,
)
from dev_cli.validation.backtest.equity_snapshot_artifacts import (
    _CSV_MEDIA_TYPE,
    _JSON_MEDIA_TYPE,
    _LEDGER_FILENAME,
    _LEDGER_SCHEMA,
    _alias_manifest_rows,
    _benchmark_context,
    _BenchmarkOutcomes,
    _component_context,
    _csv_content,
    _market_sessions_for_request,
    _package_version,
    _path_identity,
    _provider_segment_dispositions,
    _store_reference_artifacts,
    _validate_split_adjustment_request,
    _validated_candle_rows,
    _validated_dividend_rows,
    _validated_split_rows,
)
from dev_cli.validation.backtest.equity_snapshot_identity import (
    HistoricalAlias,
    HistoricalMarketSessions,
    ListingEvidenceStatus,
    MarketSessionSourceKind,
    MembershipInterval,
    MembershipSourceAuthority,
    MembershipSourceKind,
    ProviderSegment,
    SnapshotValidationError,
    StrategyIdentity,
    ValidationProvider,
    build_strategy_identity,
    read_historical_aliases,
    read_membership_intervals,
    resolve_provider_segments,
)
from dev_cli.validation.backtest.equity_snapshot_identity import (
    interval_history_bounds as _interval_history_bounds,
)
from dev_cli.validation.backtest.equity_snapshot_identity import (
    interval_request_bounds as _interval_request_bounds,
)
from dev_cli.validation.backtest.equity_snapshot_identity import (
    require_snapshot_text as _require_nonblank,
)
from dev_cli.validation.backtest.equity_snapshot_identity import (
    validate_alias_membership_alignment as _validate_alias_membership_alignment,
)
from dev_cli.validation.backtest.equity_snapshot_identity import (
    validate_resolved_provider_symbol_assignments as _validate_resolved_provider_symbol_assignments,
)
from dev_cli.validation.evidence import (
    ContentAddressedArtifact as ArtifactRecord,
)
from dev_cli.validation.evidence import (
    content_manifest_artifacts as manifest_artifacts,
)
from dev_cli.validation.evidence import (
    evidence_sha256 as canonical_json_hash,
)
from dev_cli.validation.evidence import (
    file_sha256 as sha256_file,
)
from dev_cli.validation.evidence import (
    load_content_addressed_manifest,
    nonblank_string,
    sha256_digest,
    write_content_addressed_manifest,
)
from dev_cli.validation.evidence import (
    store_content_object as _store_object,
)
from dev_cli.validation.evidence import (
    verified_content_path as verified_artifact_path,
)
from lib_common.logging import get_logger

_MANIFEST_SCHEMA = "vynmatrix.equity-historical-snapshot.v4"
_MANIFEST_DIRECTORY = "manifests"
_LISTING_EVIDENCE_ARTIFACT_ROLE = "eodhd_security_fundamentals_general_response"
_LISTING_CORRECTION_MANIFEST_ROLE = "eodhd_membership_correction_evidence_manifest"
_STRUCTURAL_LISTING_WARMUP_OWNER = "lib_strategy.equity_market_factors.StructuralBreadthExclusion"


logger = get_logger(__name__)


class ComponentStatus(StrEnum):
    """Outcome of one independently resumable provider request."""

    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class DispositionStatus(StrEnum):
    """Final disposition for one requested membership interval."""

    RESOLVED = "resolved"
    EXCLUDED = "excluded"
    UNAVAILABLE = "unavailable"
    ALIASED = "aliased"
    PARTIAL = "partial"
    FAILED = "failed"


class BenchmarkTotalReturnKind(StrEnum):
    """Supported total-return benchmark construction contracts."""

    ADJUSTED_SECURITY = "adjusted_security"
    INDEX_LEVEL = "index_level"


class _CandleLike(Protocol):
    """Provider row whose ``volume`` is EODHD split-adjusted share volume."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None


class _SplitLike(Protocol):
    ex_date: date
    ratio: Any


class _DividendLike(Protocol):
    ex_date: date
    amount: Any
    currency: str | None


class DailyHistoricalProvider(Protocol):
    """Provider-neutral slice required by the historical snapshot acquirer."""

    source: str

    def fetch_candle_rows(
        self,
        *,
        product_id: str,
        instr_id: int,
        start_time: datetime,
        end_time: datetime,
        granularity: str,
        broker_instrument_type: str | None = None,
    ) -> Sequence[_CandleLike]: ...

    def fetch_splits(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> Sequence[_SplitLike]: ...

    def fetch_dividends(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> Sequence[_DividendLike]: ...


@dataclass(frozen=True)
class BenchmarkSpec:
    """Historical benchmark/reference symbols bound into the snapshot."""

    price_symbol: str
    total_return_symbol: str
    total_return_kind: BenchmarkTotalReturnKind
    volatility_symbol: str

    def __post_init__(self) -> None:
        for field_name in ("price_symbol", "total_return_symbol", "volatility_symbol"):
            _require_nonblank(getattr(self, field_name), field=field_name)
        if not isinstance(self.total_return_kind, BenchmarkTotalReturnKind):
            message = "total_return_kind must be a BenchmarkTotalReturnKind"
            raise SnapshotValidationError(message)


@dataclass(frozen=True)
class SnapshotAcquisitionRequest:
    """Validated inputs for one historical snapshot acquisition."""

    provider: ValidationProvider
    provider_product: str
    dataset_version: str
    entitlement_scope: str
    entitlement_owner_user_id: str
    start: date
    end: date
    calendar_mic: str
    membership_path: Path
    aliases_path: Path | None
    benchmark: BenchmarkSpec
    strategy: StrategyIdentity
    git_revision: str
    adjustment_version: str
    split_adjustment_through: date
    split_adjustment_basis_complete: bool = False
    official_sessions_path: Path | None = None
    official_sessions_sha256: str | None = None
    security_history_sessions: int = 0
    # Every membership-eligible official session must be present. A lower
    # threshold is useful only for diagnostic acquisition statistics; it must
    # not turn unexplained halts, mergers, or delistings into complete evidence.
    minimum_session_coverage: float = 1.0
    membership_evidence_artifacts: tuple[ArtifactRecord, ...] = ()
    membership_lineage: Mapping[str, Any] | None = None
    permanent_identity_complete: bool | None = None
    membership_authority_complete: bool | None = None

    def __post_init__(self) -> None:
        _validate_split_adjustment_request(
            start=self.start,
            end=self.end,
            split_adjustment_through=self.split_adjustment_through,
            split_adjustment_basis_complete=self.split_adjustment_basis_complete,
        )
        for field_name in (
            "provider_product",
            "dataset_version",
            "entitlement_scope",
            "entitlement_owner_user_id",
            "calendar_mic",
            "git_revision",
            "adjustment_version",
        ):
            _require_nonblank(str(getattr(self, field_name)), field=field_name)
        if not self.membership_path.is_file():
            message = f"membership file does not exist: {self.membership_path}"
            raise SnapshotValidationError(message)
        if self.aliases_path is not None and not self.aliases_path.is_file():
            message = f"historical alias file does not exist: {self.aliases_path}"
            raise SnapshotValidationError(message)
        if (self.official_sessions_path is None) != (self.official_sessions_sha256 is None):
            message = (
                "official_sessions_path and official_sessions_sha256 must be configured together"
            )
            raise SnapshotValidationError(message)
        if self.official_sessions_path is not None and not self.official_sessions_path.is_file():
            message = f"official market-session file does not exist: {self.official_sessions_path}"
            raise SnapshotValidationError(message)
        if self.official_sessions_sha256 is not None:
            try:
                expected_sha256 = sha256_digest(
                    self.official_sessions_sha256,
                    field="official_sessions_sha256",
                )
            except (TypeError, ValueError) as exc:
                raise SnapshotValidationError(str(exc)) from exc
            if (
                self.official_sessions_path is not None
                and sha256_file(self.official_sessions_path) != expected_sha256
            ):
                message = "official market-session file differs from its configured content SHA-256"
                raise SnapshotValidationError(message)
        if (
            isinstance(self.security_history_sessions, bool)
            or not isinstance(self.security_history_sessions, int)
            or self.security_history_sessions < 0
        ):
            message = "security_history_sessions must be a non-negative integer"
            raise SnapshotValidationError(message)
        if not 0.0 < self.minimum_session_coverage <= 1.0:
            message = "minimum_session_coverage must be in (0, 1]"
            raise SnapshotValidationError(message)
        if self.permanent_identity_complete is not None and not isinstance(
            self.permanent_identity_complete,
            bool,
        ):
            message = "permanent_identity_complete must be a boolean or null"
            raise SnapshotValidationError(message)
        if self.membership_authority_complete is not None and not isinstance(
            self.membership_authority_complete,
            bool,
        ):
            message = "membership_authority_complete must be a boolean or null"
            raise SnapshotValidationError(message)
        if any(
            not artifact.role.startswith("eodhd_")
            for artifact in self.membership_evidence_artifacts
        ):
            message = "membership evidence artifacts must use an eodhd_ role"
            raise SnapshotValidationError(message)

    @property
    def request_digest(self) -> str:
        return canonical_json_hash(
            {
                "provider": self.provider.value,
                "provider_product": self.provider_product,
                "dataset_version": self.dataset_version,
                "entitlement_scope": self.entitlement_scope,
                "entitlement_owner_user_id": self.entitlement_owner_user_id,
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
                "calendar_mic": self.calendar_mic,
                "membership_sha256": sha256_file(self.membership_path),
                "aliases_sha256": (
                    sha256_file(self.aliases_path) if self.aliases_path is not None else None
                ),
                "membership_evidence": [
                    artifact.as_manifest() for artifact in self.membership_evidence_artifacts
                ],
                "membership_lineage": (
                    dict(self.membership_lineage) if self.membership_lineage is not None else None
                ),
                "permanent_identity_complete": self.permanent_identity_complete,
                "membership_authority_complete": self.membership_authority_complete,
                "official_sessions_sha256": (
                    sha256_file(self.official_sessions_path)
                    if self.official_sessions_path is not None
                    else None
                ),
                "diagnostic_calendar_library_version": (
                    None
                    if self.official_sessions_path is not None
                    else _package_version("exchange-calendars")
                ),
                "benchmark": asdict(self.benchmark),
                "strategy": asdict(self.strategy),
                "git_revision": self.git_revision,
                "adjustment_version": self.adjustment_version,
                "split_adjustment_through": self.split_adjustment_through.isoformat(),
                "split_adjustment_basis_complete": self.split_adjustment_basis_complete,
                "security_history_sessions": self.security_history_sessions,
                "minimum_session_coverage": self.minimum_session_coverage,
            }
        )


@dataclass(frozen=True)
class SnapshotAcquisitionResult:
    """Final immutable manifest identity and completeness state."""

    manifest_path: Path
    manifest_sha256: str
    complete: bool
    disposition_counts: Mapping[str, int]


@dataclass(frozen=True)
class _ComponentOutcome:
    request_id: str
    component: str
    status: ComponentStatus
    artifact: ArtifactRecord | None
    context: Mapping[str, Any]
    retrieved_at: str
    error_type: str | None = None

    def as_ledger_payload(self) -> dict[str, Any]:
        return {
            "schema": _LEDGER_SCHEMA,
            "request_id": self.request_id,
            "component": self.component,
            "status": self.status.value,
            "artifact": self.artifact.as_manifest() if self.artifact else None,
            "context": dict(self.context),
            "retrieved_at": self.retrieved_at,
            "error_type": self.error_type,
        }


class _AcquisitionLedger:
    """Append-only request journal used to resume verified provider components."""

    def __init__(self, snapshot_dir: Path) -> None:
        self.path = snapshot_dir / _LEDGER_FILENAME
        self._snapshot_dir = snapshot_dir
        self._entries = self._read_entries()
        self._attempts = Counter(
            str(entry["request_id"]) for entry in self._entries if "request_id" in entry
        )

    def _read_entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError as exc:
                    message = f"invalid acquisition ledger JSON at line {line_number}"
                    raise SnapshotValidationError(message) from exc
                if not isinstance(envelope, Mapping):
                    message = f"invalid acquisition ledger envelope at line {line_number}"
                    raise SnapshotValidationError(message)
                payload = envelope.get("entry")
                if not isinstance(payload, Mapping):
                    message = f"missing acquisition ledger entry at line {line_number}"
                    raise SnapshotValidationError(message)
                expected = sha256_digest(
                    envelope.get("entry_sha256"),
                    field="entry_sha256",
                )
                if canonical_json_hash(payload) != expected:
                    message = f"acquisition ledger hash differs at line {line_number}"
                    raise SnapshotValidationError(message)
                if payload.get("schema") != _LEDGER_SCHEMA:
                    message = f"unsupported acquisition ledger schema at line {line_number}"
                    raise SnapshotValidationError(message)
                entries.append(dict(payload))
        return entries

    def resumable(self, request_id: str) -> _ComponentOutcome | None:
        for entry in reversed(self._entries):
            if entry.get("request_id") != request_id:
                continue
            status = ComponentStatus(nonblank_string(entry.get("status"), field="status"))
            if status is ComponentStatus.FAILED:
                return None
            raw_artifact = entry.get("artifact")
            artifact = (
                ArtifactRecord.from_manifest(raw_artifact)
                if isinstance(raw_artifact, Mapping)
                else None
            )
            if status is ComponentStatus.VERIFIED:
                if artifact is None:
                    message = f"verified ledger request lacks artifact: {request_id}"
                    raise SnapshotValidationError(message)
                verified_artifact_path(self._snapshot_dir, artifact)
            context = entry.get("context")
            if not isinstance(context, Mapping):
                message = f"ledger request context is invalid: {request_id}"
                raise SnapshotValidationError(message)
            return _ComponentOutcome(
                request_id=request_id,
                component=nonblank_string(entry.get("component"), field="component"),
                status=status,
                artifact=artifact,
                context=dict(context),
                retrieved_at=nonblank_string(
                    entry.get("retrieved_at"),
                    field="retrieved_at",
                ),
                error_type=(
                    str(entry["error_type"]) if isinstance(entry.get("error_type"), str) else None
                ),
            )
        return None

    def append(self, outcome: _ComponentOutcome) -> None:
        payload = outcome.as_ledger_payload()
        payload["attempt"] = self._attempts[outcome.request_id] + 1
        envelope = {
            "entry_sha256": canonical_json_hash(payload),
            "entry": payload,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(envelope, allow_nan=False, sort_keys=True))
            sink.write("\n")
            sink.flush()
            os.fsync(sink.fileno())
        self._entries.append(payload)
        self._attempts[outcome.request_id] += 1

    @property
    def entries(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(dict(entry) for entry in self._entries)


def _unique_artifacts(outcomes: Sequence[_ComponentOutcome]) -> list[ArtifactRecord]:
    by_request: dict[str, ArtifactRecord] = {}
    for outcome in outcomes:
        if outcome.status is ComponentStatus.VERIFIED and outcome.artifact is not None:
            by_request[outcome.request_id] = outcome.artifact
    return [by_request[key] for key in sorted(by_request)]


def _request_id(
    request: SnapshotAcquisitionRequest,
    *,
    component: str,
    context: Mapping[str, Any],
) -> str:
    return canonical_json_hash(
        {
            "provider": request.provider.value,
            "provider_product": request.provider_product,
            "dataset_version": request.dataset_version,
            "component": component,
            "context": dict(context),
        }
    )


def _acquire_component(
    *,
    request: SnapshotAcquisitionRequest,
    ledger: _AcquisitionLedger,
    snapshot_dir: Path,
    component: str,
    context: Mapping[str, Any],
    producer: Callable[[], tuple[Sequence[str], Sequence[Sequence[object]]]],
    provider_errors: tuple[type[Exception], ...],
    unavailable_when_empty: bool,
    clock: Callable[[], datetime],
) -> _ComponentOutcome:
    request_id = _request_id(request, component=component, context=context)
    prior = ledger.resumable(request_id)
    if prior is not None:
        return prior
    retrieved_at = clock().astimezone(UTC).isoformat()
    try:
        header, rows = producer()
    except provider_errors as exc:
        outcome = _ComponentOutcome(
            request_id=request_id,
            component=component,
            status=ComponentStatus.FAILED,
            artifact=None,
            context=dict(context),
            retrieved_at=retrieved_at,
            error_type=type(exc).__name__,
        )
        ledger.append(outcome)
        return outcome
    if unavailable_when_empty and not rows:
        outcome = _ComponentOutcome(
            request_id=request_id,
            component=component,
            status=ComponentStatus.UNAVAILABLE,
            artifact=None,
            context=dict(context),
            retrieved_at=retrieved_at,
        )
        ledger.append(outcome)
        return outcome
    artifact = _store_object(
        snapshot_dir,
        _csv_content(header, rows),
        suffix=".csv",
        role=component,
        media_type=_CSV_MEDIA_TYPE,
        row_count=len(rows),
        context=context,
    )
    outcome = _ComponentOutcome(
        request_id=request_id,
        component=component,
        status=ComponentStatus.VERIFIED,
        artifact=artifact,
        context=dict(context),
        retrieved_at=retrieved_at,
    )
    ledger.append(outcome)
    return outcome


def _acquire_interval_segments(
    *,
    request: SnapshotAcquisitionRequest,
    provider: DailyHistoricalProvider,
    interval: MembershipInterval,
    segments: Sequence[ProviderSegment],
    ledger: _AcquisitionLedger,
    snapshot_dir: Path,
    provider_errors: tuple[type[Exception], ...],
    clock: Callable[[], datetime],
    history_only: bool,
) -> tuple[_ComponentOutcome, ...]:
    component_prefix = "security_history" if history_only else "security"
    outcomes: list[_ComponentOutcome] = []
    for segment in segments:
        context = {
            **_component_context(interval=interval, segment=segment),
            "history_only": history_only,
            "membership_eligible": not history_only,
        }
        outcomes.append(
            _acquire_component(
                request=request,
                ledger=ledger,
                snapshot_dir=snapshot_dir,
                component=f"{component_prefix}_bars",
                context=context,
                producer=partial(
                    _validated_candle_rows,
                    provider,
                    provider_symbol=segment.provider_symbol,
                    start=segment.start,
                    end=segment.end,
                ),
                provider_errors=provider_errors,
                unavailable_when_empty=True,
                clock=clock,
            )
        )
        outcomes.append(
            _acquire_component(
                request=request,
                ledger=ledger,
                snapshot_dir=snapshot_dir,
                component=f"{component_prefix}_splits",
                context={
                    **context,
                    "price_segment_to": segment.end.isoformat(),
                    "requested_to": request.split_adjustment_through.isoformat(),
                    "split_adjustment_through": (request.split_adjustment_through.isoformat()),
                    "volume_semantics": "provider_split_adjusted_integer",
                },
                producer=partial(
                    _validated_split_rows,
                    provider,
                    provider_symbol=segment.provider_symbol,
                    start=segment.start,
                    end=request.split_adjustment_through,
                ),
                provider_errors=provider_errors,
                unavailable_when_empty=False,
                clock=clock,
            )
        )
        outcomes.append(
            _acquire_component(
                request=request,
                ledger=ledger,
                snapshot_dir=snapshot_dir,
                component=f"{component_prefix}_dividends",
                context=context,
                producer=partial(
                    _validated_dividend_rows,
                    provider,
                    provider_symbol=segment.provider_symbol,
                    start=segment.start,
                    end=segment.end,
                ),
                provider_errors=provider_errors,
                unavailable_when_empty=False,
                clock=clock,
            )
        )
    return tuple(outcomes)


def _acquire_membership_interval(
    *,
    request: SnapshotAcquisitionRequest,
    provider: DailyHistoricalProvider,
    interval: MembershipInterval,
    aliases: Sequence[HistoricalAlias],
    ledger: _AcquisitionLedger,
    snapshot_dir: Path,
    market_sessions: HistoricalMarketSessions,
    provider_errors: tuple[type[Exception], ...],
    clock: Callable[[], datetime],
) -> tuple[_ComponentOutcome, ...]:
    bounds = _interval_request_bounds(interval, request)
    if bounds is None:
        return ()
    overlap_start, overlap_end = bounds
    segments = resolve_provider_segments(
        security_id=interval.security_id,
        canonical_symbol=interval.canonical_symbol,
        start=overlap_start,
        end=overlap_end,
        aliases=aliases,
    )
    history_bounds = _interval_history_bounds(
        interval,
        request,
        session_axis=market_sessions.session_dates,
    )
    history_segments: tuple[ProviderSegment, ...] = ()
    if history_bounds is not None:
        history_segments = resolve_provider_segments(
            security_id=interval.security_id,
            canonical_symbol=interval.canonical_symbol,
            start=history_bounds[0],
            end=history_bounds[1],
            aliases=aliases,
        )
        unsourced = tuple(
            segment
            for segment in history_segments
            if segment.alias_id is None or segment.alias_source_ref is None
        )
        if unsourced:
            # Every pre-membership segment requires a dated sourced provider
            # alias.  A prefix before a reviewed non-stitch identity edge has
            # none by construction -- the successor is a new security, so no
            # earlier bar belongs to it -- and is therefore not fetched.  The
            # security keeps only its membership-window bars and stays
            # panel-ineligible until it accumulates them, which can lower its
            # eligibility but can never fabricate history or admit look-ahead.
            # The omission is auditable: the disposition then carries no
            # security_history_* component at all.
            logger.info(
                "deferring unsourced pre-membership history for %s/%s: %s..%s",
                interval.security_id,
                interval.canonical_symbol,
                history_bounds[0],
                history_bounds[1],
            )
            history_bounds = None
            history_segments = ()
    outcomes = list(
        _acquire_interval_segments(
            request=request,
            provider=provider,
            interval=interval,
            segments=segments,
            ledger=ledger,
            snapshot_dir=snapshot_dir,
            provider_errors=provider_errors,
            clock=clock,
            history_only=False,
        )
    )
    if history_bounds is None:
        return tuple(outcomes)
    outcomes.extend(
        _acquire_interval_segments(
            request=request,
            provider=provider,
            interval=interval,
            segments=history_segments,
            ledger=ledger,
            snapshot_dir=snapshot_dir,
            provider_errors=provider_errors,
            clock=clock,
            history_only=True,
        )
    )
    return tuple(outcomes)


def _acquire_benchmark_bars(
    *,
    request: SnapshotAcquisitionRequest,
    provider: DailyHistoricalProvider,
    role: str,
    symbol: str,
    ledger: _AcquisitionLedger,
    snapshot_dir: Path,
    market_sessions: HistoricalMarketSessions,
    provider_errors: tuple[type[Exception], ...],
    clock: Callable[[], datetime],
) -> _ComponentOutcome:
    context = _benchmark_context(role, symbol, request)
    outcome = _acquire_component(
        request=request,
        ledger=ledger,
        snapshot_dir=snapshot_dir,
        component=f"benchmark_{role}_bars",
        context=context,
        producer=partial(
            _validated_candle_rows,
            provider,
            provider_symbol=symbol,
            start=request.start,
            end=request.end,
        ),
        provider_errors=provider_errors,
        unavailable_when_empty=True,
        clock=clock,
    )
    if outcome.status is not ComponentStatus.VERIFIED or outcome.artifact is None:
        return outcome
    observed = read_bar_sessions((verified_artifact_path(snapshot_dir, outcome.artifact),))
    expected = set(market_sessions.session_dates)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        message = (
            f"benchmark {role} {symbol} does not cover the complete official-session "
            f"axis: missing={[item.isoformat() for item in missing]}, "
            f"extra={[item.isoformat() for item in extra]}"
        )
        raise SnapshotValidationError(message)
    return outcome


def _verified_listing_evidence_artifact(
    *,
    request: SnapshotAcquisitionRequest,
    interval: MembershipInterval,
) -> ArtifactRecord | None:
    """Return exact raw listing evidence when its identity and lineage are intact."""

    evidence = interval.listing_evidence
    if (
        evidence is None
        or evidence.status is not ListingEvidenceStatus.BOUND
        or evidence.effective_listing_date is None
        or evidence.artifact_role != _LISTING_EVIDENCE_ARTIFACT_ROLE
    ):
        return None
    matches = tuple(
        artifact
        for artifact in request.membership_evidence_artifacts
        if artifact.role == evidence.artifact_role and artifact.sha256 == evidence.artifact_sha256
    )
    if len(matches) != 1:
        return None
    artifact = matches[0]
    context = artifact.context
    requested_symbol = str(context.get("requested_symbol") or "").upper()
    expected_context = {
        "content_sha256": evidence.artifact_sha256,
        "endpoint": evidence.endpoint,
        "provider": evidence.provider.value,
        "retrieved_at": evidence.retrieved_at.isoformat(),
    }
    if any(context.get(field) != value for field, value in expected_context.items()):
        return None
    if requested_symbol.removesuffix(".US") != evidence.provider_symbol:
        return None
    reviewed_manifest_sha256 = evidence.reviewed_listing_correction_manifest_sha256
    if reviewed_manifest_sha256 is not None:
        correction_manifests = tuple(
            item
            for item in request.membership_evidence_artifacts
            if item.role == _LISTING_CORRECTION_MANIFEST_ROLE
            and item.sha256 == reviewed_manifest_sha256
        )
        if len(correction_manifests) != 1:
            return None
    return artifact


def _structural_listing_warmup_candidate(  # noqa: PLR0911 - fail-closed evidence gates
    *,
    request: SnapshotAcquisitionRequest,
    interval: MembershipInterval,
    market_sessions: HistoricalMarketSessions,
    history_expected: Sequence[date],
    history_observed: set[date],
    history_missing: Sequence[date],
    history_outcomes: Sequence[_ComponentOutcome],
    membership_missing: Sequence[date],
    membership_actions_complete: bool,
    dividend_anchors_complete: bool,
) -> dict[str, Any] | None:
    """Classify only an exact pre-listing prefix for later panel-level proof.

    This does not waive price completeness.  It binds a candidate that the
    synchronized panel builder must reconstruct as a ``StructuralBreadthExclusion``
    for each decision window, including its 1% portfolio bound.
    """

    if (
        not history_expected
        or not history_missing
        or membership_missing
        or not membership_actions_complete
        or not dividend_anchors_complete
    ):
        return None
    evidence = interval.listing_evidence
    artifact = _verified_listing_evidence_artifact(request=request, interval=interval)
    if evidence is None or evidence.effective_listing_date is None or artifact is None:
        return None
    listing_date = evidence.effective_listing_date
    listing_session = next(
        (session for session in market_sessions.session_dates if session >= listing_date),
        None,
    )
    if listing_session is None:
        return None
    expected_missing = tuple(session for session in history_expected if session < listing_session)
    expected_observed = tuple(session for session in history_expected if session >= listing_session)
    if (
        not expected_missing
        or tuple(history_missing) != expected_missing
        or tuple(sorted(history_observed)) != expected_observed
    ):
        return None
    bar_outcomes = tuple(
        outcome for outcome in history_outcomes if outcome.component == "security_history_bars"
    )
    action_outcomes = tuple(
        outcome
        for outcome in history_outcomes
        if outcome.component
        in {
            "security_history_splits",
            "security_history_dividends",
        }
    )
    if (
        not bar_outcomes
        or ComponentStatus.FAILED in {outcome.status for outcome in bar_outcomes}
        or not action_outcomes
        or any(outcome.status is not ComponentStatus.VERIFIED for outcome in action_outcomes)
    ):
        return None
    evidence_symbol = evidence.provider_symbol
    history_symbols = {
        str(outcome.context.get("provider_symbol") or "").strip().upper().removesuffix(".US")
        for outcome in history_outcomes
    }
    if history_symbols != {evidence_symbol}:
        return None
    return {
        "kind": "structural_listing_warmup_candidate",
        "validation_owner": _STRUCTURAL_LISTING_WARMUP_OWNER,
        "listing_evidence_id": evidence.evidence_id,
        "listing_evidence_sha256": artifact.sha256,
        "first_official_listing_session": listing_session.isoformat(),
        "required_history_sessions": len(history_expected),
        "observed_history_sessions": len(history_observed),
        "missing_history_sessions": len(history_missing),
        "panel_requirements": (
            "revalidate the exact rolling official-session window, complete "
            "post-listing observations, immutable price hashes, and the frozen "
            "maximum structural-exclusion fraction"
        ),
    }


def _build_dispositions(
    *,
    request: SnapshotAcquisitionRequest,
    intervals: Sequence[MembershipInterval],
    outcomes_by_interval: Mapping[str, Sequence[_ComponentOutcome]],
    snapshot_dir: Path,
    market_sessions: HistoricalMarketSessions,
) -> list[dict[str, Any]]:
    dispositions: list[dict[str, Any]] = []
    for interval in intervals:
        interval_end = interval.effective_to or request.end
        overlap_start = max(request.start, interval.effective_from)
        overlap_end = min(request.end, interval_end)
        base = {
            "interval_id": interval.interval_id,
            "security_id": interval.security_id,
            "canonical_symbol": interval.canonical_symbol,
            "effective_from": interval.effective_from.isoformat(),
            "effective_to": interval.effective_to.isoformat() if interval.effective_to else None,
            "source_ref": interval.source_ref,
        }
        if overlap_start > overlap_end:
            dispositions.append(
                {
                    **base,
                    "status": DispositionStatus.EXCLUDED.value,
                    "reason": "outside_requested_period",
                    "requested_from": None,
                    "requested_to": None,
                    "expected_sessions": 0,
                    "observed_sessions": 0,
                    "coverage_ratio": None,
                    "missing_sessions": [],
                    "provider_symbols": [],
                    "alias_ids": [],
                    "provider_segments": [],
                    "component_requests": [],
                    "panel_materialization_eligible": True,
                    "deferred_panel_validation": None,
                }
            )
            continue

        relevant = list(outcomes_by_interval.get(interval.interval_id, ()))
        bar_outcomes = [outcome for outcome in relevant if outcome.component == "security_bars"]
        action_outcomes = [
            outcome
            for outcome in relevant
            if outcome.component in {"security_splits", "security_dividends"}
        ]
        history_bounds = _interval_history_bounds(
            interval,
            request,
            session_axis=market_sessions.session_dates,
        )
        history_bar_outcomes = [
            outcome for outcome in relevant if outcome.component == "security_history_bars"
        ]
        history_action_outcomes = [
            outcome
            for outcome in relevant
            if outcome.component in {"security_history_splits", "security_history_dividends"}
        ]
        observed: set[date] = set()
        for outcome in bar_outcomes:
            if outcome.status is ComponentStatus.VERIFIED and outcome.artifact is not None:
                observed.update(
                    session
                    for session in read_bar_sessions(
                        (verified_artifact_path(snapshot_dir, outcome.artifact),)
                    )
                    if overlap_start <= session <= overlap_end
                )
        expected = [
            session
            for session in market_sessions.session_dates
            if overlap_start <= session <= overlap_end
        ]
        expected_set = set(expected)
        unexpected = sorted(observed - expected_set)
        if unexpected:
            message = (
                f"{interval.canonical_symbol} contains sessions outside "
                f"{request.calendar_mic}: {unexpected[:3]}"
            )
            raise SnapshotValidationError(message)
        missing = sorted(expected_set - observed)
        coverage = len(observed) / len(expected) if expected else None
        bar_statuses = {outcome.status for outcome in bar_outcomes}
        actions_complete = bool(action_outcomes) and all(
            outcome.status is ComponentStatus.VERIFIED for outcome in action_outcomes
        )
        coverage_complete = coverage is not None and coverage >= request.minimum_session_coverage
        history_expected = (
            [
                session
                for session in market_sessions.session_dates
                if history_bounds[0] <= session <= history_bounds[1]
            ]
            if history_bounds is not None
            else []
        )
        history_observed: set[date] = set()
        for outcome in history_bar_outcomes:
            if outcome.status is ComponentStatus.VERIFIED and outcome.artifact is not None:
                history_observed.update(
                    read_bar_sessions((verified_artifact_path(snapshot_dir, outcome.artifact),))
                )
        history_missing = sorted(set(history_expected) - history_observed)
        history_complete = history_bounds is None or (
            bool(history_bar_outcomes)
            and not history_missing
            and bool(history_action_outcomes)
            and all(
                outcome.status is ComponentStatus.VERIFIED
                for outcome in (
                    *history_bar_outcomes,
                    *history_action_outcomes,
                )
            )
        )
        dividend_anchors_complete = not unanchored_dividend_dates(
            bar_paths=tuple(
                verified_artifact_path(snapshot_dir, outcome.artifact)
                for outcome in (*bar_outcomes, *history_bar_outcomes)
                if outcome.status is ComponentStatus.VERIFIED and outcome.artifact is not None
            ),
            action_paths=tuple(
                verified_artifact_path(snapshot_dir, outcome.artifact)
                for outcome in (*action_outcomes, *history_action_outcomes)
                if outcome.status is ComponentStatus.VERIFIED and outcome.artifact is not None
            ),
        )
        deferred_panel_validation = _structural_listing_warmup_candidate(
            request=request,
            interval=interval,
            market_sessions=market_sessions,
            history_expected=history_expected,
            history_observed=history_observed,
            history_missing=history_missing,
            history_outcomes=(*history_bar_outcomes, *history_action_outcomes),
            membership_missing=missing,
            membership_actions_complete=actions_complete,
            dividend_anchors_complete=dividend_anchors_complete,
        )
        if ComponentStatus.FAILED in bar_statuses and observed:
            status = DispositionStatus.PARTIAL
            reason = "bar_segment_failed"
        elif ComponentStatus.FAILED in bar_statuses:
            status = DispositionStatus.FAILED
            reason = "bar_acquisition_failed"
        elif not observed and ComponentStatus.UNAVAILABLE in bar_statuses:
            status = DispositionStatus.UNAVAILABLE
            reason = "provider_returned_no_history"
        elif not history_complete:
            status = DispositionStatus.PARTIAL
            reason = (
                "registered_price_history_structural_listing_warmup"
                if deferred_panel_validation is not None
                else "registered_price_history_incomplete"
            )
        elif not coverage_complete or not actions_complete or not dividend_anchors_complete:
            status = DispositionStatus.PARTIAL
            reason = (
                "dividend_adjustment_anchor_missing"
                if not dividend_anchors_complete
                else (
                    "corporate_actions_incomplete"
                    if coverage_complete and not actions_complete
                    else "session_coverage_below_policy"
                )
            )
        else:
            alias_ids = {
                str(outcome.context["alias_id"])
                for outcome in relevant
                if outcome.context.get("alias_id")
            }
            status = DispositionStatus.ALIASED if alias_ids else DispositionStatus.RESOLVED
            reason = "coverage_policy_satisfied"
        panel_materialization_eligible = (
            status
            in {
                DispositionStatus.RESOLVED,
                DispositionStatus.ALIASED,
                DispositionStatus.EXCLUDED,
            }
            or deferred_panel_validation is not None
        )
        dispositions.append(
            {
                **base,
                "status": status.value,
                "reason": reason,
                "requested_from": overlap_start.isoformat(),
                "requested_to": overlap_end.isoformat(),
                "expected_sessions": len(expected),
                "observed_sessions": len(observed),
                "coverage_ratio": coverage,
                "first_observed_session": min(observed).isoformat() if observed else None,
                "last_observed_session": max(observed).isoformat() if observed else None,
                "missing_sessions": [session.isoformat() for session in missing],
                "history_requested_from": (
                    history_bounds[0].isoformat() if history_bounds is not None else None
                ),
                "history_requested_to": (
                    history_bounds[1].isoformat() if history_bounds is not None else None
                ),
                "history_expected_sessions": len(history_expected),
                "history_observed_sessions": len(history_observed),
                "history_missing_sessions": [session.isoformat() for session in history_missing],
                "provider_symbols": sorted(
                    {
                        str(outcome.context["provider_symbol"])
                        for outcome in relevant
                        if outcome.context.get("provider_symbol")
                    }
                ),
                "alias_ids": sorted(
                    {
                        str(outcome.context["alias_id"])
                        for outcome in relevant
                        if outcome.context.get("alias_id")
                    }
                ),
                "provider_segments": _provider_segment_dispositions(relevant),
                "panel_materialization_eligible": panel_materialization_eligible,
                "deferred_panel_validation": deferred_panel_validation,
                "component_requests": [
                    {
                        "request_id": outcome.request_id,
                        "component": outcome.component,
                        "status": outcome.status.value,
                        "error_type": outcome.error_type,
                    }
                    for outcome in relevant
                ],
            }
        )
    return dispositions


def _acquire_all_securities(
    *,
    request: SnapshotAcquisitionRequest,
    provider: DailyHistoricalProvider,
    intervals: Sequence[MembershipInterval],
    aliases: Sequence[HistoricalAlias],
    ledger: _AcquisitionLedger,
    snapshot_dir: Path,
    market_sessions: HistoricalMarketSessions,
    provider_errors: tuple[type[Exception], ...],
    clock: Callable[[], datetime],
) -> tuple[list[_ComponentOutcome], dict[str, list[_ComponentOutcome]]]:
    all_outcomes: list[_ComponentOutcome] = []
    by_interval: dict[str, list[_ComponentOutcome]] = defaultdict(list)
    for interval in intervals:
        outcomes = _acquire_membership_interval(
            request=request,
            provider=provider,
            interval=interval,
            aliases=aliases,
            ledger=ledger,
            snapshot_dir=snapshot_dir,
            market_sessions=market_sessions,
            provider_errors=provider_errors,
            clock=clock,
        )
        all_outcomes.extend(outcomes)
        by_interval[interval.interval_id].extend(outcomes)
    return all_outcomes, by_interval


def _acquire_benchmarks(
    *,
    request: SnapshotAcquisitionRequest,
    provider: DailyHistoricalProvider,
    ledger: _AcquisitionLedger,
    snapshot_dir: Path,
    market_sessions: HistoricalMarketSessions,
    provider_errors: tuple[type[Exception], ...],
    clock: Callable[[], datetime],
) -> _BenchmarkOutcomes:
    price = _acquire_benchmark_bars(
        request=request,
        provider=provider,
        role="price",
        symbol=request.benchmark.price_symbol,
        ledger=ledger,
        snapshot_dir=snapshot_dir,
        market_sessions=market_sessions,
        provider_errors=provider_errors,
        clock=clock,
    )
    volatility = _acquire_benchmark_bars(
        request=request,
        provider=provider,
        role="volatility",
        symbol=request.benchmark.volatility_symbol,
        ledger=ledger,
        snapshot_dir=snapshot_dir,
        market_sessions=market_sessions,
        provider_errors=provider_errors,
        clock=clock,
    )
    total_return = _acquire_benchmark_bars(
        request=request,
        provider=provider,
        role="total_return",
        symbol=request.benchmark.total_return_symbol,
        ledger=ledger,
        snapshot_dir=snapshot_dir,
        market_sessions=market_sessions,
        provider_errors=provider_errors,
        clock=clock,
    )
    outcomes = [price, volatility, total_return]
    actions: tuple[_ComponentOutcome, ...] = ()
    if request.benchmark.total_return_kind is BenchmarkTotalReturnKind.ADJUSTED_SECURITY:
        context = {
            **_benchmark_context(
                "total_return",
                request.benchmark.total_return_symbol,
                request,
            ),
            "security_id": "benchmark:total_return",
            "canonical_symbol": request.benchmark.total_return_symbol,
            "alias_id": None,
            "alias_source_ref": None,
        }
        splits = _acquire_component(
            request=request,
            ledger=ledger,
            snapshot_dir=snapshot_dir,
            component="benchmark_total_return_splits",
            context={
                **context,
                "price_segment_to": request.end.isoformat(),
                "requested_to": request.split_adjustment_through.isoformat(),
                "split_adjustment_through": request.split_adjustment_through.isoformat(),
                "volume_semantics": "provider_split_adjusted_integer",
            },
            producer=partial(
                _validated_split_rows,
                provider,
                provider_symbol=request.benchmark.total_return_symbol,
                start=request.start,
                end=request.split_adjustment_through,
            ),
            provider_errors=provider_errors,
            unavailable_when_empty=False,
            clock=clock,
        )
        dividends = _acquire_component(
            request=request,
            ledger=ledger,
            snapshot_dir=snapshot_dir,
            component="benchmark_total_return_dividends",
            context=context,
            producer=partial(
                _validated_dividend_rows,
                provider,
                provider_symbol=request.benchmark.total_return_symbol,
                start=request.start,
                end=request.end,
            ),
            provider_errors=provider_errors,
            unavailable_when_empty=False,
            clock=clock,
        )
        actions = (splits, dividends)
        outcomes.extend((splits, dividends))
    return _BenchmarkOutcomes(
        price=price,
        volatility=volatility,
        total_return=total_return,
        total_return_symbol=request.benchmark.total_return_symbol,
        total_return_kind=request.benchmark.total_return_kind,
        actions=actions,
        all_outcomes=tuple(outcomes),
    )


def acquire_historical_snapshot(
    snapshot_dir: Path,
    *,
    request: SnapshotAcquisitionRequest,
    provider: DailyHistoricalProvider,
    provider_errors: tuple[type[Exception], ...],
    repo_root: Path,
    acquisition_code_paths: Sequence[Path],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SnapshotAcquisitionResult:
    """Acquire every requested security, continue on vendor misses, and finalize evidence."""

    if provider.source.strip().lower() != request.provider.value:
        message = (
            f"provider adapter source {provider.source!r} does not match "
            f"validated provider {request.provider.value!r}"
        )
        raise SnapshotValidationError(message)
    acquisition_date = clock().astimezone(UTC).date()
    if acquisition_date != request.split_adjustment_through:
        message = (
            "split-adjustment basis must be pinned to the exact acquisition UTC date; "
            "start a new content-addressed snapshot rather than resuming across dates"
        )
        raise SnapshotValidationError(message)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    intervals = read_membership_intervals(request.membership_path)
    aliases = read_historical_aliases(request.aliases_path, provider=request.provider)
    membership_symbols = {
        (interval.security_id, interval.canonical_symbol) for interval in intervals
    }
    unknown_aliases = sorted(
        {
            (alias.security_id, alias.canonical_symbol)
            for alias in aliases
            if (alias.security_id, alias.canonical_symbol) not in membership_symbols
        }
    )
    if unknown_aliases:
        message = (
            f"historical aliases reference securities absent from membership: {unknown_aliases}"
        )
        raise SnapshotValidationError(message)
    _validate_alias_membership_alignment(
        intervals=intervals,
        aliases=aliases,
    )
    _validate_resolved_provider_symbol_assignments(
        request=request,
        intervals=intervals,
        aliases=aliases,
    )
    market_sessions = _market_sessions_for_request(request)

    references = _store_reference_artifacts(
        snapshot_dir,
        request=request,
        intervals=intervals,
        aliases=aliases,
        market_sessions=market_sessions,
        repo_root=repo_root,
    )

    ledger = _AcquisitionLedger(snapshot_dir)
    all_outcomes, outcomes_by_interval = _acquire_all_securities(
        request=request,
        provider=provider,
        intervals=intervals,
        aliases=aliases,
        ledger=ledger,
        snapshot_dir=snapshot_dir,
        market_sessions=market_sessions,
        provider_errors=provider_errors,
        clock=clock,
    )
    benchmarks = _acquire_benchmarks(
        request=request,
        provider=provider,
        ledger=ledger,
        snapshot_dir=snapshot_dir,
        market_sessions=market_sessions,
        provider_errors=provider_errors,
        clock=clock,
    )
    all_outcomes.extend(benchmarks.all_outcomes)

    dispositions = _build_dispositions(
        request=request,
        intervals=intervals,
        outcomes_by_interval=outcomes_by_interval,
        snapshot_dir=snapshot_dir,
        market_sessions=market_sessions,
    )
    disposition_counts = Counter(str(item["status"]) for item in dispositions)
    accepted_dispositions = {
        DispositionStatus.RESOLVED.value,
        DispositionStatus.ALIASED.value,
        DispositionStatus.EXCLUDED.value,
    }
    securities_complete = all(
        str(disposition["status"]) in accepted_dispositions for disposition in dispositions
    )
    securities_panel_materialization_eligible = all(
        disposition.get("panel_materialization_eligible") is True for disposition in dispositions
    )
    benchmarks_complete = all(
        outcome.status is ComponentStatus.VERIFIED
        for outcome in (
            benchmarks.price,
            benchmarks.volatility,
            benchmarks.total_return,
        )
    ) and all(outcome.status is ComponentStatus.VERIFIED for outcome in benchmarks.actions)
    split_tail_outcomes = tuple(
        outcome for outcome in all_outcomes if outcome.component.endswith("_splits")
    )
    split_coordinate_reconstruction_complete = bool(split_tail_outcomes) and all(
        outcome.status is ComponentStatus.VERIFIED
        and outcome.artifact is not None
        and outcome.context.get("split_adjustment_through")
        == request.split_adjustment_through.isoformat()
        and outcome.context.get("requested_to") == request.split_adjustment_through.isoformat()
        and outcome.context.get("volume_semantics") == "provider_split_adjusted_integer"
        for outcome in split_tail_outcomes
    )
    benchmark_adjustment_anchors_complete = not unanchored_dividend_dates(
        bar_paths=(
            (verified_artifact_path(snapshot_dir, benchmarks.total_return.artifact),)
            if benchmarks.total_return.artifact is not None
            else ()
        ),
        action_paths=tuple(
            verified_artifact_path(snapshot_dir, outcome.artifact)
            for outcome in benchmarks.actions
            if outcome.artifact is not None
        ),
    )
    complete = (
        securities_complete
        and benchmarks_complete
        and benchmark_adjustment_anchors_complete
        and request.split_adjustment_basis_complete
        and request.permanent_identity_complete is True
        and request.membership_authority_complete is True
    )
    panel_materialization_eligible = (
        securities_panel_materialization_eligible
        and benchmarks_complete
        and benchmark_adjustment_anchors_complete
        and split_coordinate_reconstruction_complete
        and request.permanent_identity_complete is True
        and request.membership_authority_complete is True
    )

    evidence_rows = [dict(entry) for entry in ledger.entries]
    acquisition_evidence = _store_object(
        snapshot_dir,
        json.dumps(
            evidence_rows,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
        suffix=".json",
        role="acquisition_evidence",
        media_type=_JSON_MEDIA_TYPE,
        row_count=len(evidence_rows),
        context={"ledger_schema": _LEDGER_SCHEMA},
    )
    artifacts = [
        references.membership,
        references.calendar,
        acquisition_evidence,
        *references.membership_evidence,
        *([references.aliases] if references.aliases is not None else []),
        *_unique_artifacts(all_outcomes),
    ]
    code_identity = [
        _path_identity(path, repo_root=repo_root) for path in sorted(acquisition_code_paths)
    ]
    payload: dict[str, Any] = {
        "schema": _MANIFEST_SCHEMA,
        "acquisition_id": request.request_digest,
        "complete": complete,
        "panel_materialization_eligible": panel_materialization_eligible,
        "panel_materialization_policy": {
            "accepted_deferred_disposition": ("identity-bound structural listing warm-up only"),
            "accepted_diagnostic_global_incompleteness": (
                "explicit permanent-identity and membership-authority completeness only"
            ),
            "campaign_eligibility": (
                "complete remains false until every interval and corporate-action "
                "requirement is resolved"
            ),
            "panel_validation_owner": _STRUCTURAL_LISTING_WARMUP_OWNER,
            "terminal_tail_policy": (
                "never inferred from missing prices; requires source-backed membership "
                "and terminal-event evidence"
            ),
            "unexplained_gap_policy": "fail_closed",
        },
        "finalized_at": clock().astimezone(UTC).isoformat(),
        "provider": {
            "name": request.provider.value,
            "product": request.provider_product,
            "dataset_version": request.dataset_version,
            "scope": "historical_validation_only",
            "entitlement_scope": request.entitlement_scope,
            "entitlement_owner_user_id": request.entitlement_owner_user_id,
            "credential_persisted": False,
            "endpoints": {
                "daily_prices": "/api/eod/{provider_symbol}",
                "splits": "/api/splits/{provider_symbol}",
                "dividends": "/api/div/{provider_symbol}",
                "index_membership_snapshots": (
                    "/api/v1.1/fundamentals/{index_symbol}?historical=1&from={from}&to={to}"
                ),
                "index_membership_ticker_history": (
                    "/api/fundamentals/{index_symbol}?filter=HistoricalTickerComponents"
                ),
                "security_id_mapping": "/api/id-mapping?filter[symbol]={provider_symbol}",
                "us_symbol_directory": "/api/exchange-symbol-list/US",
            },
        },
        "evidence_scope": [
            "raw_daily_ohlc_and_provider_split_adjusted_volume",
            "splits",
            "cash_dividends",
            "membership",
            "membership_raw_provider_responses",
            "permanent_security_identity",
            "historical_aliases",
            "pre_membership_price_history",
            "official_sessions",
            "price_benchmark",
            "total_return_benchmark",
            "volatility_reference",
        ],
        "period": {
            "requested_from": request.start.isoformat(),
            "requested_to": request.end.isoformat(),
        },
        "security_history_policy": {
            "registered_sessions": request.security_history_sessions,
            "membership_eligibility": (
                "history-only observations never extend membership eligibility"
            ),
            "pre_membership_identity": (
                "every pre-membership segment requires a dated sourced provider alias"
            ),
        },
        "timestamp_semantics": {
            "daily_price_observation": (
                "provider date interpreted as official exchange session label; "
                "availability is the stored session close from the pinned "
                "market-session artifact"
            ),
            "corporate_actions": "provider ex-date; split ratio is new shares / old shares",
            "execution_eligibility": ("no earlier than the stored open of a later market session"),
            "source_volume": ("EODHD split-adjusted shares; never labelled raw as-traded volume"),
        },
        "adjustment_policy": {
            "price_input": "raw OHLC; provider adjusted_close ignored",
            "volume_input": "provider split-adjusted share volume",
            "raw_volume_reconstruction": "not performed; provider rounding is undocumented",
            "liquidity_notional": (
                "split-adjusted close times provider split-adjusted volume in one pinned "
                "split basis, with a one-reported-share conservative quantization haircut"
            ),
            "split_adjustment_through": request.split_adjustment_through.isoformat(),
            "split_coordinate_reconstruction_complete": (split_coordinate_reconstruction_complete),
            "split_adjustment_basis_complete": request.split_adjustment_basis_complete,
            "split_adjustment_basis_limitation": (
                None
                if request.split_adjustment_basis_complete
                else (
                    "EODHD documents integer split-adjusted volume but does not publish "
                    "its adjustment horizon, revision identity, or rounding convention; "
                    "the snapshot is diagnostic-only and is not admissible for "
                    "confirmatory or headline performance claims"
                )
            ),
            "diagnostic_authorization": (
                "complete split tail plus raw OHLC transformed into the provider's pinned "
                "split coordinate; volume remains provider-reported and modeled costs retain "
                "modeled_from_observed_daily_bars lineage"
            ),
            "confirmatory_authorization": (
                "requires split_adjustment_basis_complete in addition to deterministic "
                "coordinate reconstruction"
            ),
            "actions": "provider splits and cash dividends acquired separately",
            "calculation_owner": "lib_data.adjustments.apply_adjustments",
            "calculation_version": request.adjustment_version,
            "missing_action_component": "interval disposition is partial and snapshot incomplete",
        },
        "missing_data_policy": {
            "minimum_session_coverage": request.minimum_session_coverage,
            "failed_security_policy": "never silently remove; preserve interval disposition",
            "empty_price_response": DispositionStatus.UNAVAILABLE.value,
            "provider_error": DispositionStatus.FAILED.value,
            "partial_component": DispositionStatus.PARTIAL.value,
        },
        "calendar": {
            "mic": request.calendar_mic,
            "artifact_sha256": references.calendar.sha256,
            "authority": references.market_sessions.authority_payload(),
            "confirmatory_eligible": (references.market_sessions.confirmatory_eligible),
            "coverage_complete": references.market_sessions.coverage_complete,
            "coverage_from": references.market_sessions.coverage_from.isoformat(),
            "coverage_to": references.market_sessions.coverage_to.isoformat(),
            "limitation": (
                None
                if references.market_sessions.confirmatory_eligible
                else (
                    "library-generated session dates and UTC timing are "
                    "diagnostic-only and cannot support confirmatory or headline claims"
                )
            ),
            "timing_semantics": (
                "every session persists exact aware UTC opens_at and closes_at; "
                "short sessions are encoded by their actual close"
            ),
        },
        "membership": {
            "artifact_sha256": references.membership.sha256,
            "interval_digest": canonical_json_hash(
                [
                    {
                        "interval_id": interval.interval_id,
                        "security_id": interval.security_id,
                        "canonical_symbol": interval.canonical_symbol,
                        "effective_from": interval.effective_from.isoformat(),
                        "effective_to": (
                            interval.effective_to.isoformat() if interval.effective_to else None
                        ),
                        "source_ref": interval.source_ref,
                        "source_authority": (
                            interval.source_authority.to_payload()
                            if interval.source_authority is not None
                            else None
                        ),
                    }
                    for interval in intervals
                ]
            ),
            "interval_count": len(intervals),
            "source_authority": references.membership_source_authority,
            "evidence_artifact_sha256s": sorted(
                artifact.sha256 for artifact in references.membership_evidence
            ),
            "identity_scheme": references.membership.context.get("identity_scheme"),
            "lineage": (
                dict(request.membership_lineage) if request.membership_lineage is not None else None
            ),
            "permanent_identity_complete": request.permanent_identity_complete,
            "membership_authority_complete": request.membership_authority_complete,
            "source_limitation": (
                None
                if references.membership_source_authority["confirmatory_eligible"]
                else (
                    "untyped or reconstructed input is diagnostic-only and must "
                    "not be relabelled as official S&P constituent history"
                )
            ),
        },
        "aliases": {
            "configured": references.aliases is not None,
            "artifact_sha256": references.aliases.sha256 if references.aliases else None,
            "alias_digest": canonical_json_hash(_alias_manifest_rows(aliases)),
            "alias_count": len(aliases),
            "canonical_fallback": (
                "used only where no dated provider alias is configured; vendor coverage "
                "is represented by the resulting disposition"
            ),
        },
        "benchmarks": {
            "price": {
                "requested_symbol": request.benchmark.price_symbol,
                "request_id": benchmarks.price.request_id,
                "status": benchmarks.price.status.value,
            },
            "volatility": {
                "requested_symbol": request.benchmark.volatility_symbol,
                "request_id": benchmarks.volatility.request_id,
                "status": benchmarks.volatility.status.value,
            },
            "total_return": {
                "requested_symbol": request.benchmark.total_return_symbol,
                "request_id": benchmarks.total_return.request_id,
                "status": benchmarks.total_return.status.value,
                "symbol": benchmarks.total_return_symbol,
                "kind": benchmarks.total_return_kind.value,
                "adjustment_anchor_complete": benchmark_adjustment_anchors_complete,
            },
        },
        "strategy": asdict(request.strategy),
        "code_identity": {
            "git_revision": request.git_revision,
            "files": code_identity,
            "digest": canonical_json_hash(code_identity),
        },
        "configuration_identity": request.request_digest,
        "disposition_counts": dict(sorted(disposition_counts.items())),
        "dispositions": dispositions,
        "artifacts": [
            artifact.as_manifest()
            for artifact in sorted(artifacts, key=lambda item: (item.role, item.path))
        ],
    }
    payload["factor_materialization_admission"] = evaluate_snapshot_factor_admission(
        payload
    ).to_manifest()
    manifest_path, manifest_sha256 = write_content_addressed_manifest(
        snapshot_dir,
        payload,
        manifest_directory=_MANIFEST_DIRECTORY,
    )
    return SnapshotAcquisitionResult(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        complete=complete,
        disposition_counts=dict(sorted(disposition_counts.items())),
    )


def load_snapshot_manifest(
    snapshot_dir: Path,
    manifest_path: Path,
    *,
    verify_artifacts: bool = True,
) -> dict[str, Any]:
    """Load one explicit content-addressed manifest and verify its full identity."""
    try:
        return load_content_addressed_manifest(
            snapshot_dir,
            manifest_path,
            schema=_MANIFEST_SCHEMA,
            manifest_directory=_MANIFEST_DIRECTORY,
            verify_artifacts=verify_artifacts,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise SnapshotValidationError(str(exc)) from exc


__all__ = (
    "ArtifactRecord",
    "BenchmarkSpec",
    "BenchmarkTotalReturnKind",
    "DailyHistoricalProvider",
    "DispositionStatus",
    "HistoricalAlias",
    "HistoricalMarketSessions",
    "MarketSessionSourceKind",
    "MembershipInterval",
    "MembershipSourceAuthority",
    "MembershipSourceKind",
    "SnapshotAcquisitionRequest",
    "SnapshotAcquisitionResult",
    "SnapshotValidationError",
    "StrategyIdentity",
    "ValidationProvider",
    "acquire_historical_snapshot",
    "build_strategy_identity",
    "load_snapshot_manifest",
    "manifest_artifacts",
    "read_historical_aliases",
    "read_membership_intervals",
    "resolve_provider_segments",
    "verified_artifact_path",
)
