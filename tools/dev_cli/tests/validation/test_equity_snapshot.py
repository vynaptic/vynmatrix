"""Protocol tests for immutable historical-equity acquisition evidence."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from dev_cli.validation.backtest.equity_portfolio import (
    BacktestResult,
    EquityCorporateActionKind,
    PortfolioConfig,
    TotalReturnAdjustedCorporateActionPolicy,
)
from dev_cli.validation.backtest.equity_run import (
    _publish_backtest_result,
    load_snapshot,
    run_backtest,
)
from dev_cli.validation.backtest.equity_run_cli import build_argument_parser
from dev_cli.validation.backtest.equity_run_inputs import EquityRunError, _read_bars_csv
from dev_cli.validation.backtest.equity_snapshot import (
    BenchmarkSpec,
    BenchmarkTotalReturnKind,
    DispositionStatus,
    MembershipSourceKind,
    SnapshotAcquisitionRequest,
    SnapshotValidationError,
    StrategyIdentity,
    ValidationProvider,
    acquire_historical_snapshot,
    load_snapshot_manifest,
    manifest_artifacts,
    read_historical_aliases,
    read_membership_intervals,
    resolve_provider_segments,
    verified_artifact_path,
)
from dev_cli.validation.backtest.equity_snapshot_admission import (
    SnapshotFactorClaimScope,
    evaluate_snapshot_factor_admission,
)
from dev_cli.validation.backtest.equity_snapshot_identity import (
    HISTORICAL_MARKET_SESSIONS_SCHEMA,
    MarketSessionSourceKind,
    membership_source_authority_summary,
    read_historical_market_sessions,
)
from dev_cli.validation.evidence import (
    ContentAddressedArtifact,
    canonical_json_bytes,
    file_sha256,
    store_content_object,
)
from lib_data.adjustments import AdjustmentEvent, apply_adjustments
from lib_data.sessions import sessions_between

_START = date(2024, 1, 2)
_END = date(2024, 1, 5)
_CLOCK = datetime(2024, 1, 8, 12, 0, tzinfo=UTC)


class ProviderFixtureError(RuntimeError):
    """Deterministic protocol failure; never contains credentials."""


@dataclass(frozen=True)
class _Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None


@dataclass(frozen=True)
class _Split:
    ex_date: date
    ratio: str


@dataclass(frozen=True)
class _Dividend:
    ex_date: date
    amount: str
    currency: str | None


class _HistoricalProviderFixture:
    source = "eodhd"

    def __init__(
        self,
        *,
        failing_symbols: set[str] | None = None,
        empty_symbols: set[str] | None = None,
        sparse_symbols: set[str] | None = None,
        with_actions: bool = False,
    ) -> None:
        self.failing_symbols = failing_symbols or set()
        self.empty_symbols = empty_symbols or set()
        self.sparse_symbols = sparse_symbols or set()
        self.with_actions = with_actions
        self.calls: Counter[tuple[str, str]] = Counter()
        self.request_ranges: list[tuple[str, str, date, date]] = []

    def fetch_candle_rows(
        self,
        *,
        product_id: str,
        instr_id: int,
        start_time: datetime,
        end_time: datetime,
        granularity: str,
        broker_instrument_type: str | None = None,
    ) -> list[_Candle]:
        del instr_id, granularity, broker_instrument_type
        self.calls[("bars", product_id)] += 1
        self.request_ranges.append(("bars", product_id, start_time.date(), end_time.date()))
        if product_id in self.failing_symbols:
            raise ProviderFixtureError
        if product_id in self.empty_symbols:
            return []
        rows = [
            _Candle(
                ts=datetime.combine(session, time(21), tzinfo=UTC),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1_000_000.0,
            )
            for session in sessions_between("XNYS", start_time.date(), end_time.date())
        ]
        return rows[:-1] if product_id in self.sparse_symbols else rows

    def fetch_splits(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> list[object]:
        self.calls[("splits", product_id)] += 1
        self.request_ranges.append(("splits", product_id, start, end))
        if self.with_actions and product_id == "AAA":
            return [_Split(ex_date=date(2024, 1, 4), ratio="2")]
        return []

    def fetch_dividends(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> list[object]:
        self.calls[("dividends", product_id)] += 1
        self.request_ranges.append(("dividends", product_id, start, end))
        if self.with_actions and product_id == "AAA":
            return [
                _Dividend(
                    ex_date=date(2024, 1, 5),
                    amount="1",
                    currency="USD",
                )
            ]
        return []


class _UnanchoredDividendProvider(_HistoricalProviderFixture):
    def fetch_dividends(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> list[object]:
        rows = super().fetch_dividends(product_id=product_id, start=start, end=end)
        if product_id == "AAA":
            return [_Dividend(ex_date=start, amount="1", currency="USD")]
        return rows


class _PostPeriodSplitProvider(_HistoricalProviderFixture):
    """Provider fixture with a split after the requested price period."""

    def fetch_splits(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> list[object]:
        rows = super().fetch_splits(product_id=product_id, start=start, end=end)
        if product_id == "AAA" and start <= _CLOCK.date() <= end:
            return [*rows, _Split(ex_date=_CLOCK.date(), ratio="4")]
        return rows


class _ListingDateProvider(_HistoricalProviderFixture):
    """Real-history shape: no bars before listing, with optional unexplained gaps."""

    def __init__(
        self,
        *,
        listing_date: date,
        omitted_sessions: frozenset[date] = frozenset(),
    ) -> None:
        super().__init__()
        self.listing_date = listing_date
        self.omitted_sessions = omitted_sessions

    def fetch_candle_rows(
        self,
        *,
        product_id: str,
        instr_id: int,
        start_time: datetime,
        end_time: datetime,
        granularity: str,
        broker_instrument_type: str | None = None,
    ) -> list[_Candle]:
        rows = super().fetch_candle_rows(
            product_id=product_id,
            instr_id=instr_id,
            start_time=start_time,
            end_time=end_time,
            granularity=granularity,
            broker_instrument_type=broker_instrument_type,
        )
        if product_id != "AAA":
            return rows
        return [
            row
            for row in rows
            if row.ts.date() >= self.listing_date and row.ts.date() not in self.omitted_sessions
        ]


def _write_membership(path: Path, symbols: tuple[str, ...]) -> None:
    rows = [
        "ticker,effective_from,effective_to,source_ref,notes",
        *[
            f"{symbol},{_START.isoformat()},{_END.isoformat()},"
            "protocol-source,protocol contract row"
            for symbol in symbols
        ],
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_aliases(path: Path) -> None:
    path.write_text(
        (
            "security_id,canonical_symbol,provider,provider_symbol,"
            "effective_from,effective_to,source_ref\n"
            f"AAA,AAA,eodhd,AAA-OLD,{_START.isoformat()},{_END.isoformat()},"
            "provider-symbol-directory\n"
        ),
        encoding="utf-8",
    )


def _write_listing_evidence_membership(
    *,
    membership_path: Path,
    aliases_path: Path,
    evidence: ContentAddressedArtifact,
    listing_date: date,
    reviewed_listing_date: date | None = None,
    reviewed_listing_correction_id: str | None = None,
    reviewed_listing_correction_manifest_sha256: str | None = None,
) -> None:
    artifact = evidence
    reviewed_values = (
        reviewed_listing_date.isoformat() if reviewed_listing_date else "",
        reviewed_listing_correction_id or "",
        reviewed_listing_correction_manifest_sha256 or "",
    )
    membership_path.write_text(
        (
            "security_id,ticker,effective_from,effective_to,source_ref,notes,"
            "listing_evidence_artifact_role,listing_evidence_artifact_sha256,"
            "listing_evidence_endpoint,listing_evidence_provider,"
            "listing_evidence_provider_symbol,listing_evidence_retrieved_at,"
            "listing_evidence_status,listing_general_cik,listing_general_code,"
            "listing_general_isin,listing_identity_binding,vendor_reported_ipo_date,"
            "reviewed_listing_date,reviewed_listing_correction_id,"
            "reviewed_listing_correction_manifest_sha256\n"
            f"figi:BBG000000001,AAA,2024-01-04,2024-01-05,licensed-membership,,"
            f"{artifact.role},{artifact.sha256},{artifact.context['endpoint']},eodhd,"
            f"AAA,{artifact.context['retrieved_at']},bound,,AAA,,"
            f"exact_provider_symbol_code,{listing_date.isoformat()},"
            f"{','.join(reviewed_values)}\n"
        ),
        encoding="utf-8",
    )
    aliases_path.write_text(
        (
            "security_id,canonical_symbol,provider,provider_symbol,"
            "effective_from,effective_to,source_ref\n"
            "figi:BBG000000001,AAA,eodhd,AAA,2024-01-02,2024-01-05,"
            "identity-bound-provider-symbol\n"
        ),
        encoding="utf-8",
    )


def _listing_evidence_artifact(
    snapshot: Path,
    *,
    listing_date: date,
) -> ContentAddressedArtifact:
    content = canonical_json_bytes(
        {
            "General": {
                "Code": "AAA",
                "IPODate": listing_date.isoformat(),
            }
        }
    )
    content_sha256 = hashlib.sha256(content).hexdigest()
    endpoint = "/api/v1.1/fundamentals/AAA.US?filter=General&fmt=json"
    return store_content_object(
        snapshot,
        content,
        suffix=".json",
        role="eodhd_security_fundamentals_general_response",
        media_type="application/json",
        row_count=1,
        context={
            "content_sha256": content_sha256,
            "endpoint": endpoint,
            "provider": "eodhd",
            "requested_symbol": "AAA.US",
            "retrieved_at": _CLOCK.isoformat(),
        },
    )


def _write_official_sessions(
    path: Path,
    *,
    start: date = _START,
    end: date = _END,
    sessions: list[dict[str, str]] | None = None,
) -> str:
    rows = sessions or [
        {
            "closes_at": datetime.combine(session, time(21), tzinfo=UTC).isoformat(),
            "opens_at": datetime.combine(session, time(14, 30), tzinfo=UTC).isoformat(),
            "session_date": session.isoformat(),
        }
        for session in sessions_between("XNYS", start, end)
    ]
    path.write_bytes(
        canonical_json_bytes(
            {
                "coverage_complete": True,
                "coverage_from": start.isoformat(),
                "coverage_to": end.isoformat(),
                "dataset": "NYSE historical calendar",
                "dataset_version": "2024",
                "entitlement_scope": "public",
                "provider": "nyse",
                "retrieved_at": "2026-01-02T12:00:00+00:00",
                "schema": HISTORICAL_MARKET_SESSIONS_SCHEMA,
                "sessions": rows,
                "source_kind": MarketSessionSourceKind.EXCHANGE.value,
                "source_reference": "https://www.nyse.com/markets/hours-calendars",
                "source_revision": "nyse-calendar-2024",
                "timestamp_semantics": "published regular-session UTC open and close",
                "venue": "XNYS",
            }
        )
    )
    return file_sha256(path)


def _request(
    membership: Path,
    *,
    aliases: Path | None = None,
    official_sessions: Path | None = None,
) -> SnapshotAcquisitionRequest:
    return SnapshotAcquisitionRequest(
        provider=ValidationProvider.EODHD,
        provider_product="historical EOD protocol fixture",
        dataset_version="protocol-contract-v1",
        entitlement_scope="protocol-fixture-no-external-entitlement",
        entitlement_owner_user_id="protocol-research-owner",
        start=_START,
        end=_END,
        calendar_mic="XNYS",
        membership_path=membership,
        aliases_path=aliases,
        benchmark=BenchmarkSpec(
            price_symbol="GSPC.INDX",
            total_return_symbol="SPY",
            total_return_kind=BenchmarkTotalReturnKind.ADJUSTED_SECURITY,
            volatility_symbol="VIX.INDX",
        ),
        strategy=StrategyIdentity(
            strategy_id="protocol_strategy",
            strategy_version="1.0.0",
            algorithm_type_name="ProtocolCore",
            relative_path="strategies/indicator/ProtocolStrategy",
            config_sha256="0" * 64,
            source_tree_sha256="1" * 64,
        ),
        git_revision="protocol-fixture-revision",
        adjustment_version="protocol-fixture-adjustment-v1",
        split_adjustment_through=_CLOCK.date(),
        split_adjustment_basis_complete=True,
        official_sessions_path=official_sessions,
        official_sessions_sha256=(
            file_sha256(official_sessions) if official_sessions is not None else None
        ),
        minimum_session_coverage=1.0,
        permanent_identity_complete=True,
        membership_authority_complete=True,
    )


def _acquire(
    snapshot: Path,
    request: SnapshotAcquisitionRequest,
    provider: _HistoricalProviderFixture,
):
    return acquire_historical_snapshot(
        snapshot,
        request=request,
        provider=provider,
        provider_errors=(ProviderFixtureError, SnapshotValidationError),
        repo_root=Path(__file__).resolve().parents[4],
        acquisition_code_paths=(Path(__file__),),
        clock=lambda: _CLOCK,
    )


def test_membership_provider_evidence_is_bound_and_unresolved_identity_is_incomplete(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA",))
    raw_evidence = store_content_object(
        snapshot,
        b'{"HistoricalComponents":{}}',
        suffix=".json",
        role="eodhd_index_historical_components_response",
        media_type="application/json",
        row_count=0,
        context={"endpoint": "/api/v1.1/fundamentals/GSPC.INDX"},
    )
    request = replace(
        _request(membership),
        membership_evidence_artifacts=(raw_evidence,),
        membership_lineage={"index_symbol": "GSPC.INDX"},
        permanent_identity_complete=False,
        membership_authority_complete=False,
    )

    result = _acquire(snapshot, request, _HistoricalProviderFixture())
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    raw_artifacts = manifest_artifacts(
        manifest,
        role="eodhd_index_historical_components_response",
    )
    membership_artifact = manifest_artifacts(manifest, role="membership")[0]

    assert result.complete is False
    assert raw_artifacts == (raw_evidence,)
    assert manifest["membership"]["permanent_identity_complete"] is False
    assert manifest["membership"]["membership_authority_complete"] is False
    assert membership_artifact.context["identity_scheme"] == (
        "mixed_or_unresolved_security_identity"
    )
    admission = evaluate_snapshot_factor_admission(manifest)
    assert admission.diagnostic_eligible is True
    assert admission.confirmatory_eligible is False
    assert admission.explicit_global_incompleteness == (
        "membership_authority_complete",
        "permanent_identity_complete",
    )
    assert manifest["factor_materialization_admission"] == admission.to_manifest()
    dataset, loaded = load_snapshot(
        snapshot,
        result.manifest_path,
        admission_scope=SnapshotFactorClaimScope.DIAGNOSTIC,
    )
    assert dataset.sessions
    assert loaded == manifest
    with pytest.raises(EquityRunError, match="not confirmatory-eligible"):
        load_snapshot(
            snapshot,
            result.manifest_path,
            admission_scope=SnapshotFactorClaimScope.CONFIRMATORY,
        )


@pytest.mark.parametrize(
    "field",
    ["permanent_identity_complete", "membership_authority_complete"],
)
def test_unknown_membership_or_identity_authority_cannot_complete_snapshot(
    tmp_path: Path,
    field: str,
) -> None:
    membership = tmp_path / "membership.csv"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA",))
    request = replace(_request(membership), **{field: None})

    result = _acquire(snapshot, request, _HistoricalProviderFixture())
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)

    assert result.complete is False
    assert manifest["complete"] is False
    assert manifest["panel_materialization_eligible"] is False


def test_undocumented_split_volume_basis_is_diagnostic_only(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA",))
    request = replace(
        _request(membership),
        split_adjustment_basis_complete=False,
    )

    result = _acquire(snapshot, request, _HistoricalProviderFixture())
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    admission = evaluate_snapshot_factor_admission(manifest)

    assert result.complete is False
    assert manifest["panel_materialization_eligible"] is True
    assert manifest["adjustment_policy"]["split_adjustment_basis_complete"] is False
    assert manifest["adjustment_policy"]["split_coordinate_reconstruction_complete"] is True
    assert manifest["adjustment_policy"]["split_adjustment_basis_limitation"] is not None
    assert admission.gates["split_coordinate_reconstruction_complete"] is True
    assert admission.explicit_global_incompleteness == ("split_adjustment_basis_complete",)
    assert admission.diagnostic_eligible is True
    assert admission.confirmatory_eligible is False
    dataset, loaded = load_snapshot(
        snapshot,
        result.manifest_path,
        admission_scope=SnapshotFactorClaimScope.DIAGNOSTIC,
    )
    assert dataset.sessions
    assert loaded == manifest
    with pytest.raises(EquityRunError, match="not confirmatory-eligible"):
        load_snapshot(
            snapshot,
            result.manifest_path,
            admission_scope=SnapshotFactorClaimScope.CONFIRMATORY,
        )


def test_snapshot_bar_artifact_names_provider_volume_semantics_explicitly(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA",))

    result = _acquire(snapshot, _request(membership), _HistoricalProviderFixture())
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    artifact = manifest_artifacts(manifest, role="security_bars")[0]
    artifact_path = verified_artifact_path(snapshot, artifact)

    assert artifact_path.read_text(encoding="utf-8").splitlines()[0] == (
        "session,open,high,low,close,split_adjusted_volume"
    )
    legacy = tmp_path / "legacy-bars.csv"
    legacy.write_text(
        "session,open,high,low,close,volume\n2024-01-02,1,1,1,1,10\n",
        encoding="utf-8",
    )
    with pytest.raises(EquityRunError, match="split_adjusted_volume explicitly"):
        _read_bars_csv(legacy)


def test_dated_aliases_split_provider_requests_without_overlap(tmp_path: Path) -> None:
    membership = tmp_path / "membership.csv"
    aliases_path = tmp_path / "aliases.csv"
    _write_membership(membership, ("AAA",))
    aliases_path.write_text(
        (
            "security_id,canonical_symbol,provider,provider_symbol,"
            "effective_from,effective_to,source_ref\n"
            "AAA,AAA,eodhd,AAA-OLD,2024-01-02,2024-01-03,symbol-directory\n"
        ),
        encoding="utf-8",
    )
    interval = read_membership_intervals(membership)[0]
    aliases = read_historical_aliases(aliases_path, provider=ValidationProvider.EODHD)
    segments = resolve_provider_segments(
        security_id=interval.security_id,
        canonical_symbol=interval.canonical_symbol,
        start=_START,
        end=_END,
        aliases=aliases,
    )

    assert [(segment.start, segment.end, segment.provider_symbol) for segment in segments] == [
        (date(2024, 1, 2), date(2024, 1, 3), "AAA-OLD"),
        (date(2024, 1, 4), date(2024, 1, 5), "AAA"),
    ]
    assert segments[0].alias_id is not None
    assert segments[1].alias_id is None


def test_permanent_identity_acquires_sequential_tickers_only_for_interval_overlap(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    aliases_path = tmp_path / "aliases.csv"
    snapshot = tmp_path / "snapshot"
    membership.write_text(
        (
            "security_id,ticker,effective_from,effective_to,source_ref,notes\n"
            "sec:0001,OLD,2024-01-01,2024-01-03,membership-old,pre-change ticker\n"
            "sec:0001,NEW,2024-01-04,2024-01-10,membership-new,post-change ticker\n"
        ),
        encoding="utf-8",
    )
    aliases_path.write_text(
        (
            "security_id,canonical_symbol,provider,provider_symbol,"
            "effective_from,effective_to,source_ref\n"
            "sec:0001,OLD,eodhd,OLD.US,2024-01-01,2024-01-03,"
            "eodhd-symbol-directory-old\n"
            "sec:0001,NEW,eodhd,NEW.US,2024-01-04,2024-01-10,"
            "eodhd-symbol-directory-new\n"
        ),
        encoding="utf-8",
    )
    provider = _HistoricalProviderFixture()

    result = _acquire(
        snapshot,
        _request(membership, aliases=aliases_path),
        provider,
    )
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    dispositions = {row["canonical_symbol"]: row for row in manifest["dispositions"]}
    aliases = {
        alias.canonical_symbol: alias
        for alias in read_historical_aliases(
            aliases_path,
            provider=ValidationProvider.EODHD,
        )
    }

    assert result.complete is True
    assert {row["security_id"] for row in dispositions.values()} == {"sec:0001"}
    assert dispositions["OLD"]["interval_id"] != dispositions["NEW"]["interval_id"]
    assert dispositions["OLD"]["requested_from"] == "2024-01-02"
    assert dispositions["OLD"]["requested_to"] == "2024-01-03"
    assert dispositions["NEW"]["requested_from"] == "2024-01-04"
    assert dispositions["NEW"]["requested_to"] == "2024-01-05"
    assert dispositions["OLD"]["status"] == DispositionStatus.ALIASED.value
    assert dispositions["NEW"]["status"] == DispositionStatus.ALIASED.value
    assert dispositions["OLD"]["provider_segments"] == [
        {
            "requested_from": "2024-01-02",
            "requested_to": "2024-01-03",
            "provider_symbol": "OLD.US",
            "history_only": False,
            "alias_id": aliases["OLD"].alias_id,
            "alias_source_ref": "eodhd-symbol-directory-old",
        }
    ]
    assert dispositions["NEW"]["provider_segments"] == [
        {
            "requested_from": "2024-01-04",
            "requested_to": "2024-01-05",
            "provider_symbol": "NEW.US",
            "history_only": False,
            "alias_id": aliases["NEW"].alias_id,
            "alias_source_ref": "eodhd-symbol-directory-new",
        }
    ]
    assert {item for item in provider.request_ranges if item[1] in {"OLD.US", "NEW.US"}} == {
        ("bars", "OLD.US", date(2024, 1, 2), date(2024, 1, 3)),
        ("splits", "OLD.US", date(2024, 1, 2), _CLOCK.date()),
        ("dividends", "OLD.US", date(2024, 1, 2), date(2024, 1, 3)),
        ("bars", "NEW.US", date(2024, 1, 4), date(2024, 1, 5)),
        ("splits", "NEW.US", date(2024, 1, 4), _CLOCK.date()),
        ("dividends", "NEW.US", date(2024, 1, 4), date(2024, 1, 5)),
    }


def test_acquisition_records_every_interval_and_resumes_verified_components(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    aliases = tmp_path / "aliases.csv"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA", "BBB"))
    _write_aliases(aliases)
    request = _request(membership, aliases=aliases)
    provider = _HistoricalProviderFixture(failing_symbols={"BBB"})

    incomplete = _acquire(snapshot, request, provider)
    incomplete_manifest = load_snapshot_manifest(snapshot, incomplete.manifest_path)
    dispositions = {row["canonical_symbol"]: row for row in incomplete_manifest["dispositions"]}

    assert incomplete.complete is False
    assert set(dispositions) == {"AAA", "BBB"}
    assert dispositions["AAA"]["status"] == DispositionStatus.ALIASED.value
    assert dispositions["BBB"]["status"] == DispositionStatus.FAILED.value
    assert provider.calls[("bars", "AAA-OLD")] == 1
    assert provider.calls[("splits", "AAA-OLD")] == 1
    assert provider.calls[("dividends", "AAA-OLD")] == 1

    provider.failing_symbols.clear()
    complete = _acquire(snapshot, request, provider)
    complete_manifest = load_snapshot_manifest(snapshot, complete.manifest_path)
    resumed = {row["canonical_symbol"]: row for row in complete_manifest["dispositions"]}

    assert complete.complete is True
    assert resumed["AAA"]["status"] == DispositionStatus.ALIASED.value
    assert resumed["BBB"]["status"] == DispositionStatus.RESOLVED.value
    assert provider.calls[("bars", "AAA-OLD")] == 1
    assert provider.calls[("splits", "AAA-OLD")] == 1
    assert provider.calls[("dividends", "AAA-OLD")] == 1
    assert provider.calls[("bars", "BBB")] == 2
    assert provider.calls[("splits", "BBB")] == 1
    assert provider.calls[("dividends", "BBB")] == 1
    assert not (snapshot / "manifest.json").exists()
    assert complete.manifest_path.stem == complete.manifest_sha256


def test_snapshot_persists_spy_adjusted_total_return_identity(tmp_path: Path) -> None:
    membership = tmp_path / "membership.csv"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA",))

    result = _acquire(snapshot, _request(membership), _HistoricalProviderFixture())
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)

    assert manifest["benchmarks"]["total_return"] == {
        "kind": BenchmarkTotalReturnKind.ADJUSTED_SECURITY.value,
        "request_id": manifest["benchmarks"]["total_return"]["request_id"],
        "requested_symbol": "SPY",
        "status": "verified",
        "symbol": "SPY",
        "adjustment_anchor_complete": True,
    }
    assert len(manifest_artifacts(manifest, role="benchmark_total_return_bars")) == 1
    assert len(manifest_artifacts(manifest, role="benchmark_total_return_splits")) == 1
    assert len(manifest_artifacts(manifest, role="benchmark_total_return_dividends")) == 1
    dataset, _loaded = load_snapshot(snapshot, result.manifest_path)
    assert dataset.benchmark_total_return_symbol == "SPY"
    assert dataset.benchmark_total_return_kind == BenchmarkTotalReturnKind.ADJUSTED_SECURITY.value


def test_snapshot_fails_closed_when_first_bar_has_a_dividend(tmp_path: Path) -> None:
    membership = tmp_path / "membership.csv"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA",))

    result = _acquire(snapshot, _request(membership), _UnanchoredDividendProvider())
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)

    assert result.complete is False
    assert manifest["panel_materialization_eligible"] is False
    assert manifest["dispositions"][0]["status"] == DispositionStatus.PARTIAL.value
    assert manifest["dispositions"][0]["reason"] == "dividend_adjustment_anchor_missing"


def test_library_generated_market_sessions_are_diagnostic_only(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA",))

    result = _acquire(snapshot, _request(membership), _HistoricalProviderFixture())
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    calendar = manifest["calendar"]
    artifact = manifest_artifacts(manifest, role="official_sessions")[0]

    assert calendar["confirmatory_eligible"] is False
    assert calendar["authority"]["source_kind"] == (MarketSessionSourceKind.LIBRARY_GENERATED.value)
    assert artifact.context["limitation"]
    assert artifact.media_type == "application/json"


def test_authoritative_market_sessions_bind_source_content_and_exact_timing(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    sessions_path = tmp_path / "official-sessions.json"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA",))
    expected_sha256 = _write_official_sessions(sessions_path)

    result = _acquire(
        snapshot,
        _request(membership, official_sessions=sessions_path),
        _HistoricalProviderFixture(),
    )
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    calendar = manifest["calendar"]

    assert calendar["confirmatory_eligible"] is True
    assert calendar["authority"]["source_kind"] == (MarketSessionSourceKind.EXCHANGE.value)
    assert calendar["authority"]["source_content_sha256"] == expected_sha256
    assert calendar["authority"]["dataset_version"] == "2024"
    assert calendar["authority"]["entitlement_scope"] == "public"
    assert calendar["authority"]["timestamp_semantics"] == (
        "published regular-session UTC open and close"
    )
    assert calendar["timing_semantics"].startswith("every session persists exact")
    dataset, _loaded = load_snapshot(snapshot, result.manifest_path)
    assert dataset.sessions == sessions_between("XNYS", _START, _END)


def test_authoritative_schedule_preserves_known_early_close_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "official-sessions.json"
    start = date(2024, 7, 2)
    end = date(2024, 7, 3)
    expected_sha256 = _write_official_sessions(
        path,
        start=start,
        end=end,
        sessions=[
            {
                "closes_at": "2024-07-02T20:00:00+00:00",
                "opens_at": "2024-07-02T13:30:00+00:00",
                "session_date": "2024-07-02",
            },
            {
                "closes_at": "2024-07-03T17:00:00+00:00",
                "opens_at": "2024-07-03T13:30:00+00:00",
                "session_date": "2024-07-03",
            },
        ],
    )

    schedule = read_historical_market_sessions(
        path,
        expected_content_sha256=expected_sha256,
        expected_venue="XNYS",
        expected_from=start,
        expected_to=end,
    )

    assert schedule.sessions[-1].opens_at == datetime(
        2024,
        7,
        3,
        13,
        30,
        tzinfo=UTC,
    )
    assert schedule.sessions[-1].closes_at == datetime(
        2024,
        7,
        3,
        17,
        tzinfo=UTC,
    )


def test_authoritative_schedule_requires_matching_caller_content_hash(
    tmp_path: Path,
) -> None:
    path = tmp_path / "official-sessions.json"
    _write_official_sessions(path)

    with pytest.raises(SnapshotValidationError, match="caller-supplied content"):
        read_historical_market_sessions(
            path,
            expected_content_sha256="0" * 64,
            expected_venue="XNYS",
            expected_from=_START,
            expected_to=_END,
        )


@pytest.mark.parametrize("symbol", ["GSPC.INDX", "SPY", "VIX.INDX"])
def test_acquisition_rejects_incomplete_benchmark_session_axis(
    tmp_path: Path,
    symbol: str,
) -> None:
    membership = tmp_path / "membership.csv"
    _write_membership(membership, ("AAA",))

    with pytest.raises(SnapshotValidationError, match="complete official-session axis"):
        _acquire(
            tmp_path / "snapshot",
            _request(membership),
            _HistoricalProviderFixture(sparse_symbols={symbol}),
        )


def test_manifest_verification_rejects_modified_content_object(tmp_path: Path) -> None:
    membership = tmp_path / "membership.csv"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA",))
    result = _acquire(snapshot, _request(membership), _HistoricalProviderFixture())
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    membership_artifact = manifest_artifacts(manifest, role="membership")[0]
    object_path = verified_artifact_path(snapshot, membership_artifact)

    object_path.write_text("modified\n", encoding="utf-8")

    with pytest.raises(SnapshotValidationError, match="hash differs"):
        load_snapshot_manifest(snapshot, result.manifest_path)


def test_dispositions_preserve_excluded_unavailable_and_partial_intervals(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    membership.write_text(
        (
            "ticker,effective_from,effective_to,source_ref,notes\n"
            "AAA,2024-01-02,2024-01-05,source,\n"
            "EMPTY,2024-01-02,2024-01-05,source,\n"
            "SPARSE,2024-01-02,2024-01-05,source,\n"
            "OLD,2023-01-03,2023-12-29,source,\n"
        ),
        encoding="utf-8",
    )
    provider = _HistoricalProviderFixture(
        empty_symbols={"EMPTY"},
        sparse_symbols={"SPARSE"},
    )

    result = _acquire(tmp_path / "snapshot", _request(membership), provider)
    manifest = load_snapshot_manifest(tmp_path / "snapshot", result.manifest_path)
    dispositions = {row["canonical_symbol"]: row["status"] for row in manifest["dispositions"]}

    assert result.complete is False
    assert dispositions == {
        "AAA": DispositionStatus.RESOLVED.value,
        "EMPTY": DispositionStatus.UNAVAILABLE.value,
        "OLD": DispositionStatus.EXCLUDED.value,
        "SPARSE": DispositionStatus.PARTIAL.value,
    }
    assert provider.calls[("bars", "OLD")] == 0


def test_loader_preserves_adjusted_prices_and_adds_lineaged_action_rows(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA",))
    result = _acquire(
        snapshot,
        _request(membership),
        _HistoricalProviderFixture(with_actions=True),
    )

    dataset, _manifest = load_snapshot(snapshot, result.manifest_path)
    actions = dataset.corporate_actions["AAA"]
    policy = TotalReturnAdjustedCorporateActionPolicy()

    assert [action.kind for action in actions] == [
        EquityCorporateActionKind.SPLIT,
        EquityCorporateActionKind.CASH_DIVIDEND,
    ]
    assert len({action.source_observation_id for action in actions}) == 2
    assert all(len(action.source_observation_id) == 64 for action in actions)
    assert all(policy.apply(action).encoded_in_adjusted_prices for action in actions)
    assert all(policy.apply(action).share_multiplier == 1.0 for action in actions)
    assert all(policy.apply(action).cash_per_pre_action_share == 0.0 for action in actions)

    sessions = sessions_between("XNYS", _START, _END)
    raw = [(session, 100.0, 101.0, 99.0, 100.5, 1_000_000.0) for session in sessions]
    expected = apply_adjustments(
        raw,
        [
            AdjustmentEvent(
                ex_date=date(2024, 1, 4),
                action_type="split",
                ratio=Decimal("2"),
            ),
            AdjustmentEvent(
                ex_date=date(2024, 1, 5),
                action_type="dividend",
                amount=Decimal("1"),
            ),
        ],
    )
    assert [bar.close for bar in dataset.bars["AAA"]] == pytest.approx([row[4] for row in expected])


def test_split_tail_after_price_period_pins_price_volume_coordinate(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA",))
    provider = _PostPeriodSplitProvider()

    result = _acquire(snapshot, _request(membership), provider)
    dataset, manifest = load_snapshot(snapshot, result.manifest_path)

    assert ("splits", "AAA", _START, _CLOCK.date()) in provider.request_ranges
    assert manifest["adjustment_policy"]["split_adjustment_through"] == (_CLOCK.date().isoformat())
    assert [bar.split_adjustment_factor for bar in dataset.bars["AAA"]] == pytest.approx(
        [0.25] * len(dataset.bars["AAA"])
    )
    assert [bar.split_adjusted_close for bar in dataset.bars["AAA"]] == pytest.approx(
        [100.5 * 0.25] * len(dataset.bars["AAA"])
    )
    assert [bar.split_adjusted_volume for bar in dataset.bars["AAA"]] == pytest.approx(
        [1_000_000.0] * len(dataset.bars["AAA"])
    )


def test_provider_identity_mismatch_fails_before_any_request(tmp_path: Path) -> None:
    membership = tmp_path / "membership.csv"
    _write_membership(membership, ("AAA",))
    provider = _HistoricalProviderFixture()
    provider.source = "unexpected"

    with pytest.raises(SnapshotValidationError, match="does not match"):
        _acquire(tmp_path / "snapshot", _request(membership), provider)

    assert provider.calls == Counter()


def test_backtest_runner_has_no_unbound_diagnostics_injection_surface() -> None:
    assert "diagnostics_evidence" not in inspect.signature(run_backtest).parameters


def test_report_is_strict_json_and_precedes_result_persistence(tmp_path: Path) -> None:
    stamp = datetime(2024, 1, 2, 21, 0)
    result = BacktestResult(
        equity_curve=[(stamp, 100_000.0)],
        gross_equity_curve=[(stamp, 100_000.0)],
        trades=[],
        cost_ledger=[],
        corporate_action_ledger=[],
        terminal_disposition_ledger=[],
        rebalance_log=[],
        skipped_entries=0,
        metrics={"profit_factor": float("inf")},
        gross_metrics={"profit_factor": float("inf")},
    )
    config = PortfolioConfig(eval_start=stamp.date(), eval_end=stamp.date())
    persisted: list[BacktestResult] = []

    _publish_backtest_result(
        tmp_path / "valid",
        result,
        config,
        {},
        diagnostics=None,
        result_persistence_gate=persisted.append,
    )

    summary = json.loads((tmp_path / "valid" / "summary.json").read_text(encoding="utf-8"))
    assert summary["metrics"]["profit_factor"] is None
    assert summary["metric_dispositions"]["net"]["profit_factor"] == "positive_infinity"
    assert persisted == [result]

    result.benchmark = {"invalid_nonfinite_value": float("inf")}
    persisted.clear()
    with pytest.raises(ValueError, match="Out of range float values"):
        _publish_backtest_result(
            tmp_path / "invalid",
            result,
            config,
            {},
            diagnostics=None,
            result_persistence_gate=persisted.append,
        )
    assert persisted == []


def test_fetch_cli_defaults_to_licensed_eodhd_membership_acquisition(tmp_path: Path) -> None:
    args = build_argument_parser().parse_args(
        [
            "fetch",
            "--snapshot",
            str(tmp_path / "snapshot"),
            "--strategy",
            str(tmp_path / "strategy"),
            "--provider",
            "eodhd",
            "--entitlement-scope",
            "personal-research-single-user",
            "--end",
            "2025-12-31",
            "--split-adjustment-through",
            "2026-08-02",
        ]
    )

    assert args.membership is None
    assert args.aliases is None
    assert args.membership_index == "GSPC.INDX"
    assert args.split_adjustment_through == date(2026, 8, 2)
    assert args.membership_evidence_start == date(2018, 1, 1)
    assert args.membership_authority_corrections_manifest is None


def test_membership_correction_acquisition_cli_requires_explicit_paths(tmp_path: Path) -> None:
    args = build_argument_parser().parse_args(
        [
            "acquire-membership-correction-evidence",
            "--snapshot",
            str(tmp_path / "snapshot"),
            "--source-spec",
            str(tmp_path / "corrections.json"),
        ]
    )

    assert args.snapshot == tmp_path / "snapshot"
    assert args.source_spec == tmp_path / "corrections.json"


def test_official_session_compiler_cli_freezes_required_coverage(tmp_path: Path) -> None:
    args = build_argument_parser().parse_args(
        [
            "compile-official-sessions",
            "--output-root",
            str(tmp_path / "sessions"),
        ]
    )

    assert args.start == date(2018, 11, 27)
    assert args.end == date(2026, 1, 6)
    assert args.output_root == tmp_path / "sessions"
    assert args.source_artifact is None
    assert args.source_artifact_sha256 is None


def test_official_session_compiler_cli_accepts_pinned_source_artifact(tmp_path: Path) -> None:
    source_sha256 = "a" * 64
    source_artifact = tmp_path / f"{source_sha256}.json"
    args = build_argument_parser().parse_args(
        [
            "compile-official-sessions",
            "--output-root",
            str(tmp_path / "sessions"),
            "--end",
            "2026-12-31",
            "--source-artifact",
            str(source_artifact),
            "--source-artifact-sha256",
            source_sha256,
        ]
    )

    assert args.end == date(2026, 12, 31)
    assert args.source_artifact == source_artifact
    assert args.source_artifact_sha256 == source_sha256


def test_membership_overlap_is_rejected(tmp_path: Path) -> None:
    membership = tmp_path / "membership.csv"
    membership.write_text(
        (
            "ticker,effective_from,effective_to,source_ref,notes\n"
            "AAA,2024-01-02,2024-01-04,source-a,\n"
            "AAA,2024-01-04,2024-01-05,source-b,\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotValidationError, match="overlapping membership"):
        read_membership_intervals(membership)


def test_membership_authority_is_typed_revision_pinned_and_non_reconstructed(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    revision = hashlib.sha256(b"official-membership-release").hexdigest()
    membership.write_text(
        (
            "security_id,ticker,effective_from,effective_to,source_ref,notes,"
            "source_kind,source_provider,source_dataset,source_revision_sha256\n"
            "sec:0001,AAA,2024-01-02,2024-01-05,release-catalogue,,"
            f"{MembershipSourceKind.OFFICIAL_POINT_IN_TIME.value},"
            f"index-provider,sp500-membership-history,{revision}\n"
        ),
        encoding="utf-8",
    )

    intervals = read_membership_intervals(membership)
    summary = membership_source_authority_summary(intervals)

    assert summary["confirmatory_eligible"] is True
    assert summary["untyped_interval_count"] == 0
    assert summary["reconstructed_interval_count"] == 0
    assert intervals[0].source_authority is not None
    assert intervals[0].source_authority.revision_sha256 == revision


def test_legacy_membership_citation_remains_diagnostic_only(tmp_path: Path) -> None:
    membership = tmp_path / "membership.csv"
    _write_membership(membership, ("AAA",))

    summary = membership_source_authority_summary(read_membership_intervals(membership))

    assert summary["confirmatory_eligible"] is False
    assert summary["untyped_interval_count"] == 1


def test_membership_authority_requires_complete_typed_columns(tmp_path: Path) -> None:
    membership = tmp_path / "membership.csv"
    membership.write_text(
        (
            "ticker,effective_from,effective_to,source_ref,source_kind\n"
            "AAA,2024-01-02,2024-01-05,source,official_point_in_time\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotValidationError, match="authority fields are incomplete"):
        read_membership_intervals(membership)


def test_ticker_change_requires_nonblank_explicit_security_identity(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    membership.write_text(
        (
            "security_id,ticker,effective_from,effective_to,source_ref,notes\n"
            "sec:0001,OLD,2024-01-02,2024-01-03,source-old,\n"
            ",NEW,2024-01-04,2024-01-05,source-new,\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotValidationError, match="security_id must be non-blank"):
        read_membership_intervals(membership)


def test_sequential_ticker_identity_rejects_overlapping_membership_intervals(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    membership.write_text(
        (
            "security_id,ticker,effective_from,effective_to,source_ref,notes\n"
            "sec:0001,OLD,2024-01-02,2024-01-04,source-old,\n"
            "sec:0001,NEW,2024-01-04,2024-01-05,source-new,\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotValidationError, match="overlapping membership"):
        read_membership_intervals(membership)


def test_recycled_ticker_rejects_overlapping_security_assignments(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    membership.write_text(
        (
            "security_id,ticker,effective_from,effective_to,source_ref,notes\n"
            "sec:0001,AAA,2024-01-02,2024-01-04,source-one,\n"
            "sec:0002,AAA,2024-01-04,2024-01-05,source-two,\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        SnapshotValidationError,
        match="overlapping membership ticker assignments",
    ):
        read_membership_intervals(membership)


def test_historical_aliases_allow_sequential_ticker_warmup_but_require_provenance(
    tmp_path: Path,
) -> None:
    aliases = tmp_path / "aliases.csv"
    aliases.write_text(
        (
            "security_id,canonical_symbol,provider,provider_symbol,"
            "effective_from,effective_to,source_ref\n"
            "sec:0001,OLD,eodhd,OLD.US,2024-01-02,2024-01-04,source-old\n"
            "sec:0001,NEW,eodhd,NEW.US,2024-01-04,2024-01-05,source-new\n"
        ),
        encoding="utf-8",
    )

    parsed = read_historical_aliases(
        aliases,
        provider=ValidationProvider.EODHD,
    )

    assert [(row.canonical_symbol, row.provider_symbol) for row in parsed] == [
        ("OLD", "OLD.US"),
        ("NEW", "NEW.US"),
    ]

    aliases.write_text(
        (
            "security_id,canonical_symbol,provider,provider_symbol,"
            "effective_from,effective_to,source_ref\n"
            "sec:0001,OLD,eodhd,OLD.US,2024-01-02,2024-01-03,\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(SnapshotValidationError, match="source_ref must be non-blank"):
        read_historical_aliases(aliases, provider=ValidationProvider.EODHD)


def test_snapshot_admits_only_bound_prelisting_history_for_panel_materialization(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    aliases = tmp_path / "aliases.csv"
    snapshot = tmp_path / "snapshot"
    listing_date = date(2024, 1, 3)
    evidence = _listing_evidence_artifact(snapshot, listing_date=listing_date)
    _write_listing_evidence_membership(
        membership_path=membership,
        aliases_path=aliases,
        evidence=evidence,
        listing_date=listing_date,
    )
    request = replace(
        _request(membership, aliases=aliases),
        security_history_sessions=3,
        membership_evidence_artifacts=(evidence,),
        permanent_identity_complete=True,
        membership_authority_complete=True,
    )

    result = _acquire(
        snapshot,
        request,
        _ListingDateProvider(listing_date=listing_date),
    )
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    disposition = manifest["dispositions"][0]
    listing_evidence = read_membership_intervals(membership)[0].listing_evidence

    assert result.complete is False
    assert listing_evidence is not None
    assert manifest["complete"] is False
    assert manifest["panel_materialization_eligible"] is True
    assert disposition["status"] == DispositionStatus.PARTIAL.value
    assert disposition["reason"] == ("registered_price_history_structural_listing_warmup")
    assert disposition["history_missing_sessions"] == ["2024-01-02"]
    assert disposition["panel_materialization_eligible"] is True
    assert disposition["deferred_panel_validation"] == {
        "kind": "structural_listing_warmup_candidate",
        "validation_owner": ("lib_strategy.equity_market_factors.StructuralBreadthExclusion"),
        "listing_evidence_id": listing_evidence.evidence_id,
        "listing_evidence_sha256": evidence.sha256,
        "first_official_listing_session": "2024-01-03",
        "required_history_sessions": 2,
        "observed_history_sessions": 1,
        "missing_history_sessions": 1,
        "panel_requirements": (
            "revalidate the exact rolling official-session window, complete "
            "post-listing observations, immutable price hashes, and the frozen "
            "maximum structural-exclusion fraction"
        ),
    }
    with pytest.raises(EquityRunError, match="ineligible for portfolio validation"):
        load_snapshot(snapshot, result.manifest_path)
    dataset, loaded = load_snapshot(
        snapshot,
        result.manifest_path,
        admission_scope=SnapshotFactorClaimScope.DIAGNOSTIC,
    )
    assert loaded == manifest
    assert [bar.session for bar in dataset.bars["AAA"]] == [
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 5),
    ]


def test_snapshot_uses_reviewed_listing_date_only_with_frozen_manifest(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    aliases = tmp_path / "aliases.csv"
    snapshot = tmp_path / "snapshot"
    vendor_listing_date = date(2024, 1, 4)
    reviewed_listing_date = date(2024, 1, 3)
    evidence = _listing_evidence_artifact(snapshot, listing_date=vendor_listing_date)
    correction_manifest = store_content_object(
        snapshot,
        canonical_json_bytes({"schema": "reviewed-listing-fixture-v1"}),
        suffix=".json",
        role="eodhd_membership_correction_evidence_manifest",
        media_type="application/json",
        row_count=1,
        context={},
    )
    _write_listing_evidence_membership(
        membership_path=membership,
        aliases_path=aliases,
        evidence=evidence,
        listing_date=vendor_listing_date,
        reviewed_listing_date=reviewed_listing_date,
        reviewed_listing_correction_id="reviewed-listing-v1",
        reviewed_listing_correction_manifest_sha256=correction_manifest.sha256,
    )
    request = replace(
        _request(membership, aliases=aliases),
        security_history_sessions=3,
        membership_evidence_artifacts=(evidence, correction_manifest),
        permanent_identity_complete=True,
        membership_authority_complete=True,
    )

    result = _acquire(
        snapshot,
        request,
        _ListingDateProvider(listing_date=reviewed_listing_date),
    )
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    disposition = manifest["dispositions"][0]

    assert disposition["panel_materialization_eligible"] is True
    assert disposition["deferred_panel_validation"]["first_official_listing_session"] == (
        reviewed_listing_date.isoformat()
    )

    missing_manifest_snapshot = tmp_path / "snapshot-without-correction-manifest"
    missing_manifest_evidence = _listing_evidence_artifact(
        missing_manifest_snapshot,
        listing_date=vendor_listing_date,
    )
    missing_manifest_request = replace(
        request,
        membership_evidence_artifacts=(missing_manifest_evidence,),
    )
    missing_manifest_result = _acquire(
        missing_manifest_snapshot,
        missing_manifest_request,
        _ListingDateProvider(listing_date=reviewed_listing_date),
    )
    missing_manifest = load_snapshot_manifest(
        missing_manifest_snapshot,
        missing_manifest_result.manifest_path,
    )
    assert missing_manifest["dispositions"][0]["panel_materialization_eligible"] is False


def test_postlisting_history_gap_remains_ineligible_for_panel_materialization(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    aliases = tmp_path / "aliases.csv"
    snapshot = tmp_path / "snapshot"
    listing_date = date(2024, 1, 2)
    evidence = _listing_evidence_artifact(snapshot, listing_date=listing_date)
    _write_listing_evidence_membership(
        membership_path=membership,
        aliases_path=aliases,
        evidence=evidence,
        listing_date=listing_date,
    )
    request = replace(
        _request(membership, aliases=aliases),
        security_history_sessions=3,
        membership_evidence_artifacts=(evidence,),
        permanent_identity_complete=True,
        membership_authority_complete=True,
    )

    result = _acquire(
        snapshot,
        request,
        _ListingDateProvider(
            listing_date=listing_date,
            omitted_sessions=frozenset({date(2024, 1, 3)}),
        ),
    )
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    disposition = manifest["dispositions"][0]

    assert manifest["panel_materialization_eligible"] is False
    assert disposition["reason"] == "registered_price_history_incomplete"
    assert disposition["deferred_panel_validation"] is None
    with pytest.raises(
        EquityRunError,
        match="not diagnostic-eligible for factor materialization",
    ):
        load_snapshot(
            snapshot,
            result.manifest_path,
            admission_scope=SnapshotFactorClaimScope.DIAGNOSTIC,
        )


def test_listing_warmup_requires_exact_raw_evidence_context(tmp_path: Path) -> None:
    membership = tmp_path / "membership.csv"
    aliases = tmp_path / "aliases.csv"
    snapshot = tmp_path / "snapshot"
    listing_date = date(2024, 1, 3)
    evidence = _listing_evidence_artifact(snapshot, listing_date=listing_date)
    _write_listing_evidence_membership(
        membership_path=membership,
        aliases_path=aliases,
        evidence=evidence,
        listing_date=listing_date,
    )
    mismatched_evidence = replace(
        evidence,
        context={**evidence.context, "requested_symbol": "OTHER.US"},
    )
    request = replace(
        _request(membership, aliases=aliases),
        security_history_sessions=3,
        membership_evidence_artifacts=(mismatched_evidence,),
        permanent_identity_complete=True,
        membership_authority_complete=True,
    )

    result = _acquire(
        snapshot,
        request,
        _ListingDateProvider(listing_date=listing_date),
    )
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    disposition = manifest["dispositions"][0]

    assert disposition["reason"] == "registered_price_history_incomplete"
    assert disposition["deferred_panel_validation"] is None
    assert manifest["panel_materialization_eligible"] is False


def test_unverified_terminal_tail_remains_ineligible_for_panel_materialization(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    snapshot = tmp_path / "snapshot"
    _write_membership(membership, ("AAA",))

    result = _acquire(
        snapshot,
        _request(membership),
        _HistoricalProviderFixture(sparse_symbols={"AAA"}),
    )
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    disposition = manifest["dispositions"][0]

    assert disposition["missing_sessions"] == ["2024-01-05"]
    assert disposition["reason"] == "session_coverage_below_policy"
    assert disposition["panel_materialization_eligible"] is False
    assert manifest["panel_materialization_eligible"] is False


def test_price_warmup_is_stitched_without_extending_membership(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    aliases = tmp_path / "aliases.csv"
    snapshot = tmp_path / "snapshot"
    membership.write_text(
        (
            "security_id,ticker,effective_from,effective_to,source_ref,notes\n"
            "sec:0001,AAA,2024-01-04,2024-01-05,membership-source,new constituent\n"
        ),
        encoding="utf-8",
    )
    aliases.write_text(
        (
            "security_id,canonical_symbol,provider,provider_symbol,"
            "effective_from,effective_to,source_ref\n"
            "sec:0001,AAA,eodhd,AAA-OLD,2024-01-02,2024-01-03,"
            "provider-symbol-directory\n"
        ),
        encoding="utf-8",
    )
    request = replace(
        _request(membership, aliases=aliases),
        security_history_sessions=3,
    )
    provider = _HistoricalProviderFixture()

    result = _acquire(snapshot, request, provider)
    dataset, manifest = load_snapshot(snapshot, result.manifest_path)
    disposition = manifest["dispositions"][0]

    assert result.complete is True
    assert [bar.session for bar in dataset.bars["AAA"]] == sessions_between(
        "XNYS",
        _START,
        _END,
    )
    assert dataset.membership["AAA"] == [(date(2024, 1, 4), date(2024, 1, 5))]
    assert disposition["history_requested_from"] == "2024-01-02"
    assert disposition["history_requested_to"] == "2024-01-03"
    assert disposition["history_missing_sessions"] == []
    assert manifest["security_history_policy"]["registered_sessions"] == 3
    history_segments = [
        segment for segment in disposition["provider_segments"] if segment["history_only"]
    ]
    assert history_segments == [
        {
            "alias_id": read_historical_aliases(
                aliases,
                provider=ValidationProvider.EODHD,
            )[0].alias_id,
            "alias_source_ref": "provider-symbol-directory",
            "history_only": True,
            "provider_symbol": "AAA-OLD",
            "requested_from": "2024-01-02",
            "requested_to": "2024-01-03",
        }
    ]


def test_price_warmup_without_sourced_alias_is_deferred_not_fetched(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    snapshot = tmp_path / "snapshot"
    membership.write_text(
        (
            "security_id,ticker,effective_from,effective_to,source_ref,notes\n"
            "sec:0001,AAA,2024-01-04,2024-01-05,membership-source,new constituent\n"
        ),
        encoding="utf-8",
    )
    provider = _HistoricalProviderFixture()
    request = replace(
        _request(membership),
        security_history_sessions=3,
    )

    result = _acquire(snapshot, request, provider)
    manifest = load_snapshot_manifest(snapshot, result.manifest_path)
    disposition = manifest["dispositions"][0]

    # The window is still computed and recorded, so the omission is auditable,
    # but no pre-membership bar is requested: without a dated sourced alias the
    # prefix belongs to a different security.
    assert disposition["history_requested_from"] == "2024-01-02"
    assert disposition["history_requested_to"] == "2024-01-03"
    assert [
        segment for segment in disposition["provider_segments"] if segment["history_only"]
    ] == []
    assert [
        component
        for component in disposition["component_requests"]
        if component["component"].startswith("security_history_")
    ] == []
    # The deferred prefix leaves the interval partial, never resolved, so a
    # downstream consumer cannot mistake it for full requested coverage.
    assert disposition["status"] == DispositionStatus.PARTIAL.value
    # The membership window itself is unaffected: exactly one bar request, for
    # the constituent's own sessions.
    assert provider.calls[("bars", "AAA")] == 1
    assert [entry for entry in provider.request_ranges if entry[1] == "AAA"] == [
        ("bars", "AAA", date(2024, 1, 4), date(2024, 1, 5)),
        ("splits", "AAA", date(2024, 1, 4), date(2024, 1, 8)),
        ("dividends", "AAA", date(2024, 1, 4), date(2024, 1, 5)),
    ]


def test_alias_dates_cannot_cross_a_canonical_ticker_change(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    aliases = tmp_path / "aliases.csv"
    membership.write_text(
        (
            "security_id,ticker,effective_from,effective_to,source_ref,notes\n"
            "sec:0001,OLD,2024-01-02,2024-01-03,source-old,\n"
            "sec:0001,NEW,2024-01-04,2024-01-05,source-new,\n"
        ),
        encoding="utf-8",
    )
    aliases.write_text(
        (
            "security_id,canonical_symbol,provider,provider_symbol,"
            "effective_from,effective_to,source_ref\n"
            "sec:0001,OLD,eodhd,OLD.US,2024-01-02,2024-01-05,"
            "provider-directory\n"
        ),
        encoding="utf-8",
    )
    provider = _HistoricalProviderFixture()

    with pytest.raises(SnapshotValidationError, match="overlaps membership ticker NEW"):
        _acquire(
            tmp_path / "snapshot",
            _request(membership, aliases=aliases),
            provider,
        )

    assert provider.calls == Counter()


def test_resolved_provider_symbol_collision_fails_before_acquisition(
    tmp_path: Path,
) -> None:
    membership = tmp_path / "membership.csv"
    aliases = tmp_path / "aliases.csv"
    membership.write_text(
        (
            "security_id,ticker,effective_from,effective_to,source_ref,notes\n"
            "sec:0001,AAA,2024-01-02,2024-01-05,source-one,\n"
            "sec:0002,BBB,2024-01-02,2024-01-05,source-two,\n"
        ),
        encoding="utf-8",
    )
    aliases.write_text(
        (
            "security_id,canonical_symbol,provider,provider_symbol,"
            "effective_from,effective_to,source_ref\n"
            "sec:0002,BBB,eodhd,AAA,2024-01-02,2024-01-05,"
            "provider-directory\n"
        ),
        encoding="utf-8",
    )
    provider = _HistoricalProviderFixture()

    with pytest.raises(SnapshotValidationError, match="ambiguous resolved eodhd symbol"):
        _acquire(
            tmp_path / "snapshot",
            _request(membership, aliases=aliases),
            provider,
        )

    assert provider.calls == Counter()
