"""Shared fixtures and persistence helpers for campaign contract tests."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from dev_cli.validation import campaign_contracts as campaign_module
from dev_cli.validation import campaign_environment as campaign_environment_module
from dev_cli.validation.backtest.engine import (
    BacktestConfig,
    BacktestReport,
    RawSignalEvidence,
)
from dev_cli.validation.backtest.metrics import compute_metrics
from dev_cli.validation.backtest.simulator import TerminalPosition, Trade
from dev_cli.validation.backtest.walk_forward import FoldOOSSeries, pool_fold_oos_returns
from dev_cli.validation.campaign import StrategyValidationCampaign
from dev_cli.validation.correctness import (
    CorrectnessContentClassification,
    CorrectnessFileBinding,
    CorrectnessFileClass,
    CorrectnessFindingClass,
    CorrectnessFindingSeverity,
    CorrectnessFindingStatus,
    CorrectnessReviewer,
    CorrectnessSemanticFinding,
    build_strategy_correctness_attestation,
    verify_registered_strategy_correctness_attestation,
)
from dev_cli.validation.persistence.backtest_manifest_store import (
    manifest_sha256,
)
from dev_cli.validation.persistence.backtest_result_store import (
    DerivedBacktestEvidence,
    DerivedEvidenceMetrics,
)
from lib_application.db.models import (
    BacktestExperiment,
    BacktestResult,
    BacktestTrial,
    Base,
    Broker,
    LinkedBrokerAccount,
    Strategy,
    StrategyVersion,
    User,
    UserStrategyBinding,
    UserStrategyConfig,
)
from lib_strategy.signals.loading import (
    load_pure_strategy_core,
)
from lib_strategy.signals.signal import Signal, SignalAction

_REPO = Path(__file__).resolve().parents[3]

_REGISTERED_CAMPAIGN = _REPO / "tests/fixtures/strategy_validation/registered_campaign"


def _load_registered_protocol() -> dict[str, Any]:
    """Load the tracked contract only after verifying its exact source bindings."""

    protocol_path = _REGISTERED_CAMPAIGN / "validation_protocol.json"
    protocol: dict[str, Any] = json.loads(protocol_path.read_text())
    for binding in protocol["correctness_attestation"]["files"]:
        source_path = (_REPO / str(binding["location"])).resolve()
        source_path.relative_to(_REPO.resolve())
        payload = source_path.read_bytes()
        assert len(payload) == int(binding["byte_count"]), binding["location"]
        assert hashlib.sha256(payload).hexdigest() == binding["sha256"], binding["location"]
    return protocol


@lru_cache(maxsize=1)
def _cached_registered_correctness_evidence() -> tuple[dict[str, object], dict[str, object]]:
    contract = _load_registered_protocol()["correctness_attestation"]
    bindings = tuple(
        CorrectnessFileBinding(
            binding_id=str(row["id"]),
            file_class=CorrectnessFileClass(str(row["class"])),
            content_classification=CorrectnessContentClassification(
                str(row["content_classification"])
            ),
            path=_REPO / str(row["location"]),
            location=str(row["location"]),
        )
        for row in contract["files"]
    )
    findings = tuple(
        CorrectnessSemanticFinding(
            finding_id=str(row["id"]),
            authority=str(row["authority"]),
            current_behavior=str(row["current_behavior"]),
            authoritative_behavior=str(row["authoritative_behavior"]),
            severity=CorrectnessFindingSeverity(str(row["severity"])),
            status=CorrectnessFindingStatus(str(row["status"])),
            finding_class=CorrectnessFindingClass(str(row["finding_class"])),
            remediation=str(row["remediation"]),
            evidence_file_ids=tuple(str(value) for value in row["evidence_file_ids"]),
        )
        for row in contract["findings"]
    )
    reviewer_row = contract["reviewer"]
    reviewer = CorrectnessReviewer(
        identity=str(reviewer_row["identity"]),
        role=str(reviewer_row["role"]),
        independent=bool(reviewer_row["independent"]),
        reviewed_at_utc=datetime.fromisoformat(str(reviewer_row["reviewed_at_utc"])),
    )
    subject = contract["subject"]
    payload = build_strategy_correctness_attestation(
        strategy_id=str(subject["strategy_id"]),
        strategy_version=str(subject["strategy_version"]),
        files=bindings,
        findings=findings,
        reviewer=reviewer,
    )
    return payload, contract


def _registered_correctness_evidence() -> tuple[dict[str, object], dict[str, object]]:
    payload, contract = _cached_registered_correctness_evidence()
    return deepcopy(payload), deepcopy(contract)


def _runtime_distribution_environment_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    rows = ("test-runtime==1.0",)
    digest = hashlib.sha256(rows[0].encode("utf-8")).hexdigest()
    monkeypatch.setattr(
        campaign_environment_module,
        "_runtime_distribution_snapshot",
        lambda: (rows, digest),
    )
    return {
        "runtime_distributions": list(rows),
        "runtime_distribution_lock_sha256": digest,
    }


def _engine() -> Engine:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Strategy(
                strategy_id="registered_campaign_v1",
                strategy_name="RegisteredCampaign",
                asset_class="crypto",
            )
        )
        session.flush()
        session.add_all(
            [
                StrategyVersion(
                    strategy_id="registered_campaign_v1",
                    semver="1.0.0",
                    param_schema={},
                    default_params={},
                ),
                User(
                    user_id="validation-user",
                    email="validation-user@example.invalid",
                    base_ccy="USD",
                    status="active",
                ),
                Broker(
                    broker_id=1,
                    code="paper",
                    name="Validation paper broker",
                    capabilities={"spot": True, "paper": True},
                ),
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="validation-user",
                    broker_id=1,
                    environment="paper",
                    display_name="Validation paper account",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                UserStrategyBinding(
                    binding_id=1,
                    user_id="validation-user",
                    strategy_id="registered_campaign_v1",
                    broker_account_id=1,
                    execution_modes_allowed=["spot"],
                    preferred_mode="spot",
                    is_active=False,
                    autopilot=False,
                ),
                UserStrategyConfig(
                    config_id="validation-campaign-config",
                    user_id="validation-user",
                    strategy_id="registered_campaign_v1",
                    execution_mode="paper",
                    is_active=True,
                    parameters={
                        "require_explicit_scoring_inputs": True,
                        "require_stop_loss": True,
                    },
                ),
            ]
        )
        session.commit()
    return engine


def _protocol() -> dict[str, Any]:
    return _load_registered_protocol()


def _unit_evidence(kind: str) -> dict[str, object]:
    """Return a pure orchestration contract with no provider or market claim."""

    payload: dict[str, object] = {
        "schema_id": "vynmatrix.validation-unit-evidence",
        "schema_version": 1,
        "kind": kind,
        "scope": "orchestration_only",
        "market_evidence": False,
    }
    payload["evidence_sha256"] = manifest_sha256(payload)
    return payload


def _verify_unit_evidence(payload: Mapping[str, object], *, kind: str) -> None:
    expected = _unit_evidence(kind)
    if payload != expected:
        message = f"{kind} unit evidence differs from its immutable contract"
        raise ValueError(message)


def _unit_upstream_selection_ledger(protocol: dict[str, Any]) -> dict[str, Any]:
    """Build a unit-only selection contract; it is not market evidence."""

    contract = protocol["multiple_testing"]["upstream_selection_ledger_contract"]
    identities = (
        ("first.py", "First", "BTC-USD", "1d", "ok", "-1"),
        ("second.py", "Second", "SPY", "1d", "ok", "1"),
        ("third.py", "Third", "BTC-USD", "1d", "error", None),
        ("fourth.py", "Fourth", "SPY", "1d", "timeout", None),
    )
    return {
        "schema_version": "1.0",
        "source": {
            "filename": "unit_contract_no_market_evidence.csv",
            "sha256": "f" * 64,
            "format": "rfc4180_csv_utf8",
            "columns": contract["required_columns"],
        },
        "normalization": {
            "identity_fields": ["python_file", "class_name", "symbol", "timeframe"],
            "status_field": "status",
            "sharpe_field": "sharpe_daily_ann",
            "finite_inclusion_policy": "all_nonblank_finite_sharpe_values",
            "non_ok_sharpe_must_be_blank": True,
            "sample_standard_deviation_ddof": 1,
            "summary_decimal_places": 15,
        },
        "summary": {
            "row_count": 4,
            "status_counts": {"error": 1, "ok": 2, "timeout": 1},
            "finite_sharpe_count": 2,
            "finite_sharpe_mean": "0.000000000000000",
            "finite_sharpe_sample_std": "1.414213562373095",
        },
        "trials": [
            {
                "sequence": sequence,
                "python_file": python_file,
                "class_name": class_name,
                "symbol": symbol,
                "timeframe": timeframe,
                "status": status,
                "sharpe_daily_ann": sharpe,
            }
            for sequence, (python_file, class_name, symbol, timeframe, status, sharpe) in enumerate(
                identities
            )
        ],
    }


def _manifest(protocol: dict[str, Any]) -> dict[str, Any]:
    runtime_config_path = _REGISTERED_CAMPAIGN / "config.json"
    core_path = _REGISTERED_CAMPAIGN / "core.py"
    datasets = [
        {
            "dataset_id": "BTC-USDC:coinbase_validation_v1:1d:2019-10-01:2026-01-01",
            "instrument_id": 1,
            "requested_product": "BTC-USDC",
            "canonical_product": "BTC-USD",
            "source": "coinbase_validation_v1",
            "timeframe": "1d",
            "asset_class": "crypto",
            "start": "2019-10-01T00:00:00+00:00",
            "end_exclusive": "2026-01-01T00:00:00+00:00",
            "minimum_coverage": 1.0,
            "metadata_snapshot": {
                "base_increment": "0.00000001",
                "quote_increment": "0.01",
            },
            "audit": {"ohlcv_sha256": "a" * 64},
        },
        {
            "dataset_id": "ETH-USDC:coinbase_validation_v1:1d:2019-10-01:2026-01-01",
            "instrument_id": 2,
            "requested_product": "ETH-USDC",
            "canonical_product": "ETH-USD",
            "source": "coinbase_validation_v1",
            "timeframe": "1d",
            "asset_class": "crypto",
            "start": "2019-10-01T00:00:00+00:00",
            "end_exclusive": "2026-01-01T00:00:00+00:00",
            "minimum_coverage": 1.0,
            "metadata_snapshot": {
                "base_increment": "0.00000001",
                "quote_increment": "0.01",
            },
            "audit": {"ohlcv_sha256": "b" * 64},
        },
    ]
    validator = object.__new__(StrategyValidationCampaign)
    trials = validator._build_trial_registry(protocol, datasets)
    correctness_payload, correctness_contract = _registered_correctness_evidence()
    if correctness_contract != protocol["correctness_attestation"]:
        raise AssertionError("registered correctness contract differs from tracked protocol")
    correctness_decision = verify_registered_strategy_correctness_attestation(
        correctness_payload,
        protocol["correctness_attestation"],
    )
    return {
        "manifest_schema_version": "1.4",
        "status": "frozen",
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": manifest_sha256(protocol),
        "strategy": {
            "strategy_id": "registered_campaign_v1",
            "strategy_version": "1.0.0",
            "core_class": "RegisteredCampaignCore",
            "core_path": "tests/fixtures/strategy_validation/registered_campaign/core.py",
            "core_sha256": StrategyValidationCampaign._file_sha256(core_path),
            "runtime_config_path": "tests/fixtures/strategy_validation/registered_campaign/config.json",
            "runtime_config_sha256": StrategyValidationCampaign._file_sha256(runtime_config_path),
            "protocol_path": (
                "tests/fixtures/strategy_validation/registered_campaign/validation_protocol.json"
            ),
        },
        "data": {"datasets": datasets},
        "data_parity_contract": protocol["data"]["minute_parity"],
        "data_parity_attestation": _unit_evidence("data_parity"),
        "correctness_attestation_contract": protocol["correctness_attestation"],
        "correctness_attestation": correctness_payload,
        "correctness_attestation_decision": correctness_decision.to_dict(),
        "cost_measurement": protocol["cost_measurement"],
        "execution_cost_measurement": _unit_evidence("execution_cost"),
        "cost_scenarios": protocol["cost_scenarios"],
        "execution_policy": protocol["execution_policy"],
        "validation_design": protocol["validation_design"],
        "evidence_boundary": protocol["evidence_boundary"],
        "multiple_testing": protocol["multiple_testing"],
        "inference": protocol["inference"],
        "upstream_selection_ledger": _unit_upstream_selection_ledger(protocol),
        "historical_disposition_gates": protocol["historical_disposition_gates"],
        "historical_disposition_policy": protocol["historical_disposition_policy"],
        "allowed_dispositions": protocol["allowed_dispositions"],
        "promotion": protocol["promotion"],
        "portfolio_assumptions": protocol["portfolio_assumptions"],
        "habitat": {**protocol["habitat"], "asset_class_db": "crypto"},
        "environment": {"execution_installation": "test-orchestration-only"},
        "operational_binding_snapshot": {
            "schema_version": "1.0",
            "thresholds_included": False,
            "thresholds_evaluated": False,
            "bindings": [
                {
                    "binding_id": "1",
                    "strategy_config_id": "validation-campaign-config",
                    "strategy_id": "registered_campaign_v1",
                    "active": False,
                    "autopilot": False,
                    "approval_mode": "manual_approval",
                    "execution_modes_allowed": ["spot"],
                    "preferred_mode": "spot",
                    "strategy_config_active": True,
                    "execution_mode": "paper",
                    "require_stop_loss": True,
                    "require_explicit_scoring_inputs": True,
                }
            ],
        },
        "trial_registry": trials,
    }


def _runner_counts(manifest: dict[str, Any]) -> Counter[str]:
    return Counter(row["runner_kind"] for row in manifest["trial_registry"])


def _family_counts(manifest: dict[str, Any]) -> Counter[str]:
    return Counter(row["trial_family"] for row in manifest["trial_registry"])


def _abandoned_count(manifest: dict[str, Any]) -> int:
    return sum(row.get("registration_status") == "abandoned" for row in manifest["trial_registry"])


def _campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    allow_execution: bool = True,
) -> tuple[StrategyValidationCampaign, dict[str, Any]]:
    protocol = _protocol()
    manifest = _manifest(protocol)
    config = json.loads((_REGISTERED_CAMPAIGN / "config.json").read_text())
    campaign = StrategyValidationCampaign(
        _engine(),
        repo_root=_REPO,
        artifact_root=tmp_path / ".artifacts",
        execution_cost_measurement_verifier=lambda _payload: None,
        data_parity_attestation_verifier=lambda _payload: None,
    )

    def load_json(path: Path) -> dict[str, Any]:
        return protocol if path.name == "validation_protocol.json" else config

    monkeypatch.setattr(campaign, "_load_json_object", load_json)
    monkeypatch.setattr(campaign, "_build_manifest", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(campaign, "_revalidate_datasets", lambda *_args: None)
    monkeypatch.setattr(campaign, "_revalidate_product_metadata", lambda *_args: None)

    def validate_unit_cost(
        frozen_manifest: Mapping[str, object],
        **_kwargs: object,
    ) -> None:
        _verify_unit_evidence(
            frozen_manifest["execution_cost_measurement"],
            kind="execution_cost",
        )

    def validate_unit_parity(
        frozen_manifest: Mapping[str, object],
        *,
        current_source_required: bool = True,
        **_kwargs: object,
    ) -> None:
        payload = frozen_manifest["data_parity_attestation"]
        _verify_unit_evidence(payload, kind="data_parity")
        if current_source_required:
            campaign._data_parity_attestation_verifier(payload)

    monkeypatch.setattr(campaign, "_validate_manifest_cost_provenance", validate_unit_cost)
    monkeypatch.setattr(campaign, "_validate_manifest_data_parity_provenance", validate_unit_parity)
    if allow_execution:
        monkeypatch.setattr(
            campaign,
            "_require_frozen_runtime_environment",
            lambda *_args: {},
        )
        monkeypatch.setattr(campaign, "_require_execution_environment", lambda *_args: None)
        monkeypatch.setattr(
            campaign,
            "_load_installed_core",
            lambda *_args: load_pure_strategy_core(
                _REGISTERED_CAMPAIGN,
                expected_class_name="RegisteredCampaignCore",
            ),
        )
    return campaign, manifest


def _store_content_addressed_protocol(tmp_path: Path, protocol: Mapping[str, Any]) -> Path:
    payload = json.dumps(protocol, indent=2, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    path = (
        tmp_path
        / ".artifacts"
        / "research"
        / "protocols"
        / "sha256"
        / digest[:2]
        / f"{digest}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _registered_retired_derived_trial(
    campaign: StrategyValidationCampaign,
    manifest: dict[str, Any],
) -> tuple[str, int, str]:
    retired_runner = "retired_strategy_specific_diagnostic"
    retired = deepcopy(manifest["trial_registry"][0])
    retired.update(
        {
            "arm_id": "RETIRED_DIAGNOSTIC",
            "evidence_role": "retired_historical_diagnostic_only",
            "parameters": {"frozen_retired_contract": "v1"},
            "runner_kind": retired_runner,
            "trial_family": "retired_strategy_specific_family",
        }
    )
    manifest["trial_registry"].insert(-1, retired)
    for sequence, spec in enumerate(manifest["trial_registry"]):
        spec["sequence"] = sequence
    reference = campaign._manifest_store.store(manifest)
    experiment_id = campaign._get_or_create_experiment(manifest, reference.sha256)
    trial_ids = campaign._register_all_trials(
        experiment_id=experiment_id,
        manifest_hash=reference.sha256,
        manifest=manifest,
        trial_specs=manifest["trial_registry"],
    )
    retired_sequence = len(manifest["trial_registry"]) - 2
    retired_trial_id = trial_ids[retired_sequence]
    campaign._trial_store.mark_running(retired_trial_id)
    campaign._result_store.save_derived_evidence(
        DerivedBacktestEvidence(
            strategy_id="registered_campaign_v1",
            strategy_semver="1.0.0",
            experiment_id=experiment_id,
            evidence_kind=retired_runner,
            symbol="BTC-USDC",
            asset_class="crypto",
            timeframe="1440m",
            start_date=datetime(2020, 1, 2, tzinfo=UTC).date(),
            end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
            payload={
                "historical_only": True,
                "runner_kind": retired_runner,
            },
            metrics=DerivedEvidenceMetrics(),
        ),
        trial_id=retired_trial_id,
    )
    campaign._refresh_experiment_summary(experiment_id)
    return reference.sha256, experiment_id, retired_trial_id


def _complete_retired_disposition(
    campaign: StrategyValidationCampaign,
    manifest: dict[str, Any],
    *,
    experiment_id: int,
) -> str:
    final_spec = manifest["trial_registry"][-1]
    with Session(campaign._engine) as session:
        final_trial = session.execute(
            select(BacktestTrial).where(
                BacktestTrial.experiment_id == experiment_id,
                BacktestTrial.sequence == final_spec["sequence"],
            )
        ).scalar_one()
        final_trial_id = final_trial.trial_id
    campaign._trial_store.mark_running(final_trial_id)
    campaign._result_store.save_derived_evidence(
        DerivedBacktestEvidence(
            strategy_id="registered_campaign_v1",
            strategy_semver="1.0.0",
            experiment_id=experiment_id,
            evidence_kind="historical_strategy_disposition",
            symbol="BTC-USDC+ETH-USDC",
            asset_class="crypto",
            timeframe="1440m",
            start_date=datetime(2022, 1, 1, tzinfo=UTC).date(),
            end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
            payload={
                "authority": {
                    "automatic_parameter_deployment_authorized": False,
                    "live_trading_authorized": False,
                    "paper_trading_authorized": False,
                    "prospective_shadow_research_eligible": False,
                },
                "blocker_codes": [],
                "completion_state": "COMPLETE",
                "economic_disposition": "RETIRE",
                "frozen_arm_id": None,
                "reason_codes": ["historical_rejection"],
            },
            metrics=DerivedEvidenceMetrics(),
        ),
        trial_id=final_trial_id,
    )
    campaign._refresh_experiment_summary(experiment_id)
    return final_trial_id


@dataclass(frozen=True)
class _UnitOrchestrationWindow:
    fold: Any
    selected_registry_index: int
    equal_weight_oos_series: FoldOOSSeries
    oos_asset_reports: tuple[Any, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold_index": self.fold.index,
            "test_start": self.fold.test_start_ts.isoformat(),
            "test_end_exclusive": self.fold.test_end_exclusive_ts.isoformat(),
            "selected_registry_index": self.selected_registry_index,
            "equity_curve": [
                [timestamp.isoformat(), equity]
                for timestamp, equity in self.equal_weight_oos_series.equity_curve
            ],
        }


@dataclass(frozen=True)
class _UnitOrchestrationResult:
    asset_order: tuple[str, ...]
    windows: tuple[_UnitOrchestrationWindow, ...]
    pooled_oos: Any


def _unit_orchestration_result() -> _UnitOrchestrationResult:
    """Return a flat, zero-activity result for persistence orchestration tests.

    This object deliberately carries no invented return, signal, or trade
    evidence. Market-dependent behavior is covered by hash-pinned provider
    fixtures in the provider and reference-execution integration tests.
    """

    windows: list[_UnitOrchestrationWindow] = []
    series: list[FoldOOSSeries] = []
    for index, fold in enumerate(_protocol()["folds"]):
        start = datetime.fromisoformat(str(fold["test_start"]))
        end = datetime.fromisoformat(str(fold["test_end_exclusive"]))
        timestamps = tuple(start + timedelta(days=offset) for offset in range(3))
        curve = tuple((timestamp, 1.0) for timestamp in timestamps)
        fold_series = FoldOOSSeries(
            fold_index=index,
            expected_timestamps=timestamps,
            equity_curve=curve,
            initial_capital=1.0,
        )
        series.append(fold_series)
        report = SimpleNamespace(
            metrics=SimpleNamespace(total_trades=0),
            signals_emitted=0,
        )
        windows.append(
            _UnitOrchestrationWindow(
                fold=SimpleNamespace(
                    index=index,
                    test_start_ts=start,
                    test_end_exclusive_ts=end,
                ),
                selected_registry_index=0,
                equal_weight_oos_series=fold_series,
                oos_asset_reports=(
                    SimpleNamespace(report=report),
                    SimpleNamespace(report=report),
                ),
            )
        )
    return _UnitOrchestrationResult(
        asset_order=("BTC-USDC", "ETH-USDC"),
        windows=tuple(windows),
        pooled_oos=pool_fold_oos_returns(series, annualization_factor=365.0),
    )


def _stub_primary_execution(
    campaign: StrategyValidationCampaign,
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: _UnitOrchestrationResult | None = None,
) -> list[int]:
    calls: list[int] = []
    frozen_result = result or _unit_orchestration_result()
    monkeypatch.setattr(
        campaign,
        "_prepare_primary_dependencies",
        lambda *_args, **_kwargs: [],
    )

    def execute_once(*_args: Any, **_kwargs: Any) -> tuple[Any, dict[str, Any]]:
        calls.append(1)
        return frozen_result, {
            "BTC-USDC": {"ohlcv_sha256": "a" * 64},
            "ETH-USDC": {"ohlcv_sha256": "b" * 64},
        }

    monkeypatch.setattr(campaign, "_run_primary_selector", execute_once)
    return calls


def _persist_scoring_source_folds(
    campaign: StrategyValidationCampaign,
    manifest: dict[str, Any],
    *,
    duplicate_ids: bool = False,
) -> None:
    source_specs = [
        spec
        for spec in manifest["trial_registry"]
        if spec["runner_kind"] == "production_core_direct"
        and spec["arm_id"] == manifest["historical_disposition_policy"]["baseline_arm_id"]
        and spec["cost_scenario"] == "expected"
        and spec["fold_id"] != "full-panel"
    ]
    trial_ids = _trial_ids_by_sequence(campaign)
    datasets = {str(dataset["dataset_id"]): dataset for dataset in manifest["data"]["datasets"]}
    for spec in source_specs:
        trial_id = trial_ids[int(spec["sequence"])]
        campaign._trial_store.mark_running(trial_id)
        timestamp = datetime.fromisoformat(str(spec["evaluation_start"]))
        dataset = datasets[str(spec["dataset_id"])]
        product = str(dataset["requested_product"])
        external_id = "duplicate-signal" if duplicate_ids else f"{spec['fold_id']}:{product}:long"
        raw = RawSignalEvidence.from_signal(
            Signal(
                signal_id=external_id,
                external_signal_id=external_id,
                strategy_id="registered_campaign_v1",
                strategy_type="indicator",
                symbol=product,
                action=SignalAction.LONG,
                confidence=0.7,
                timestamp=timestamp,
                entry_price=100.0,
                stop_loss=95.0,
                source="backtest",
            )
        )
        config = campaign._backtest_config(
            spec,
            dataset=dataset,
            manifest=manifest,
            fill_policy=campaign._fill_policy(spec, manifest=manifest),
        )
        curve = [(timestamp, config.initial_capital)]
        report = BacktestReport(
            config=config,
            metrics=compute_metrics(
                curve,
                [],
                initial_capital=config.initial_capital,
                annualization_factor=config.annualization_factor,
            ),
            trades=[],
            equity_curve=curve,
            signals_emitted=1,
            consolidated_bars=1,
            unfilled_signals=1,
            execution_policy_id=config.fill_policy.policy_id,
            cost_scenario="expected",
            bootstrap_bars=0,
            flat_model_boundary_applied=True,
            raw_signal_ledger=(raw,),
        )
        campaign._save_report_result(
            report,
            audit={"ohlcv_sha256": dataset["audit"]["ohlcv_sha256"]},
            spec=spec,
            trial_id=trial_id,
            manifest=manifest,
        )


def _persist_baseline_full_panel_reports(
    campaign: StrategyValidationCampaign,
    manifest: dict[str, Any],
    *,
    trades_by_product: dict[str, list[Trade]] | None = None,
    terminal_products: frozenset[str] = frozenset(),
) -> None:
    source_specs = [
        spec
        for spec in manifest["trial_registry"]
        if spec["runner_kind"] == "production_core_direct"
        and spec["arm_id"] == manifest["historical_disposition_policy"]["baseline_arm_id"]
        and spec["cost_scenario"] == "expected"
        and spec["fold_id"] == "full-panel"
    ]
    trial_ids = _trial_ids_by_sequence(campaign)
    datasets = {str(dataset["dataset_id"]): dataset for dataset in manifest["data"]["datasets"]}
    for spec in source_specs:
        dataset = datasets[str(spec["dataset_id"])]
        product = str(dataset["requested_product"])
        trades = list((trades_by_product or {}).get(product, []))
        config = campaign._backtest_config(
            spec,
            dataset=dataset,
            manifest=manifest,
            fill_policy=campaign._fill_policy(spec, manifest=manifest),
        )
        start = datetime.fromisoformat(str(spec["evaluation_start"]))
        end = datetime.fromisoformat(str(spec["evaluation_end_exclusive"])) - timedelta(days=1)
        curve = [(start, config.initial_capital), (end, config.initial_capital)]
        terminal_position = (
            TerminalPosition(
                symbol=product,
                side="long",
                quantity=1.0,
                entry_ts=end - timedelta(days=10),
                entry_price=100.0,
                mark_price=100.0,
                gross_unrealized_pnl=0.0,
                net_unrealized_pnl=0.0,
                stop_loss=90.0,
                take_profit=None,
                holding_bars=10,
            )
            if product in terminal_products
            else None
        )
        report = BacktestReport(
            config=config,
            metrics=compute_metrics(
                curve,
                [trade.pnl for trade in trades],
                initial_capital=config.initial_capital,
                annualization_factor=config.annualization_factor,
            ),
            trades=trades,
            equity_curve=curve,
            signals_emitted=0,
            consolidated_bars=len(curve),
            unfilled_signals=0,
            execution_policy_id=config.fill_policy.policy_id,
            cost_scenario="expected",
            bootstrap_bars=0,
            flat_model_boundary_applied=True,
            terminal_position=terminal_position,
        )
        trial_id = trial_ids[int(spec["sequence"])]
        campaign._trial_store.mark_running(trial_id)
        campaign._save_report_result(
            report,
            audit={"ohlcv_sha256": dataset["audit"]["ohlcv_sha256"]},
            spec=spec,
            trial_id=trial_id,
            manifest=manifest,
        )


def _persist_robustness_gate(
    campaign: StrategyValidationCampaign,
    manifest: dict[str, Any],
    *,
    experiment_id: int,
    failed_gate_ids: tuple[str, ...] = (),
    missing_evidence_ids: tuple[str, ...] = (),
    raw_baseline_zero_trades_or_exposure: bool = False,
) -> None:
    spec = next(
        spec
        for spec in manifest["trial_registry"]
        if spec["runner_kind"] == "robustness_concentration_inference"
    )
    checks = {
        gate_id: gate_id not in failed_gate_ids for gate_id in campaign_module._SIZING_RAW_GATE_IDS
    }
    trial_id = _trial_ids_by_sequence(campaign)[int(spec["sequence"])]
    with Session(campaign._engine) as session:
        completed_report_trial_ids = [
            str(row.trial_id)
            for row in session.query(BacktestTrial)
            if row.status == "completed"
            and row.result_id is not None
            and session.get(BacktestResult, row.result_id).engine == "internal"
        ]
    source_attestation = campaign._normalized_source_attestation(
        report_sources=[
            {
                "trial_id": source_trial_id,
                "report_sha256": campaign._result_store.verified_report_sha256(source_trial_id),
            }
            for source_trial_id in completed_report_trial_ids
        ],
        dataset_sources=[
            {
                "dataset_id": str(dataset["dataset_id"]),
                "ohlcv_sha256": str(dataset["audit"]["ohlcv_sha256"]),
            }
            for dataset in manifest["data"]["datasets"]
        ],
    )
    campaign._trial_store.mark_running(trial_id)
    start_date, end_date = campaign._evidence_dates(spec)
    campaign._result_store.save_derived_evidence(
        DerivedBacktestEvidence(
            strategy_id="registered_campaign_v1",
            strategy_semver="1.0.0",
            experiment_id=experiment_id,
            evidence_kind="robustness_concentration_inference",
            symbol="BTC-USDC+ETH-USDC",
            asset_class="crypto",
            timeframe="1440m",
            start_date=start_date,
            end_date=end_date,
            payload={
                "blocking_quantitative_gate_ids": list(failed_gate_ids),
                "disposition_source_arm_id": manifest["historical_disposition_policy"][
                    "baseline_arm_id"
                ],
                "evidence_complete": not missing_evidence_ids,
                "missing_evidence_ids": list(missing_evidence_ids),
                "quantitative_gate_checks": checks,
                "quantitative_robustness_gate_eligible": not failed_gate_ids,
                "raw_baseline_zero_trades_or_exposure": raw_baseline_zero_trades_or_exposure,
                "runner_kind": "robustness_concentration_inference",
                "source_attestation": source_attestation,
            },
            metrics=DerivedEvidenceMetrics(),
        ),
        trial_id=trial_id,
    )


@dataclass(frozen=True)
class _Audit:
    ohlcv_sha256: str = "a" * 64

    def to_dict(self) -> dict[str, str]:
        return {"ohlcv_sha256": self.ohlcv_sha256}


def _stub_benchmark_component_execution(
    campaign: StrategyValidationCampaign,
    manifest: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    calls: list[int] = []
    datasets = {str(dataset["dataset_id"]): dataset for dataset in manifest["data"]["datasets"]}

    def execute(
        spec: dict[str, Any],
        *,
        manifest: dict[str, Any],
    ) -> tuple[BacktestReport, dict[str, Any]]:
        calls.append(int(spec["sequence"]))
        dataset = datasets[str(spec["dataset_id"])]
        config: BacktestConfig = campaign._backtest_config(
            spec,
            dataset=dataset,
            manifest=manifest,
            fill_policy=campaign._fill_policy(spec, manifest=manifest),
        )
        curve = [
            (timestamp, config.initial_capital)
            for timestamp in campaign._expected_daily_timestamps(
                spec,
                manifest=manifest,
            )
        ]
        report = BacktestReport(
            config=config,
            metrics=compute_metrics(
                curve,
                [],
                initial_capital=config.initial_capital,
                annualization_factor=config.annualization_factor,
            ),
            trades=[],
            equity_curve=curve,
            signals_emitted=0,
            consolidated_bars=len(curve),
            unfilled_signals=0,
            execution_policy_id=config.fill_policy.policy_id,
            cost_scenario=str(spec["cost_scenario"]),
            flat_model_boundary_applied=True,
        )
        component = spec["parameters"]["component_dataset"]
        return report, {
            "benchmark": {
                "benchmark_kind": spec["parameters"]["benchmark_kind"],
                "decision_timing": "completed_bar_decision_next_tradable_open",
            },
            "dataset": {
                "dataset_id": component["dataset_id"],
                "ohlcv_sha256": component["ohlcv_sha256"],
            },
        }

    monkeypatch.setattr(campaign, "_execute_benchmark_component", execute)
    return calls


def _trial_ids_by_sequence(campaign: StrategyValidationCampaign) -> dict[int, str]:
    with Session(campaign._engine) as session:
        return {row.sequence: row.trial_id for row in session.query(BacktestTrial).all()}


def _cash_expected_benchmark_specs(
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        spec
        for spec in manifest["trial_registry"]
        if spec["arm_id"] == "BENCHMARK_CASH"
        and spec["cost_scenario"] == "expected"
        and spec["runner_kind"]
        in {"equal_weight_benchmark_portfolio", "pooled_oos_benchmark_aggregate"}
    ]


def _minimal_historical_audit_registry(
    campaign: StrategyValidationCampaign,
    *,
    prior_runner_kind: str = "scoring_binding_ledger_pooled",
) -> tuple[
    int,
    list[dict[str, Any]],
    dict[int, str],
    dict[str, Any],
]:
    manifest = {
        "protocol_id": "generic-historical-audit-v1",
        "strategy": {
            "strategy_id": "registered_campaign_v1",
            "strategy_version": "1.0.0",
        },
        "data": {"datasets": []},
        "habitat": {"asset_class_db": "crypto"},
    }
    with Session(campaign._engine) as session:
        experiment = BacktestExperiment(
            strategy_id="registered_campaign_v1",
            experiment_type="parameter_sweep",
            metric="integrity",
            status="running",
            config={"manifest_hash": "f" * 64},
            started_at=datetime.now(tz=UTC),
        )
        session.add(experiment)
        session.commit()
        experiment_id = int(experiment.experiment_id)
    specs = [
        {
            "sequence": 0,
            "arm_id": "SCORING",
            "trial_family": "pipeline_reconciliation",
            "runner_kind": prior_runner_kind,
            "fold_id": "pooled-oos",
            "cost_scenario": "expected",
            "parameters": {},
        },
        {
            "sequence": 1,
            "arm_id": "HISTORICAL_DISPOSITION",
            "trial_family": "historical_disposition",
            "runner_kind": "historical_strategy_disposition",
            "fold_id": "historical-disposition",
            "cost_scenario": "not_applicable",
            "parameters": {},
        },
    ]
    trial_ids = campaign._register_all_trials(
        experiment_id=experiment_id,
        manifest_hash="f" * 64,
        manifest=manifest,
        trial_specs=specs,
    )
    return experiment_id, specs, trial_ids, manifest


def _complete_minimal_scoring_trial(
    campaign: StrategyValidationCampaign,
    *,
    experiment_id: int,
    trial_id: str,
) -> None:
    campaign._trial_store.mark_running(trial_id)
    campaign._result_store.save_derived_evidence(
        DerivedBacktestEvidence(
            strategy_id="registered_campaign_v1",
            strategy_semver="1.0.0",
            experiment_id=experiment_id,
            evidence_kind="scoring_binding_ledger_pooled",
            symbol="BTC-USDC",
            asset_class="crypto",
            timeframe="1440m",
            start_date=datetime(2022, 1, 1, tzinfo=UTC).date(),
            end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
            payload={
                "runner_kind": "scoring_binding_ledger_pooled",
                "coverage": {
                    "source_signals": 0,
                    "replayed_signals": 0,
                    "missing_signals": 0,
                    "duplicate_signals": 0,
                },
            },
            metrics=DerivedEvidenceMetrics(total_signals=0),
        ),
        trial_id=trial_id,
    )
