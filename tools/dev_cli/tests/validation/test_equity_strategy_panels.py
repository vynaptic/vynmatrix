"""Historical synchronized-panel artifact and runner policy tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from dev_cli.validation.backtest import equity_run as equity_run_module
from dev_cli.validation.backtest.equity_portfolio import (
    DailyBar,
    EquityDataset,
    PortfolioConfig,
)
from dev_cli.validation.backtest.equity_run import _run_panel_backtest
from dev_cli.validation.backtest.equity_run_cli import build_argument_parser
from dev_cli.validation.backtest.equity_run_inputs import EquityRunError
from dev_cli.validation.backtest.equity_snapshot import StrategyIdentity, ValidationProvider
from dev_cli.validation.backtest.equity_strategy_panels import (
    STRATEGY_PANEL_ARTIFACT_ROLE,
    STRATEGY_PANEL_MANIFEST_SCHEMA,
    STRATEGY_PANEL_MEDIA_TYPE,
    EquityStrategyPanelError,
    compose_strategy_panel_dataset,
    strategy_panel_artifact_context,
    write_strategy_panel_manifest,
)
from dev_cli.validation.evidence import (
    canonical_json_bytes,
    store_content_object,
    write_content_addressed_manifest,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.panels import (
    EffectivePanelMember,
    OfficialSessionCutoff,
    PanelObservationRef,
    PanelReadyInput,
    SessionAuthority,
    panel_ready_input_from_payload,
    panel_ready_input_to_payload,
)
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy

_SNAPSHOT_SHA256 = canonical_json_hash({"snapshot": "panel-contract-test"})
_STRATEGY_IDENTITY = StrategyIdentity(
    strategy_id="panel_contract_test_v1",
    strategy_version="1.0.0",
    algorithm_type_name="_PanelStrategy",
    relative_path="tests/fixtures/panel_contract_test",
    config_sha256=canonical_json_hash({"config": "panel-contract-test"}),
    source_tree_sha256=canonical_json_hash({"source": "panel-contract-test"}),
)
_POLICY = ProviderAuthorityPolicy(
    policy_version="historical-panel-contract-test-v1",
    data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
    rules=(
        ProviderAuthorityRule(
            provider="sec",
            decision=ProviderAuthorityDecision.ALLOW,
            entitlement_scopes=("public_filings",),
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class _StrategyPanelInput:
    panel: PanelReadyInput


class _PanelStrategy(PureSignalStrategy):
    def __init__(self, **_kwargs: Any) -> None:
        super().__init__(
            strategy_id=_STRATEGY_IDENTITY.strategy_id,
            strategy_type="indicator",
            config={"strategy_version": _STRATEGY_IDENTITY.strategy_version},
        )

    def initialize(self) -> None:
        self.warmup_bars_needed = 0

    def on_data(self, state: MarketState) -> None:
        del state

    def panel_ready_input(self, panel_input: object) -> PanelReadyInput:
        if not isinstance(panel_input, _StrategyPanelInput):
            raise TypeError("unexpected test panel input")
        return panel_input.panel

    def serialize_panel_input(self, panel_input: object) -> Mapping[str, Any]:
        return {
            "panel": panel_ready_input_to_payload(self.panel_ready_input(panel_input)),
            "schema": "historical-panel-contract-test-v1",
        }

    def deserialize_panel_input(self, payload: Mapping[str, Any]) -> object:
        if set(payload) != {"panel", "schema"}:
            raise ValueError("unexpected test panel fields")
        if payload.get("schema") != "historical-panel-contract-test-v1":
            raise ValueError("unexpected test panel schema")
        raw_panel = payload.get("panel")
        if not isinstance(raw_panel, Mapping):
            raise TypeError("test panel payload must be an object")
        return _StrategyPanelInput(panel_ready_input_from_payload(raw_panel))

    def evaluate_panel(self, panel_input: object) -> None:
        self.panel_ready_input(panel_input)


def _official_session(session_date: date) -> OfficialSessionCutoff:
    opens_at = datetime.combine(session_date, time(14, 30), tzinfo=UTC)
    closes_at = datetime.combine(session_date, time(21), tzinfo=UTC)
    return OfficialSessionCutoff(
        mic="XNYS",
        session_date=session_date,
        opens_at=opens_at,
        closes_at=closes_at,
        authority=SessionAuthority.OFFICIAL_EXCHANGE,
        source_identity="exchange:nyse:panel-contract-test",
        content_sha256=canonical_json_hash(
            {
                "session_date": session_date.isoformat(),
                "opens_at": opens_at.isoformat(),
                "closes_at": closes_at.isoformat(),
            }
        ),
    )


def _panel_input(
    decision_session: OfficialSessionCutoff,
    execution_session: OfficialSessionCutoff,
) -> _StrategyPanelInput:
    observation_sha256 = canonical_json_hash(
        {"observation": decision_session.session_date.isoformat()}
    )
    return _StrategyPanelInput(
        PanelReadyInput(
            cutoff=decision_session.closes_at,
            session=decision_session,
            execution_session=execution_session,
            data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
            provider_authority_policy=_POLICY,
            provider_authority_sha256=_POLICY.digest,
            membership_sha256=canonical_json_hash(
                {"membership": decision_session.session_date.isoformat()}
            ),
            factor_snapshot_sha256=canonical_json_hash(
                {"factors": decision_session.session_date.isoformat()}
            ),
            members=(
                EffectivePanelMember(
                    security_id="US0378331005",
                    issuer_id="APPLE-INC",
                    instrument_id=1,
                    canonical_symbol="AAPL",
                ),
            ),
            observations=(
                PanelObservationRef(
                    security_id="US0378331005",
                    observation_id="panel-contract-observation",
                    observed_at=decision_session.closes_at - timedelta(minutes=1),
                    available_at=decision_session.closes_at,
                    content_revision=1,
                    content_sha256=observation_sha256,
                ),
            ),
        )
    )


def _bar(session: date) -> DailyBar:
    return DailyBar(
        session=session,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        raw_open=100.0,
        raw_high=101.0,
        raw_low=99.0,
        raw_close=100.0,
        split_adjusted_open=100.0,
        split_adjusted_high=101.0,
        split_adjusted_low=99.0,
        split_adjusted_close=100.0,
        split_adjusted_volume=1_000_000.0,
        split_adjustment_factor=1.0,
    )


def _dataset() -> tuple[EquityDataset, _StrategyPanelInput]:
    session_dates = (
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 5),
    )
    official_sessions = {item: _official_session(item) for item in session_dates}
    panel_input = _panel_input(
        official_sessions[session_dates[1]],
        official_sessions[session_dates[2]],
    )
    return (
        EquityDataset(
            sessions=list(session_dates),
            bars={"AAPL": [_bar(item) for item in session_dates]},
            benchmark_price=dict.fromkeys(session_dates, 100.0),
            benchmark_total_return=dict.fromkeys(session_dates, 100.0),
            vix=dict.fromkeys(session_dates, 15.0),
            earnings={"AAPL": ()},
            membership={"AAPL": ((session_dates[0], None),)},
            benchmark_total_return_kind="adjusted_security",
            benchmark_total_return_symbol="SPY",
            official_sessions=official_sessions,
        ),
        panel_input,
    )


def _write_panel_manifest(
    root: Path,
    panel_input: _StrategyPanelInput,
    *,
    row_count: int = 1,
) -> Path:
    payload = _PanelStrategy().serialize_panel_input(panel_input)
    if row_count == 1:
        return write_strategy_panel_manifest(
            root,
            historical_snapshot_manifest_sha256=_SNAPSHOT_SHA256,
            strategy_identity=_STRATEGY_IDENTITY,
            panel_payloads=(payload,),
            lineage={"fixture": "recorded-contract-shape"},
        ).manifest_path
    artifact = store_content_object(
        root,
        canonical_json_bytes(payload) + b"\n",
        suffix=".ndjson",
        role=STRATEGY_PANEL_ARTIFACT_ROLE,
        media_type=STRATEGY_PANEL_MEDIA_TYPE,
        row_count=row_count,
        context=strategy_panel_artifact_context(
            historical_snapshot_manifest_sha256=_SNAPSHOT_SHA256,
            strategy_identity=_STRATEGY_IDENTITY,
        ),
    )
    manifest, _digest = write_content_addressed_manifest(
        root,
        {
            "schema": STRATEGY_PANEL_MANIFEST_SCHEMA,
            "historical_snapshot_manifest_sha256": _SNAPSHOT_SHA256,
            "strategy": asdict(_STRATEGY_IDENTITY),
            "artifacts": [artifact.as_manifest()],
        },
    )
    return manifest


def test_compose_strategy_panel_dataset_loads_verified_codec_inputs(tmp_path: Path) -> None:
    dataset, panel_input = _dataset()
    manifest_path = _write_panel_manifest(tmp_path, panel_input)

    composed = compose_strategy_panel_dataset(
        dataset,
        artifact_root=tmp_path,
        manifest_path=manifest_path,
        historical_snapshot_manifest_sha256=_SNAPSHOT_SHA256,
        strategy_identity=_STRATEGY_IDENTITY,
        strategy=_PanelStrategy(),
    )

    assert composed.panel_inputs == {panel_input.panel.session.session_date: panel_input}
    assert composed.panel_manifest_sha256 == manifest_path.stem
    assert dataset.panel_inputs == {}


def test_compose_strategy_panel_dataset_records_resolved_manifest_digest(
    tmp_path: Path,
) -> None:
    dataset, panel_input = _dataset()
    manifest_path = _write_panel_manifest(tmp_path, panel_input)
    alias = manifest_path.parent / "latest.json"
    alias.symlink_to(manifest_path.name)

    composed = compose_strategy_panel_dataset(
        dataset,
        artifact_root=tmp_path,
        manifest_path=alias,
        historical_snapshot_manifest_sha256=_SNAPSHOT_SHA256,
        strategy_identity=_STRATEGY_IDENTITY,
        strategy=_PanelStrategy(),
    )

    assert composed.panel_manifest_sha256 == manifest_path.stem


def test_compose_strategy_panel_dataset_rejects_declared_row_count_mismatch(
    tmp_path: Path,
) -> None:
    dataset, panel_input = _dataset()
    manifest_path = _write_panel_manifest(tmp_path, panel_input, row_count=2)

    with pytest.raises(EquityStrategyPanelError, match="row count differs"):
        compose_strategy_panel_dataset(
            dataset,
            artifact_root=tmp_path,
            manifest_path=manifest_path,
            historical_snapshot_manifest_sha256=_SNAPSHOT_SHA256,
            strategy_identity=_STRATEGY_IDENTITY,
            strategy=_PanelStrategy(),
        )


def test_compose_strategy_panel_dataset_rejects_nonadjacent_execution_session(
    tmp_path: Path,
) -> None:
    dataset, panel_input = _dataset()
    delayed_panel = _panel_input(
        panel_input.panel.session,
        dataset.official_sessions[dataset.sessions[-1]],
    )
    manifest_path = _write_panel_manifest(tmp_path, delayed_panel)

    with pytest.raises(EquityStrategyPanelError, match="not the immediate official session"):
        compose_strategy_panel_dataset(
            dataset,
            artifact_root=tmp_path,
            manifest_path=manifest_path,
            historical_snapshot_manifest_sha256=_SNAPSHOT_SHA256,
            strategy_identity=_STRATEGY_IDENTITY,
            strategy=_PanelStrategy(),
        )


def test_panel_backtest_selects_synchronized_policies() -> None:
    dataset, panel_input = _dataset()
    dataset = EquityDataset(
        **{
            **dataset.__dict__,
            "panel_inputs": {panel_input.panel.session.session_date: panel_input},
        }
    )
    result = _run_panel_backtest(
        dataset,
        _PanelStrategy,
        PortfolioConfig(eval_start=dataset.sessions[1], eval_end=dataset.sessions[-1]),
    )

    assert result.policies["universe"] == "panel_supplied_pit_universe_v1"
    assert result.policies["cadence"] == "static_panel_execution_feed_v1"
    assert result.policies["panel_context"] == "synchronized_strategy_panel_v1"


def test_panel_backtest_fails_closed_without_evaluation_period_panel() -> None:
    dataset, _panel_input = _dataset()
    with pytest.raises(EquityRunError, match="immutable panel inputs"):
        _run_panel_backtest(
            dataset,
            _PanelStrategy,
            PortfolioConfig(eval_start=dataset.sessions[0], eval_end=dataset.sessions[-1]),
        )


def test_panel_backtest_rejects_window_that_omits_prior_panel_state() -> None:
    dataset, panel_input = _dataset()
    dataset = EquityDataset(
        **{
            **dataset.__dict__,
            "panel_inputs": {panel_input.panel.session.session_date: panel_input},
        }
    )

    with pytest.raises(EquityRunError, match="cannot omit an earlier synchronized panel"):
        _run_panel_backtest(
            dataset,
            _PanelStrategy,
            PortfolioConfig(eval_start=dataset.sessions[2], eval_end=dataset.sessions[-1]),
        )


def test_panel_backtest_rejects_window_before_panel_execution() -> None:
    dataset, panel_input = _dataset()
    dataset = EquityDataset(
        **{
            **dataset.__dict__,
            "panel_inputs": {panel_input.panel.session.session_date: panel_input},
        }
    )

    with pytest.raises(EquityRunError, match="executes after the evaluation end"):
        _run_panel_backtest(
            dataset,
            _PanelStrategy,
            PortfolioConfig(eval_start=dataset.sessions[0], eval_end=dataset.sessions[1]),
        )


def test_run_backtest_requires_panel_manifest_for_panel_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, _panel_input = _dataset()
    strategy_dir = tmp_path / "strategy"
    strategy_dir.mkdir()
    (strategy_dir / "config.json").write_text(
        '{"parameters":{"asset_class":"equity"},"strategy_version":"1.0.0"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        equity_run_module,
        "load_snapshot",
        lambda *_args, **_kwargs: (dataset, {}),
    )
    monkeypatch.setattr(
        equity_run_module,
        "_assert_run_matches_manifest",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        equity_run_module,
        "_strategy_identity",
        lambda _path: (_PanelStrategy, _STRATEGY_IDENTITY),
    )

    with pytest.raises(EquityRunError, match="requires --strategy-panel-manifest"):
        equity_run_module.run_backtest(
            tmp_path,
            tmp_path / "manifests" / f"{_SNAPSHOT_SHA256}.json",
            tmp_path / "out",
            strategy_dir=strategy_dir,
            provider=ValidationProvider.EODHD,
            eval_start=dataset.sessions[1],
            eval_end=dataset.sessions[-1],
        )


def test_run_cli_accepts_exact_strategy_panel_manifest_path(tmp_path: Path) -> None:
    manifest = tmp_path / "manifests" / f"{_SNAPSHOT_SHA256}.json"
    args = build_argument_parser().parse_args(
        [
            "run",
            "--snapshot",
            str(tmp_path),
            "--manifest",
            str(tmp_path / "snapshot-manifest.json"),
            "--strategy",
            str(tmp_path / "strategy"),
            "--provider",
            "eodhd",
            "--out",
            str(tmp_path / "out"),
            "--strategy-panel-manifest",
            str(manifest),
        ]
    )

    assert args.strategy_panel_manifest == manifest
