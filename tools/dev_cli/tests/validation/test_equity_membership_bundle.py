"""Frozen EODHD membership materialization and resume-boundary tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest

from dev_cli.validation.backtest import equity_run_inputs
from dev_cli.validation.backtest.equity_membership_bundle import (
    EODHDMembershipProvider,
    freeze_eodhd_membership_materialization,
    load_frozen_eodhd_membership_materialization,
)
from dev_cli.validation.backtest.equity_membership_eodhd import (
    EODHDMembershipMaterializationError,
    materialize_eodhd_index_membership,
)
from dev_cli.validation.backtest.equity_run_cli import build_argument_parser
from dev_cli.validation.backtest.equity_run_inputs import EquityRunError, fetch_snapshot
from dev_cli.validation.backtest.equity_snapshot import (
    BenchmarkSpec,
    BenchmarkTotalReturnKind,
    SnapshotAcquisitionResult,
    StrategyIdentity,
    ValidationProvider,
)
from dev_cli.validation.evidence import (
    ContentAddressedArtifact,
    canonical_json_bytes,
    evidence_sha256,
    load_json_object,
    store_content_object,
    write_content_addressed_manifest,
)

_DATASET = "membership-bundle-test-v1"
_ENTITLEMENT = "personal-research-single-user"
_OWNER = "owner-user"
_INDEX = "GSPC.INDX"
_EVIDENCE_START = date(2018, 1, 1)
_START = date(2020, 1, 1)
_END = date(2025, 12, 31)
_SPLIT_THROUGH = date(2026, 8, 2)


def _object(
    root: Path,
    *,
    role: str,
    row_count: int,
    context: dict[str, object],
) -> ContentAddressedArtifact:
    return store_content_object(
        root,
        canonical_json_bytes({"fixture_role": role}) + b"\n",
        suffix=".json",
        role=role,
        media_type="application/json",
        row_count=row_count,
        context=context,
    )


def _dataset_context() -> dict[str, object]:
    return {
        "dataset_version": _DATASET,
        "entitlement_scope": _ENTITLEMENT,
    }


def _freeze_bundle(root: Path) -> Any:
    membership = store_content_object(
        root,
        (
            b"security_id,ticker,effective_from,effective_to,source_ref\n"
            b"figi:TEST,TEST,2020-01-01,2025-12-31,fixture\n"
        ),
        suffix=".csv",
        role="licensed_point_in_time_membership",
        media_type="text/csv; charset=utf-8",
        row_count=1,
        context={
            "membership_authority_complete": True,
            "permanent_identity_complete": True,
            "reviewed_identity_edges_sha256": None,
        },
    )
    aliases = store_content_object(
        root,
        (
            b"security_id,canonical_symbol,provider,provider_symbol,"
            b"effective_from,effective_to,source_ref\n"
            b"figi:TEST,TEST,eodhd,TEST,2020-01-01,2025-12-31,fixture\n"
        ),
        suffix=".csv",
        role="licensed_historical_aliases",
        media_type="text/csv; charset=utf-8",
        row_count=1,
        context={
            "membership_authority_complete": True,
            "permanent_identity_complete": True,
            "reviewed_identity_edges_sha256": None,
        },
    )
    source_roles = (
        "eodhd_index_historical_ticker_components_response",
        "eodhd_index_historical_components_response",
        "eodhd_active_symbol_directory_response",
        "eodhd_delisted_symbol_directory_response",
        "eodhd_us_symbol_change_history_response",
    )
    source_evidence = tuple(
        _object(
            root,
            role=role,
            row_count=1,
            context=_dataset_context(),
        )
        for role in source_roles
    )
    crosscheck = _object(
        root,
        role="eodhd_membership_authority_crosscheck",
        row_count=0,
        context={"membership_authority_complete": True},
    )
    resolutions = _object(
        root,
        role="eodhd_membership_identity_resolutions",
        row_count=1,
        context={"permanent_identity_complete": True},
    )
    mapping = _object(
        root,
        role="eodhd_id_mapping_response",
        row_count=1,
        context=_dataset_context(),
    )
    general = _object(
        root,
        role="eodhd_security_fundamentals_general_response",
        row_count=1,
        context=_dataset_context(),
    )
    lineage: dict[str, object] = {
        "dataset_version": _DATASET,
        "entitlement_scope": _ENTITLEMENT,
        "entitlement_owner_user_id": _OWNER,
        "evidence_from": _EVIDENCE_START.isoformat(),
        "finalized_at": "2026-08-02T12:00:00+00:00",
        "fundamentals_general_response_count": 1,
        "historical_membership_publication_availability_complete": False,
        "index_symbol": _INDEX,
        "interval_count": 1,
        "mapping_response_count": 1,
        "membership_authority_complete": True,
        "permanent_identity_complete": True,
        "requested_from": _START.isoformat(),
        "requested_to": _END.isoformat(),
        "reviewed_correction_count": 0,
        "reviewed_correction_evidence_manifest_sha256": None,
        "reviewed_identity_edges_sha256": None,
        "security_count": 1,
    }
    return freeze_eodhd_membership_materialization(
        root,
        membership_artifact=membership,
        aliases_artifact=aliases,
        identity_edges_artifact=None,
        evidence_artifacts=(
            *source_evidence,
            crosscheck,
            resolutions,
            mapping,
            general,
        ),
        lineage=lineage,
        dataset_version=_DATASET,
        entitlement_scope=_ENTITLEMENT,
        entitlement_owner_user_id=_OWNER,
        index_symbol=_INDEX,
        evidence_start=_EVIDENCE_START,
        requested_start=_START,
        requested_end=_END,
        interval_count=1,
        security_count=1,
        permanent_identity_complete=True,
        membership_authority_complete=True,
    )


def _load(root: Path, manifest_path: Path, **overrides: object) -> Any:
    expected: dict[str, object] = {
        "expected_dataset_version": _DATASET,
        "expected_entitlement_scope": _ENTITLEMENT,
        "expected_entitlement_owner_user_id": _OWNER,
        "expected_index_symbol": _INDEX,
        "expected_evidence_start": _EVIDENCE_START,
        "expected_requested_start": _START,
        "expected_requested_end": _END,
    }
    expected.update(overrides)
    return load_frozen_eodhd_membership_materialization(
        root,
        manifest_path,
        **expected,  # type: ignore[arg-type]
    )


def _benchmark() -> BenchmarkSpec:
    return BenchmarkSpec(
        price_symbol="GSPC.INDX",
        total_return_symbol="SPY",
        total_return_kind=BenchmarkTotalReturnKind.ADJUSTED_SECURITY,
        volatility_symbol="VIX.INDX",
    )


def test_frozen_bundle_round_trips_every_resume_input(tmp_path: Path) -> None:
    frozen = _freeze_bundle(tmp_path)

    loaded = _load(tmp_path, frozen.manifest_path)

    assert frozen.manifest_path.name == f"{frozen.manifest_sha256}.json"
    assert loaded.membership_path == frozen.membership_path
    assert loaded.aliases_path == frozen.aliases_path
    assert loaded.identity_edges_path is None
    assert loaded.lineage == frozen.lineage
    assert loaded.interval_count == frozen.interval_count == 1
    assert loaded.security_count == frozen.security_count == 1
    assert loaded.permanent_identity_complete is True
    assert loaded.membership_authority_complete is True
    assert loaded.manifest_sha256 == frozen.manifest_sha256
    assert loaded.evidence_artifacts == frozen.evidence_artifacts
    assert loaded.evidence_artifacts[0].role == "eodhd_membership_materialization_manifest"
    assert all("api_token" not in str(item.as_manifest()) for item in loaded.evidence_artifacts)


@pytest.mark.parametrize(
    ("override", "value", "error"),
    [
        ("expected_dataset_version", "different", "dataset_version differs"),
        ("expected_entitlement_scope", "different", "entitlement_scope differs"),
        (
            "expected_entitlement_owner_user_id",
            "different-owner",
            "entitlement_owner_user_id differs",
        ),
        ("expected_index_symbol", "NDX.INDX", "index_symbol differs"),
        ("expected_evidence_start", date(2017, 1, 1), "requested window differs"),
        ("expected_requested_start", date(2020, 1, 2), "requested window differs"),
        ("expected_requested_end", date(2025, 12, 30), "requested window differs"),
    ],
)
def test_frozen_bundle_rejects_scope_or_window_drift(
    tmp_path: Path,
    override: str,
    value: object,
    error: str,
) -> None:
    frozen = _freeze_bundle(tmp_path)

    with pytest.raises(EODHDMembershipMaterializationError, match=error):
        _load(tmp_path, frozen.manifest_path, **{override: value})


def test_frozen_bundle_rejects_altered_content_object(tmp_path: Path) -> None:
    frozen = _freeze_bundle(tmp_path)
    frozen.membership_path.write_bytes(b"altered")

    with pytest.raises(EODHDMembershipMaterializationError, match="artifact hash differs"):
        _load(tmp_path, frozen.manifest_path)


def test_frozen_bundle_rejects_unknown_evidence_role_in_valid_envelope(
    tmp_path: Path,
) -> None:
    frozen = _freeze_bundle(tmp_path)
    envelope = load_json_object(frozen.manifest_path)
    payload = deepcopy(envelope["manifest"])
    assert isinstance(payload, dict)
    artifacts = payload["artifacts"]
    bindings = payload["bindings"]
    assert isinstance(artifacts, list)
    assert isinstance(bindings, dict)
    assert isinstance(bindings["evidence"], list)
    artifacts[2]["role"] = "eodhd_unknown_membership_evidence"
    bindings["evidence"][0]["role"] = "eodhd_unknown_membership_evidence"
    bindings["evidence"][0]["descriptor_sha256"] = evidence_sha256(artifacts[2])
    altered_path, _altered_sha256 = write_content_addressed_manifest(tmp_path, payload)

    with pytest.raises(EODHDMembershipMaterializationError, match="unsupported evidence roles"):
        _load(tmp_path, altered_path)


def test_frozen_bundle_requires_its_manifest_evidence_object(tmp_path: Path) -> None:
    frozen = _freeze_bundle(tmp_path)
    manifest_evidence = frozen.evidence_artifacts[0]
    (tmp_path / manifest_evidence.path).unlink()

    with pytest.raises(
        EODHDMembershipMaterializationError,
        match="manifest content object is missing or altered",
    ):
        _load(tmp_path, frozen.manifest_path)


@pytest.mark.parametrize(
    "conflict",
    ["membership_path", "aliases_path", "membership_authority_correction_manifest_path"],
)
def test_fetch_rejects_manifest_with_another_membership_source(
    tmp_path: Path,
    conflict: str,
) -> None:
    kwargs: dict[str, object] = {
        "membership_path": None,
        "aliases_path": None,
        "membership_authority_correction_manifest_path": None,
    }
    kwargs[conflict] = tmp_path / "conflict"

    with pytest.raises(EquityRunError, match="cannot be combined"):
        fetch_snapshot(
            tmp_path / "snapshot",
            start=_START,
            end=_END,
            strategy_dir=tmp_path / "strategy",
            benchmark=_benchmark(),
            provider=ValidationProvider.EODHD,
            entitlement_scope=_ENTITLEMENT,
            dataset_version=_DATASET,
            split_adjustment_through=_SPLIT_THROUGH,
            membership_materialization_manifest_path=tmp_path / "bundle.json",
            **kwargs,  # type: ignore[arg-type]
        )


def test_fetch_resume_passes_exact_bundle_without_membership_refetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "snapshot"
    frozen = _freeze_bundle(snapshot)
    captured: dict[str, object] = {}

    class _Client:
        closed = False

        def close(self) -> None:
            self.closed = True

    client = _Client()
    expected = SnapshotAcquisitionResult(
        manifest_path=snapshot / "manifests" / f"{'f' * 64}.json",
        manifest_sha256="f" * 64,
        complete=False,
        disposition_counts={"failed": 1},
    )
    identity = StrategyIdentity(
        strategy_id="bundle-test",
        strategy_version="1.0.0",
        algorithm_type_name="BundleTestCore",
        relative_path="strategies/indicator/BundleTest",
        config_sha256="1" * 64,
        source_tree_sha256="2" * 64,
    )

    def _acquire(*_args: object, **kwargs: object) -> SnapshotAcquisitionResult:
        captured["request"] = kwargs["request"]
        captured["provider"] = kwargs["provider"]
        return expected

    monkeypatch.setenv("EODHD_API_TOKEN", "secret-not-persisted")
    monkeypatch.setenv("SP500_RESEARCH_OWNER_USER_ID", "owner-user")
    monkeypatch.setattr(equity_run_inputs, "EODHDClient", lambda **_kwargs: client)
    monkeypatch.setattr(
        equity_run_inputs,
        "materialize_eodhd_index_membership",
        lambda *_args, **_kwargs: pytest.fail("membership acquisition must not run"),
    )
    monkeypatch.setattr(equity_run_inputs, "_strategy_identity", lambda _path: (object, identity))
    monkeypatch.setattr(equity_run_inputs, "_git_rev", lambda: "bundle-test-revision")
    monkeypatch.setattr(equity_run_inputs, "acquire_historical_snapshot", _acquire)

    observed = fetch_snapshot(
        snapshot,
        start=_START,
        end=_END,
        strategy_dir=tmp_path / "strategy",
        membership_path=None,
        aliases_path=None,
        benchmark=_benchmark(),
        provider=ValidationProvider.EODHD,
        entitlement_scope=_ENTITLEMENT,
        dataset_version=_DATASET,
        split_adjustment_through=_SPLIT_THROUGH,
        membership_index_symbol=_INDEX,
        membership_evidence_start=_EVIDENCE_START,
        membership_materialization_manifest_path=frozen.manifest_path,
    )

    request = captured["request"]
    assert observed is expected
    assert captured["provider"] is client
    assert request.membership_path == frozen.membership_path
    assert request.aliases_path == frozen.aliases_path
    assert request.membership_evidence_artifacts == frozen.evidence_artifacts
    assert request.membership_lineage == frozen.lineage
    assert request.entitlement_owner_user_id == _OWNER
    assert request.permanent_identity_complete is True
    assert request.membership_authority_complete is True
    assert client.closed is True


@pytest.mark.parametrize(
    ("configured_owner", "error"),
    [
        (None, "SP500_RESEARCH_OWNER_USER_ID"),
        ("different-owner", "entitlement_owner_user_id differs"),
    ],
)
def test_fetch_resume_rejects_absent_or_mismatched_owner_before_provider_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_owner: str | None,
    error: str,
) -> None:
    snapshot = tmp_path / "snapshot"
    frozen = _freeze_bundle(snapshot)
    monkeypatch.setenv("EODHD_API_TOKEN", "secret-not-persisted")
    if configured_owner is None:
        monkeypatch.delenv("SP500_RESEARCH_OWNER_USER_ID", raising=False)
    else:
        monkeypatch.setenv("SP500_RESEARCH_OWNER_USER_ID", configured_owner)
    monkeypatch.setattr(
        equity_run_inputs,
        "EODHDClient",
        lambda **_kwargs: pytest.fail("provider must not be initialized"),
    )

    with pytest.raises(EquityRunError, match=error):
        fetch_snapshot(
            snapshot,
            start=_START,
            end=_END,
            strategy_dir=tmp_path / "strategy",
            membership_path=None,
            aliases_path=None,
            benchmark=_benchmark(),
            provider=ValidationProvider.EODHD,
            entitlement_scope=_ENTITLEMENT,
            dataset_version=_DATASET,
            split_adjustment_through=_SPLIT_THROUGH,
            membership_index_symbol=_INDEX,
            membership_evidence_start=_EVIDENCE_START,
            membership_materialization_manifest_path=frozen.manifest_path,
        )


def test_fetch_parser_accepts_exact_materialization_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "snapshot" / "manifests" / f"{'a' * 64}.json"
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
            _ENTITLEMENT,
            "--membership-materialization-manifest",
            str(manifest),
            "--end",
            _END.isoformat(),
            "--split-adjustment-through",
            _SPLIT_THROUGH.isoformat(),
        ]
    )

    assert args.membership_materialization_manifest == manifest
    assert args.membership is None
    assert args.aliases is None
    assert args.membership_authority_corrections_manifest is None


def test_evidence_must_start_before_requested_period(tmp_path: Path) -> None:
    provider = cast(EODHDMembershipProvider, object())

    with pytest.raises(EODHDMembershipMaterializationError, match="evidence_start"):
        materialize_eodhd_index_membership(
            tmp_path,
            provider=provider,
            index_symbol=_INDEX,
            evidence_start=date(2020, 1, 2),
            requested_start=date(2020, 1, 1),
            requested_end=date(2020, 1, 10),
            dataset_version=_DATASET,
            entitlement_scope=_ENTITLEMENT,
            entitlement_owner_user_id=_OWNER,
        )
