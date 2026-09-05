"""Fail-closed strategy-selection contracts for the indicator runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from indicator_runner import process_manager
from indicator_runner.main import StrategyRunnerApp
from indicator_runner.process_manager import (
    DEV_DISCOVERY_ENV,
    IndicatorProcess,
    IndicatorRunner,
)
from indicator_runner.runtime_journal import StrategyOperationalStatus
from indicator_runner.signal_worker import HISTORICAL_REBUILD_EXIT_CODE
from lib_common.app import HealthStatus
from lib_common.hashing import canonical_json_hash, sha256_file
from lib_common.paper_promotion import (
    PAPER_PROMOTION_EVIDENCE_NAMES,
    PAPER_PROMOTION_EVIDENCE_SCHEMA_VERSION,
    PAPER_PROMOTION_IMAGE,
    PaperPromotionScope,
    build_paper_promotion_manifest,
    load_paper_promotion_scope,
    paper_promotion_instrument_set_sha256,
)

_EVIDENCE_RUN_ID = "swing-btcusdc-paper-20260726"
_EVIDENCE_OBSERVED_AT = "2026-07-26T12:00:00Z"
_CANARY_DIRECTORY = "SwingHighLowPMO"
_CANARY_CONTRACT: dict[str, object] = {
    "strategy_id": "swing_high_low_pmo_v1",
    "strategy_version": "1.0.1",
    "strategy_universe": "BTCUSDC",
    "universe_contract": None,
    "model_scope": "single_instrument",
    "canonical_instrument": "BTC-USDC",
    "asset_class": "crypto",
    "market_data_source": "coinbase_live",
    "market_data_timeframe": "1m",
    "consolidation_minutes": 15,
    "broker_code": "paper",
    "data_use_scope": None,
    "model_configuration_sha256": None,
    "instrument_set_sha256": canonical_json_hash(
        {
            "schema": "paper-promotion-single-instrument-v1",
            "canonical_instrument": "BTC-USDC",
        }
    ),
    "scoring_semantics": "calibrated_forecast",
    "order_evidence_profile": "bracket_oco",
}


def _write_strategy(
    root: Path,
    name: str,
    *,
    enabled: bool = True,
    environments: tuple[str, ...] = ("dev", "production"),
    readiness: str = "READY_FOR_PAPER_TRADING",
    run_mode: str = "paper",
    paper_canary: bool = False,
) -> Path:
    if paper_canary:
        name = _CANARY_DIRECTORY
    strategy_dir = root / "strategies" / "indicator" / name
    strategy_dir.mkdir(parents=True)
    config = {
        "strategy_id": (_CANARY_CONTRACT["strategy_id"] if paper_canary else f"{name.lower()}_v1"),
        "strategy_version": (_CANARY_CONTRACT["strategy_version"] if paper_canary else "1.0.0"),
        "schema_version": "2",
        "enabled": enabled,
        "environments": list(environments),
        "runner_kind": "signal_worker",
        "runtime": {"mode": run_mode},
        "trade_direction_mode": "long_only",
        "parameters": {
            "universe": _CANARY_CONTRACT["strategy_universe"],
            "asset_class": _CANARY_CONTRACT["asset_class"],
            "trade_direction_mode": "long_only",
        },
        "market_data": {
            "source": _CANARY_CONTRACT["market_data_source"],
            "timeframe": _CANARY_CONTRACT["market_data_timeframe"],
            "consolidation_minutes": (
                _CANARY_CONTRACT["consolidation_minutes"] if paper_canary else 1
            ),
            "bootstrap_bars": 1,
        },
        "deployment": {
            "paper_candidate": {
                "canonical_instrument": _CANARY_CONTRACT["canonical_instrument"],
                "broker_code": "local-paper",
            }
        },
        "metadata": {
            "decision": "INSUFFICIENT_EVIDENCE",
            "readiness": readiness,
        },
    }
    config_path = strategy_dir / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _promotion_evidence_scope(config_path: Path) -> dict[str, object]:
    return {
        **_CANARY_CONTRACT,
        "broker_environment": "paper",
        "capital_mode": "paper",
        "live_authority": False,
        "dedicated_account": True,
        "user_id": "u1",
        "broker_account_id": 101,
        "strategy_binding_id": 201,
        "image_repository": PAPER_PROMOTION_IMAGE,
        "image_tag": "1.2.3",
        "config_sha256": sha256_file(config_path),
    }


def _promotion_evidence_outcomes(name: str) -> dict[str, object]:
    outcomes: dict[str, dict[str, object]] = {
        "account_binding": {
            "user_active": True,
            "account_connected": True,
            "binding_active": True,
            "autopilot_enabled": True,
            "entries_enabled": True,
            "exits_enabled": True,
            "scope_matches_persistence": True,
            "dedicated_account_confirmed": True,
            "matching_active_route_count": 1,
            "conflicting_active_route_count": 0,
        },
        "current_authorization": {
            "user_current": True,
            "account_current": True,
            "binding_current": True,
            "route_current": True,
            "credential_state": "not_required",
            "paper_environment_confirmed": True,
            "pre_broker_io_revalidation_passed": True,
            "revoked_entry_rejected": True,
            "close_only_entry_rejected": True,
            "close_only_exit_allowed": True,
            "live_broker_call_count": 0,
            "unknown_submission_count": 0,
            "authorization_check_count": 4,
        },
        "durable_model_restart": {
            "mid_position_restart_passed": True,
            "state_contract_match": True,
            "incompatible_state_rejected": True,
            "watermark_monotonic": True,
            "post_restart_decision_match": True,
            "divergent_user_outcomes_isolated": True,
            "duplicate_signal_count": 0,
            "orphan_signal_count": 0,
            "restart_count": 1,
        },
        "paper_order_restart": {
            "stop_case_passed": True,
            "target_case_passed": True,
            "same_bar_adverse_case_passed": True,
            "gap_case_passed": True,
            "partial_fill_case_passed": True,
            "oco_atomicity_passed": True,
            "pre_trigger_restart_passed": True,
            "post_fill_restart_passed": True,
            "exact_source_provenance": True,
            "exact_fee_provenance": True,
            "duplicate_fill_count": 0,
            "orphan_fill_count": 0,
            "restart_count": 2,
            "canonical_fill_count": 2,
        },
        "real_market_data": {
            "real_market_data": True,
            "simulated_market_data": False,
            "market_evidence": True,
            "source_timestamps_complete": True,
            "persisted_price_lineage_complete": True,
            "price_row_count": 8640,
            "complete_price_row_count": 8640,
            "provenance_price_row_count": 8640,
            "coverage_start": "2026-07-10T00:00:00Z",
            "coverage_end": "2026-07-16T00:00:00Z",
        },
        "reconciliation": {
            "initial_reconciliation_complete": True,
            "reconciliation_healthy": True,
            "orders_match": True,
            "fills_match": True,
            "positions_match": True,
            "cash_match": True,
            "pnl_match": True,
            "drift_count": 0,
            "unknown_submission_count": 0,
            "orphan_order_count": 0,
            "orphan_fill_count": 0,
            "reconciliation_run_count": 2,
        },
        "scoring_inputs": {
            "entry_scoring_input_source": "explicit",
            "require_explicit_scoring_inputs": True,
            "expected_return_present": True,
            "predicted_risk_present": True,
            "out_of_sample_calibration": True,
            "leakage_check_passed": True,
            "calibration_current": True,
            "uncalibrated_entry_rejected": True,
            "heuristic_entry_signal_count": 0,
            "explicit_entry_signal_count": 2,
            "uncalibrated_guard_rejection_count": 1,
            "calibration_version": "swing-btcusdc-oos-v1",
            "calibration_valid_from": "2026-07-01T00:00:00Z",
            "calibration_valid_until": "2026-08-01T00:00:00Z",
        },
        "service_transport_restart": {
            "production_compose_topology": True,
            "current_time_signal_path_passed": True,
            "historical_stale_rejection_passed": True,
            "retry_injected": True,
            "restart_injected": True,
            "stable_identity_preserved": True,
            "exact_feedback_lineage": True,
            "duplicate_signal_count": 0,
            "duplicate_order_count": 0,
            "duplicate_fill_count": 0,
            "dead_letter_count": 0,
            "orphan_record_count": 0,
            "canonical_signal_count": 2,
            "scoring_decision_count": 2,
            "execution_command_count": 2,
            "entry_fill_count": 1,
            "close_fill_count": 1,
            "feedback_evaluation_count": 2,
        },
        "soak_acceptance": {
            "report_passed": True,
            "feedback_liveness": True,
            "market_data_freshness": True,
            "signal_activity": True,
            "outbox_backlog": True,
            "execution_fills": True,
            "duplicate_submissions": True,
            "positions_consistency": True,
            "nav_recorded": True,
            "alert_sink": True,
            "duration_days": 14,
        },
    }
    return outcomes[name]


def _write_promotion_evidence(
    root: Path,
    config_path: Path,
) -> dict[str, Path]:
    evidence_dir = root / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    evidence_paths: dict[str, Path] = {}
    for evidence_name in PAPER_PROMOTION_EVIDENCE_NAMES:
        evidence_path = evidence_dir / f"{evidence_name}.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "schema_version": PAPER_PROMOTION_EVIDENCE_SCHEMA_VERSION,
                    "evidence_type": evidence_name,
                    "status": "passed",
                    "run_id": _EVIDENCE_RUN_ID,
                    "observed_at": _EVIDENCE_OBSERVED_AT,
                    "scope": _promotion_evidence_scope(config_path),
                    "outcomes": _promotion_evidence_outcomes(evidence_name),
                }
            ),
            encoding="utf-8",
        )
        evidence_paths[evidence_name] = evidence_path
    return evidence_paths


def _build_test_promotion_manifest(
    *,
    root: Path,
    config_path: Path,
    evidence_paths: dict[str, Path],
) -> dict[str, Any]:
    return build_paper_promotion_manifest(
        config_path=config_path,
        artifact_root=root,
        evidence_paths=evidence_paths,
        user_id="u1",
        broker_account_id=101,
        strategy_binding_id=201,
        image_tag="1.2.3",
        operator="test-operator",
    )


def _write_promotion_manifest(
    root: Path,
    config_path: Path,
    monkeypatch,
    *,
    live_authority: bool = False,
) -> Path:
    evidence_paths = _write_promotion_evidence(root, config_path)
    manifest = _build_test_promotion_manifest(
        root=root,
        config_path=config_path,
        evidence_paths=evidence_paths,
    )
    manifest["live_authority"] = live_authority
    manifest_path = root / "paper-promotion.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("INDICATOR_PAPER_PROMOTION_MANIFEST", str(manifest_path))
    monkeypatch.setenv("VM_DEPLOY_IMAGE_TAG", "1.2.3")
    return manifest_path


def _clear_selection(monkeypatch) -> None:
    # Unit selection follows the config under test, independent of the caller's
    # paper-mode environment. These tests never start an execution engine.
    monkeypatch.delenv("EXECUTION_MODE", raising=False)
    monkeypatch.delenv("RUN_MODE", raising=False)
    monkeypatch.delenv("STRATEGY_NAME", raising=False)
    monkeypatch.delenv("STRATEGY_LIST", raising=False)
    monkeypatch.delenv(DEV_DISCOVERY_ENV, raising=False)
    monkeypatch.delenv("INDICATOR_PAPER_PROMOTION_MANIFEST", raising=False)
    monkeypatch.delenv("VM_DEPLOY_IMAGE_TAG", raising=False)
    monkeypatch.delenv("VM_IMAGE_TAG", raising=False)
    monkeypatch.delenv("INDICATOR_PANEL_DATA_USE_SCOPE", raising=False)
    monkeypatch.delenv("INDICATOR_PANEL_ENTITLEMENT_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("INDICATOR_PANEL_ACTIVATION_CUTOFF", raising=False)


def _runner() -> IndicatorRunner:
    return IndicatorRunner(category="indicator", deployment_config={}, secrets={})


def test_parent_main_configures_logging_before_application_setup(monkeypatch) -> None:
    """Parent lifecycle and shutdown logs must be visible in container output."""
    from indicator_runner import main as runner_main

    calls: list[str] = []
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(runner_main, "setup_logging", lambda level: calls.append(level))

    sentinel = RuntimeError("stop after logging setup")

    class _FailingApp:
        def __init__(self) -> None:
            raise sentinel

    monkeypatch.setattr(runner_main, "StrategyRunnerApp", _FailingApp)

    with pytest.raises(RuntimeError, match="stop after logging setup"):
        runner_main.main()

    assert calls == ["DEBUG"]


def test_missing_selector_fails_closed_in_production(tmp_path, monkeypatch) -> None:
    _write_strategy(tmp_path, "SelectedStrategy")
    monkeypatch.chdir(tmp_path)
    _clear_selection(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")

    runner = _runner()
    runner.load_strategies()

    assert runner.strategies == []


def test_dev_discovery_requires_explicit_dev_only_override(tmp_path, monkeypatch) -> None:
    _write_strategy(tmp_path, "EnabledStrategy", readiness="STATIC_REVIEW_ONLY")
    _write_strategy(tmp_path, "DisabledStrategy", enabled=False)
    monkeypatch.chdir(tmp_path)
    _clear_selection(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv(DEV_DISCOVERY_ENV, "true")

    runner = _runner()
    runner.load_strategies()

    assert [strategy.name for strategy in runner.strategies] == ["EnabledStrategy"]


def test_production_rejects_backtest_only_strategy(tmp_path, monkeypatch) -> None:
    _write_strategy(tmp_path, "BacktestOnly", readiness="READY_FOR_BACKTEST")
    monkeypatch.chdir(tmp_path)
    _clear_selection(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STRATEGY_LIST", "BacktestOnly")

    runner = _runner()
    with pytest.raises(RuntimeError, match=r"unavailable strategies.*BacktestOnly"):
        runner.load_strategies()
    assert runner.strategies == []


def test_production_live_requires_live_candidate(tmp_path, monkeypatch) -> None:
    config_path = _write_strategy(
        tmp_path,
        _CANARY_DIRECTORY,
        readiness="READY_FOR_PAPER_TRADING",
        run_mode="live",
        paper_canary=True,
    )
    monkeypatch.chdir(tmp_path)
    _clear_selection(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STRATEGY_LIST", _CANARY_DIRECTORY)
    _write_promotion_manifest(tmp_path, config_path, monkeypatch)

    runner = _runner()
    with pytest.raises(RuntimeError, match=r"unavailable strategies.*SwingHighLowPMO"):
        runner.load_strategies()


def test_dev_discovery_override_is_rejected_outside_dev(tmp_path, monkeypatch) -> None:
    _write_strategy(tmp_path, "SelectedStrategy")
    monkeypatch.chdir(tmp_path)
    _clear_selection(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv(DEV_DISCOVERY_ENV, "true")

    runner = _runner()
    runner.load_strategies()

    assert runner.strategies == []


def test_strategy_list_matching_is_exact_case_sensitive(tmp_path, monkeypatch) -> None:
    _write_strategy(tmp_path, "SelectedStrategy")
    _write_strategy(tmp_path, "DisabledStrategy", enabled=False)
    monkeypatch.chdir(tmp_path)
    _clear_selection(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STRATEGY_LIST", "selectedstrategy,DisabledStrategy")

    wrong_case_runner = _runner()
    with pytest.raises(RuntimeError, match=r"unavailable strategies.*selectedstrategy"):
        wrong_case_runner.load_strategies()

    monkeypatch.setenv("STRATEGY_LIST", "SelectedStrategy,DisabledStrategy")
    exact_case_runner = _runner()
    with pytest.raises(RuntimeError, match=r"unavailable strategies.*DisabledStrategy"):
        exact_case_runner.load_strategies()


def test_staging_strategy_list_rejects_environment_excluded_member(tmp_path, monkeypatch) -> None:
    selected_config = _write_strategy(
        tmp_path,
        _CANARY_DIRECTORY,
        environments=("staging", "production"),
        paper_canary=True,
    )
    _write_strategy(tmp_path, "DevOnlyProbe", environments=("dev",))
    monkeypatch.chdir(tmp_path)
    _clear_selection(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("STRATEGY_LIST", f"{_CANARY_DIRECTORY},DevOnlyProbe")
    _write_promotion_manifest(tmp_path, selected_config, monkeypatch)

    runner = _runner()
    with pytest.raises(RuntimeError, match=r"unavailable strategies.*DevOnlyProbe"):
        runner.load_strategies()
    assert [strategy.name for strategy in runner.strategies] == [_CANARY_DIRECTORY]


def test_production_paper_requires_exact_evidence_manifest(tmp_path, monkeypatch) -> None:
    config_path = _write_strategy(
        tmp_path,
        _CANARY_DIRECTORY,
        paper_canary=True,
    )
    monkeypatch.chdir(tmp_path)
    _clear_selection(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STRATEGY_LIST", _CANARY_DIRECTORY)

    without_manifest = _runner()
    with pytest.raises(RuntimeError, match=r"unavailable strategies.*SwingHighLowPMO"):
        without_manifest.load_strategies()

    manifest_path = _write_promotion_manifest(tmp_path, config_path, monkeypatch)
    scope, errors = load_paper_promotion_scope(
        manifest_path=manifest_path,
        deploy_image_tag="1.2.3",
    )
    assert errors == ()
    assert scope is not None
    assert scope.user_id == "u1"
    assert scope.strategy_binding_id == 201
    assert scope.broker_account_id == 101
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "passed"
    assert manifest["image_repository"] == "vynmatrix/platform"
    assert manifest["evidence_run_id"] == _EVIDENCE_RUN_ID
    assert manifest["live_authority"] is False

    with_manifest = _runner()
    with_manifest.load_strategies()

    assert [strategy.name for strategy in with_manifest.strategies] == [_CANARY_DIRECTORY]


def test_paper_manifest_rejects_retired_image_with_matching_evidence(tmp_path, monkeypatch) -> None:
    config_path = _write_strategy(tmp_path, _CANARY_DIRECTORY, paper_canary=True)
    manifest_path = _write_promotion_manifest(tmp_path, config_path, monkeypatch)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["image_repository"] = "vynmatrix/indicator-runner"
    for descriptor in manifest["evidence"].values():
        evidence_path = tmp_path / descriptor["path"]
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["scope"]["image_repository"] = "vynmatrix/indicator-runner"
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        descriptor["sha256"] = sha256_file(evidence_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    scope, errors = load_paper_promotion_scope(
        manifest_path=manifest_path,
        deploy_image_tag="1.2.3",
    )

    assert scope is None
    assert any("image_repository" in error for error in errors)


def test_synchronized_promotion_requires_exact_panel_owner_scope(tmp_path, monkeypatch) -> None:
    manifest_path = tmp_path / "portfolio-promotion.json"
    manifest_path.write_text("{}", encoding="utf-8")
    scope = PaperPromotionScope(
        user_id="owner-1",
        broker_account_id=101,
        strategy_binding_id=201,
        strategy_id="us_quality_compounder_v1",
        strategy_version="1.0.0",
        strategy_universe="SP500",
        model_scope="synchronized_portfolio",
        canonical_instrument=None,
        asset_class="equity",
        broker_code="ibkr",
        data_use_scope="paper_forward",
        model_configuration_sha256="a" * 64,
        instrument_set_sha256="b" * 64,
        instruments=((1, "IBM"),),
        scoring_semantics="rank_model",
        order_evidence_profile="synchronized_targets",
    )
    _clear_selection(monkeypatch)
    monkeypatch.setenv("INDICATOR_PAPER_PROMOTION_MANIFEST", str(manifest_path))
    monkeypatch.setenv("VM_DEPLOY_IMAGE_TAG", "1.2.3")
    monkeypatch.setenv("INDICATOR_PANEL_DATA_USE_SCOPE", "paper_forward")
    monkeypatch.setenv("INDICATOR_PANEL_ENTITLEMENT_OWNER_USER_ID", "owner-1")
    monkeypatch.setenv("INDICATOR_PANEL_ACTIVATION_CUTOFF", "2026-12-31T21:00:00Z")
    monkeypatch.setattr(
        process_manager,
        "load_paper_promotion_scope",
        lambda **_kwargs: (scope, ()),
    )

    matching = _runner()
    assert matching._has_exact_paper_promotion(
        {"strategy_id": scope.strategy_id},
        config_path=tmp_path / "config.json",
    )

    monkeypatch.setenv("INDICATOR_PANEL_ENTITLEMENT_OWNER_USER_ID", "other-owner")
    mismatched = _runner()
    assert not mismatched._has_exact_paper_promotion(
        {"strategy_id": scope.strategy_id},
        config_path=tmp_path / "config.json",
    )


def test_portfolio_manifest_binds_config_model_and_instrument_set(tmp_path) -> None:
    strategy_dir = tmp_path / "strategies" / "indicator" / "USQualityCompounder"
    strategy_dir.mkdir(parents=True)
    config_path = strategy_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "strategy_id": "us_quality_compounder_v1",
                "strategy_version": "1.0.0",
                "parameters": {
                    "universe": "SP500",
                    "universe_contract": "point_in_time_sp500_membership",
                    "asset_class": "equity",
                },
                "market_data": {
                    "source": "eodhd",
                    "timeframe": "1d",
                    "consolidation_minutes": 0,
                },
                "execution": {"require_explicit_scoring_inputs": False},
            }
        ),
        encoding="utf-8",
    )
    instruments = {1: "IBM", 2: "MSFT"}
    model_configuration_sha256 = "a" * 64
    portfolio_contract = {
        "strategy_id": "us_quality_compounder_v1",
        "strategy_version": "1.0.0",
        "strategy_universe": "SP500",
        "universe_contract": "point_in_time_sp500_membership",
        "model_scope": "synchronized_portfolio",
        "canonical_instrument": None,
        "asset_class": "equity",
        "market_data_source": "eodhd",
        "market_data_timeframe": "1d",
        "consolidation_minutes": 0,
        "broker_code": "ibkr",
        "data_use_scope": "paper_forward",
        "model_configuration_sha256": model_configuration_sha256,
        "instrument_set_sha256": paper_promotion_instrument_set_sha256(instruments),
        "scoring_semantics": "rank_model",
        "order_evidence_profile": "synchronized_targets",
    }
    instrument_set_path = tmp_path / "usqc-instruments.json"
    instrument_set_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "strategy_id": "us_quality_compounder_v1",
                "strategy_version": "1.0.0",
                "model_configuration_sha256": model_configuration_sha256,
                "data_use_scope": "paper_forward",
                "instruments": [
                    {"instrument_id": instrument_id, "canonical_symbol": symbol}
                    for instrument_id, symbol in sorted(instruments.items())
                ],
            }
        ),
        encoding="utf-8",
    )
    evidence_paths = _write_promotion_evidence(tmp_path, config_path)
    portfolio_scope = {
        **portfolio_contract,
        "broker_environment": "paper",
        "capital_mode": "paper",
        "live_authority": False,
        "dedicated_account": True,
        "user_id": "u1",
        "broker_account_id": 101,
        "strategy_binding_id": 201,
        "image_repository": PAPER_PROMOTION_IMAGE,
        "image_tag": "1.2.3",
        "config_sha256": sha256_file(config_path),
    }
    for evidence_path in evidence_paths.values():
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["scope"] = portfolio_scope
        if evidence["evidence_type"] == "current_authorization":
            evidence["outcomes"]["credential_state"] = "current"
        elif evidence["evidence_type"] == "paper_order_restart":
            evidence["outcomes"] = {
                "target_batch_passed": True,
                "reduce_case_passed": True,
                "exit_case_passed": True,
                "partial_fill_case_passed": True,
                "pre_submission_restart_passed": True,
                "post_partial_fill_restart_passed": True,
                "exact_source_provenance": True,
                "exact_fee_provenance": True,
                "duplicate_fill_count": 0,
                "orphan_fill_count": 0,
                "restart_count": 2,
                "canonical_fill_count": 1,
            }
        elif evidence["evidence_type"] == "scoring_inputs":
            evidence["outcomes"] = {
                "entry_scoring_input_source": "model_rank",
                "require_explicit_scoring_inputs": False,
                "rank_snapshot_present": True,
                "configuration_identity_present": True,
                "calibration_required": False,
                "synthetic_expected_return_count": 0,
                "synthetic_predicted_risk_count": 0,
                "ranked_entry_signal_count": 1,
            }
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    manifest = build_paper_promotion_manifest(
        config_path=config_path,
        artifact_root=tmp_path,
        evidence_paths=evidence_paths,
        user_id="u1",
        broker_account_id=101,
        strategy_binding_id=201,
        image_tag="1.2.3",
        operator="test-operator",
        model_scope="synchronized_portfolio",
        broker_code="ibkr",
        model_configuration_sha256=model_configuration_sha256,
        instrument_set_artifact=instrument_set_path,
    )
    manifest_path = tmp_path / "portfolio-promotion.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    scope, errors = load_paper_promotion_scope(
        manifest_path=manifest_path,
        deploy_image_tag="1.2.3",
        config_path=config_path,
    )

    assert errors == ()
    assert scope is not None
    assert scope.is_synchronized_portfolio
    assert scope.model_configuration_sha256 == model_configuration_sha256
    assert scope.instrument_set_sha256 == portfolio_contract["instrument_set_sha256"]
    assert scope.instruments == ((1, "IBM"), (2, "MSFT"))

    instrument_set_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "strategy_id": "us_quality_compounder_v1",
                "strategy_version": "1.0.0",
                "model_configuration_sha256": model_configuration_sha256,
                "data_use_scope": "paper_forward",
                "instruments": [
                    {"instrument_id": 1, "canonical_symbol": "IBM"},
                    {"instrument_id": 2, "canonical_symbol": "I_BM"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="identities must be trimmed and unique"):
        build_paper_promotion_manifest(
            config_path=config_path,
            artifact_root=tmp_path,
            evidence_paths=evidence_paths,
            user_id="u1",
            broker_account_id=101,
            strategy_binding_id=201,
            image_tag="1.2.3",
            operator="test-operator",
            model_configuration_sha256=model_configuration_sha256,
            instrument_set_artifact=instrument_set_path,
        )


def test_paper_manifest_builder_rejects_arbitrary_hashed_json(tmp_path) -> None:
    config_path = _write_strategy(
        tmp_path,
        _CANARY_DIRECTORY,
        paper_canary=True,
    )
    evidence_paths = _write_promotion_evidence(tmp_path, config_path)
    evidence_paths["account_binding"].write_text(
        json.dumps({"status": "passed", "name": "account_binding"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"evidence\.account_binding.*schema 2"):
        _build_test_promotion_manifest(
            root=tmp_path,
            config_path=config_path,
            evidence_paths=evidence_paths,
        )


def test_paper_manifest_builder_rejects_failed_evidence_outcome(tmp_path) -> None:
    config_path = _write_strategy(
        tmp_path,
        _CANARY_DIRECTORY,
        paper_canary=True,
    )
    evidence_paths = _write_promotion_evidence(tmp_path, config_path)
    evidence_path = evidence_paths["soak_acceptance"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["status"] = "failed"
    evidence["outcomes"]["report_passed"] = False
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"evidence\.soak_acceptance.*header mismatch.*outcomes failed",
    ):
        _build_test_promotion_manifest(
            root=tmp_path,
            config_path=config_path,
            evidence_paths=evidence_paths,
        )


def test_paper_manifest_builder_rejects_cross_account_evidence(tmp_path) -> None:
    config_path = _write_strategy(
        tmp_path,
        _CANARY_DIRECTORY,
        paper_canary=True,
    )
    evidence_paths = _write_promotion_evidence(tmp_path, config_path)
    evidence_path = evidence_paths["reconciliation"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["scope"]["broker_account_id"] = 999
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"evidence\.reconciliation\.scope mismatch.*broker_account_id",
    ):
        _build_test_promotion_manifest(
            root=tmp_path,
            config_path=config_path,
            evidence_paths=evidence_paths,
        )


def test_paper_manifest_builder_rejects_unexpected_outcome_field(tmp_path) -> None:
    config_path = _write_strategy(
        tmp_path,
        _CANARY_DIRECTORY,
        paper_canary=True,
    )
    evidence_paths = _write_promotion_evidence(tmp_path, config_path)
    evidence_path = evidence_paths["durable_model_restart"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["outcomes"]["unregistered_witness"] = "different-signal"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"evidence\.durable_model_restart\.outcomes fields",
    ):
        _build_test_promotion_manifest(
            root=tmp_path,
            config_path=config_path,
            evidence_paths=evidence_paths,
        )


def test_paper_manifest_builder_rejects_mixed_evidence_runs(tmp_path) -> None:
    config_path = _write_strategy(
        tmp_path,
        _CANARY_DIRECTORY,
        paper_canary=True,
    )
    evidence_paths = _write_promotion_evidence(tmp_path, config_path)
    evidence_path = evidence_paths["paper_order_restart"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["run_id"] = "different-paper-run"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(ValueError, match="must share one non-empty run_id"):
        _build_test_promotion_manifest(
            root=tmp_path,
            config_path=config_path,
            evidence_paths=evidence_paths,
        )


def test_paper_manifest_runtime_revalidates_semantics_after_rehash(tmp_path) -> None:
    config_path = _write_strategy(
        tmp_path,
        _CANARY_DIRECTORY,
        paper_canary=True,
    )
    evidence_paths = _write_promotion_evidence(tmp_path, config_path)
    manifest = _build_test_promotion_manifest(
        root=tmp_path,
        config_path=config_path,
        evidence_paths=evidence_paths,
    )
    evidence_path = evidence_paths["real_market_data"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["outcomes"]["simulated_market_data"] = True
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    manifest["evidence"]["real_market_data"]["sha256"] = sha256_file(evidence_path)
    manifest_path = tmp_path / "paper-promotion.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    scope, errors = load_paper_promotion_scope(
        manifest_path=manifest_path,
        deploy_image_tag="1.2.3",
        config_path=config_path,
    )

    assert scope is None
    assert any(
        "evidence.real_market_data.outcomes failed" in error and "simulated_market_data" in error
        for error in errors
    )


def test_paper_manifest_refuses_live_authority(tmp_path, monkeypatch) -> None:
    config_path = _write_strategy(
        tmp_path,
        _CANARY_DIRECTORY,
        paper_canary=True,
    )
    monkeypatch.chdir(tmp_path)
    _clear_selection(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STRATEGY_LIST", _CANARY_DIRECTORY)
    _write_promotion_manifest(
        tmp_path,
        config_path,
        monkeypatch,
        live_authority=True,
    )

    runner = _runner()
    with pytest.raises(RuntimeError, match=r"unavailable strategies.*SwingHighLowPMO"):
        runner.load_strategies()


def test_paper_manifest_rejects_tampered_evidence(tmp_path, monkeypatch) -> None:
    config_path = _write_strategy(
        tmp_path,
        _CANARY_DIRECTORY,
        paper_canary=True,
    )
    monkeypatch.chdir(tmp_path)
    _clear_selection(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STRATEGY_LIST", _CANARY_DIRECTORY)
    manifest_path = _write_promotion_manifest(tmp_path, config_path, monkeypatch)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence_path = tmp_path / manifest["evidence"]["real_market_data"]["path"]
    evidence_path.write_text('{"status":"tampered"}', encoding="utf-8")

    runner = _runner()
    with pytest.raises(RuntimeError, match=r"unavailable strategies.*SwingHighLowPMO"):
        runner.load_strategies()


def test_paper_manifest_rejects_any_non_canary_strategy(tmp_path, monkeypatch) -> None:
    canary_config = _write_strategy(
        tmp_path,
        _CANARY_DIRECTORY,
        paper_canary=True,
    )
    _write_strategy(tmp_path, "OtherStrategy")
    monkeypatch.chdir(tmp_path)
    _clear_selection(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STRATEGY_LIST", "OtherStrategy")
    _write_promotion_manifest(tmp_path, canary_config, monkeypatch)

    runner = _runner()
    with pytest.raises(RuntimeError, match=r"unavailable strategies.*OtherStrategy"):
        runner.load_strategies()


def test_repository_swing_canary_remains_dev_backtest_only() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    config_path = repo_root / "strategies" / "indicator" / _CANARY_DIRECTORY / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["environments"] == ["dev"]
    assert config["metadata"]["readiness"] == "READY_FOR_BACKTEST"
    assert config.get("runtime", {}).get("mode", "backtest") == "backtest"
    assert config["deployment"]["paper_candidate"] == {
        "canonical_instrument": "BTC-USDC",
        "broker_code": "local-paper",
    }


def test_explicit_missing_retired_strategy_is_fatal(tmp_path, monkeypatch) -> None:
    _write_strategy(tmp_path, "SelectedStrategy")
    monkeypatch.chdir(tmp_path)
    _clear_selection(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STRATEGY_LIST", "VortexTrendCapture")

    runner = _runner()
    with pytest.raises(RuntimeError, match=r"unavailable strategies.*VortexTrendCapture"):
        runner.load_strategies()


def test_historical_rebuild_restart_does_not_consume_crash_budget(monkeypatch) -> None:
    class _ExitedProcess:
        returncode = HISTORICAL_REBUILD_EXIT_CODE

        def poll(self) -> int:
            return HISTORICAL_REBUILD_EXIT_CODE

    runner = _runner()
    strategy = IndicatorProcess(
        name="SelectedStrategy",
        path=Path("/strategies/SelectedStrategy"),
        config={"strategy_id": "selected_v1"},
        process=_ExitedProcess(),  # type: ignore[arg-type]
        restart_count=4,
        status="running",
    )
    runner.strategies = [strategy]
    restarted: list[IndicatorProcess] = []

    def _restart(candidate: IndicatorProcess) -> None:
        restarted.append(candidate)
        runner.shutdown_flag = True

    monkeypatch.setattr(runner, "_start_strategy", _restart)
    monkeypatch.setattr(process_manager.time, "sleep", lambda _seconds: None)

    runner._monitor_processes()

    assert restarted == [strategy]
    assert strategy.restart_count == 4
    assert strategy.last_restart is not None


def test_parent_health_fails_when_running_child_is_operationally_stale() -> None:
    status = StrategyOperationalStatus(
        worker_id="SelectedStrategy:paper",
        strategy_id="selected_v1",
        ready=False,
        outbox_counts={"dead_letter": 1},
        outbox_oldest_age_seconds=600,
        feed_lags=(),
    )

    class _Reader:
        def read(self, **_kwargs: object) -> StrategyOperationalStatus:
            return status

    runner = IndicatorRunner(
        category="indicator",
        deployment_config={},
        secrets={},
        operational_reader=_Reader(),  # type: ignore[arg-type]
    )
    runner.strategies = [
        IndicatorProcess(
            name="SelectedStrategy",
            path=Path("/strategies/SelectedStrategy"),
            config={
                "strategy_id": "selected_v1",
                "parameters": {
                    "universe": ["BTCUSDC"],
                    "asset_class": "crypto",
                },
                "market_data": {
                    "source": "coinbase_live",
                    "timeframe": "1m",
                },
            },
            run_mode="paper",
            status="running",
        )
    ]
    app = StrategyRunnerApp()
    app.strategy_runner = runner

    health = app.get_health_status()

    assert health.status == HealthStatus.UNHEALTHY
    assert health.message == "1/1 strategies outside operational SLO"
    assert health.details["operational_ready"] is False
    assert health.details["operational_slo"] == {
        "max_signal_backlog_age_seconds": 300,
        "max_strategy_lag_seconds": 300,
    }
    assert health.details["operational"][0]["outbox"]["counts"] == {"dead_letter": 1}


def test_parent_passes_configured_panel_freshness_to_operational_reader() -> None:
    captured: dict[str, object] = {}

    class _Reader:
        def read(self, **kwargs: object) -> StrategyOperationalStatus:
            captured.update(kwargs)
            return StrategyOperationalStatus(
                worker_id="USQualityCompounder:paper",
                strategy_id="us_quality_compounder_v1",
                ready=True,
                outbox_counts={},
                outbox_oldest_age_seconds=0.0,
                feed_lags=(),
            )

    runner = IndicatorRunner(
        category="indicator",
        deployment_config={},
        secrets={},
        operational_reader=_Reader(),  # type: ignore[arg-type]
    )
    runner.strategies = [
        IndicatorProcess(
            name="USQualityCompounder",
            path=Path("/strategies/USQualityCompounder"),
            config={
                "strategy_id": "us_quality_compounder_v1",
                "strategy_version": "0.2.0",
                "parameters": {
                    "universe": "SP500",
                    "asset_class": "equity",
                    "universe_contract": "point_in_time_sp500_membership",
                    "max_panel_age_days": 100,
                },
                "market_data": {"source": "eodhd", "timeframe": "1d"},
            },
            run_mode="paper",
            status="running",
        )
    ]

    runner.get_status()

    assert captured["panel_capable"] is True
    assert captured["max_panel_age_seconds"] == 100 * 24 * 60 * 60


def test_parent_status_degrades_on_operational_database_failure() -> None:
    class _Reader:
        def read(self, **_kwargs: object) -> StrategyOperationalStatus:
            raise SQLAlchemyError("database unavailable")

    runner = IndicatorRunner(
        category="indicator",
        deployment_config={},
        secrets={},
        operational_reader=_Reader(),  # type: ignore[arg-type]
    )
    runner.strategies = [
        IndicatorProcess(
            name="SelectedStrategy",
            path=Path("/strategies/SelectedStrategy"),
            config={"strategy_id": "selected_v1"},
            run_mode="paper",
            status="running",
        )
    ]

    status = runner.get_status()

    assert status["operational_ready"] is False
    assert status["operational"][0]["error"] == ("operational_status_unavailable:SQLAlchemyError")


def test_parent_status_does_not_hide_unexpected_reader_defect() -> None:
    class _Reader:
        def read(self, **_kwargs: object) -> StrategyOperationalStatus:
            raise RuntimeError("unexpected readiness defect")

    runner = IndicatorRunner(
        category="indicator",
        deployment_config={},
        secrets={},
        operational_reader=_Reader(),  # type: ignore[arg-type]
    )
    runner.strategies = [
        IndicatorProcess(
            name="SelectedStrategy",
            path=Path("/strategies/SelectedStrategy"),
            config={"strategy_id": "selected_v1"},
            run_mode="paper",
            status="running",
        )
    ]

    with pytest.raises(RuntimeError, match="unexpected readiness defect"):
        runner.get_status()
