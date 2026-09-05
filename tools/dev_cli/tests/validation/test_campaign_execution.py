"""Execution, scoring, sizing, and benchmark campaign contracts."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from campaign_test_support import (
    _REGISTERED_CAMPAIGN,
    _REPO,
    _abandoned_count,
    _Audit,
    _campaign,
    _cash_expected_benchmark_specs,
    _manifest,
    _persist_baseline_full_panel_reports,
    _persist_robustness_gate,
    _persist_scoring_source_folds,
    _protocol,
    _stub_benchmark_component_execution,
    _stub_primary_execution,
    _trial_ids_by_sequence,
    _unit_orchestration_result,
)
from sqlalchemy import select
from sqlalchemy.orm import Session
from validation_helpers import bars_from_ohlcv, load_market_fixture

from dev_cli.validation import campaign_derived_evidence as campaign_derived_module
from dev_cli.validation import campaign_registry as campaign_registry_module
from dev_cli.validation.backtest.engine import (
    BacktestEngine,
)
from dev_cli.validation.backtest.simulator import Trade
from dev_cli.validation.campaign import StrategyValidationCampaign
from lib_application.db.models import (
    BacktestResult,
    BacktestTrial,
)
from lib_data.bars import Bar
from lib_strategy.signals.loading import (
    load_pure_strategy_core,
)


def _interrupt_second_call(callback: Any) -> Any:
    calls = 0

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return callback(*args, **kwargs)

    return wrapper


def test_campaign_metadata_is_protocol_driven_for_equity_intraday_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise metadata propagation only; no market-performance claim is made."""

    campaign, _ = _campaign(tmp_path, monkeypatch)
    protocol = _protocol()
    protocol["data"]["timeframe"] = "60m"
    protocol["habitat"]["asset_class"] = "equity"
    protocol["inference"]["annualization_factor"] = 252.0
    runtime_config = json.loads((_REGISTERED_CAMPAIGN / "config.json").read_text())
    runtime_config["parameters"]["asset_class"] = "equity"
    resolver_calls: list[tuple[str, str]] = []
    monkeypatch.delattr(campaign, "_build_manifest")

    def resolve(requested: str, *, asset_class: str) -> int:
        resolver_calls.append((requested, asset_class))
        return len(resolver_calls)

    def load_bars(*_args: Any, symbol: str, **_kwargs: Any) -> tuple[list[Any], Any]:
        product = next(
            row
            for row in protocol["data"]["primary_products"]
            if row["requested_product"] == symbol
        )
        audit = SimpleNamespace(
            observed_rows=product["dataset_rows"],
            ohlcv_sha256=product["dataset_sha256"],
            to_dict=lambda: {"ohlcv_sha256": product["dataset_sha256"]},
        )
        return [], audit

    monkeypatch.setattr(campaign, "_validate_protocol", lambda _protocol: None)
    monkeypatch.setattr(campaign._price_service, "resolve_instrument", resolve)
    monkeypatch.setattr(campaign._historical_prices, "load_validated_bars", load_bars)
    monkeypatch.setattr(campaign, "_fetch_and_verify_product_metadata", lambda _row: {})
    monkeypatch.setattr(campaign, "_load_json_object", lambda _path: {})
    monkeypatch.setattr(campaign, "_validate_execution_cost_measurement", lambda *_a, **_k: None)
    monkeypatch.setattr(campaign, "_validate_data_parity_attestation", lambda *_a, **_k: None)
    monkeypatch.setattr(campaign, "_validate_correctness_attestation", lambda *_a, **_k: {})
    monkeypatch.setattr(campaign, "_environment_manifest", lambda *_a, **_k: {})
    monkeypatch.setattr(campaign_registry_module, "load_upstream_selection_ledger", lambda *_a: {})

    manifest = campaign._build_manifest(
        _REGISTERED_CAMPAIGN,
        protocol=protocol,
        runtime_config=runtime_config,
        execution_environment=None,
        upstream_selection_ledger=tmp_path / "unit-ledger",
        execution_cost_measurement=tmp_path / "unit-cost",
        data_parity_attestation=tmp_path / "unit-parity",
        correctness_attestation=tmp_path / "unit-correctness",
    )

    assert {asset_class for _symbol, asset_class in resolver_calls} == {"equity"}
    assert {row["asset_class"] for row in manifest["data"]["datasets"]} == {"equity"}
    assert {row["timeframe"] for row in manifest["data"]["datasets"]} == {"60m"}
    assert manifest["habitat"]["asset_class_db"] == "equity"

    spec = next(
        row
        for row in manifest["trial_registry"]
        if row["runner_kind"] == "production_core_direct" and row["fold_id"] == "full-panel"
    )
    dataset = manifest["data"]["datasets"][0]
    config = campaign._backtest_config(
        spec,
        dataset=dataset,
        manifest=manifest,
        fill_policy=campaign._fill_policy(spec, manifest=manifest),
    )
    assert (config.asset_class, config.consolidation_minutes, config.annualization_factor) == (
        "equity",
        60,
        252.0,
    )

    primary_specs = [
        row
        for row in manifest["trial_registry"]
        if row["runner_kind"] in {"anchored_joint_asset_selector", "pooled_oos_primary_aggregate"}
    ]
    captured: list[Any] = []
    monkeypatch.setattr(campaign, "_trial_status", lambda _trial_id: "registered")
    monkeypatch.setattr(
        campaign._result_store,
        "save_derived_evidence",
        lambda evidence, **_kwargs: captured.append(evidence),
    )
    campaign._persist_primary_result(
        _unit_orchestration_result(),
        primary_specs=primary_specs,
        trial_ids={int(row["sequence"]): str(row["sequence"]) for row in primary_specs},
        experiment_id=1,
        manifest=manifest,
        dataset_audits={
            row["requested_product"]: row["audit"] for row in manifest["data"]["datasets"]
        },
    )
    assert captured
    assert {(row.asset_class, row.timeframe) for row in captured} == {("equity", "60m")}


def test_primary_family_executes_once_and_completes_exact_registered_trials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    calls = _stub_primary_execution(campaign, monkeypatch)

    first = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, arm_ids=("TRAINING_SELECTED",))
    second = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=first.manifest_hash,
        arm_ids=("TRAINING_SELECTED",),
    )

    assert calls == [1]
    assert first.failures_this_run == second.failures_this_run == 0
    with Session(campaign._engine) as session:
        primary = session.query(BacktestTrial).filter_by(
            trial_family="training_selected_pooled_oos"
        )
        assert {row.status for row in primary} == {"completed"}
        assert primary.count() == 5
        assert (
            session.query(BacktestResult)
            .filter(BacktestResult.backtest_id.in_([row.trial_id for row in primary]))
            .count()
            == 5
        )
        abandoned = _abandoned_count(manifest)
        assert Counter(first.status_counts) == Counter(
            {
                "abandoned": abandoned,
                "completed": 5,
                "registered": len(manifest["trial_registry"]) - abandoned - 5,
            }
        )


def test_primary_partial_fold_filter_fails_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    calls = _stub_primary_execution(campaign, monkeypatch)

    with pytest.raises(RuntimeError, match="one checkpointed group"):
        campaign.run(
            strategy_path=_REGISTERED_CAMPAIGN,
            arm_ids=("TRAINING_SELECTED",),
            fold_ids=("oos-2022",),
        )

    assert calls == []
    with Session(campaign._engine) as session:
        primary = session.query(BacktestTrial).filter_by(
            trial_family="training_selected_pooled_oos"
        )
        assert primary.count() == 5
        assert {row.status for row in primary} == {"registered"}
        assert session.query(BacktestResult).count() == 0
        assert session.query(BacktestTrial).count() == len(manifest["trial_registry"])


def test_primary_group_resumes_after_checkpoint_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    selector_calls = _stub_primary_execution(campaign, monkeypatch)
    original_save = campaign._result_store.save_derived_evidence
    save_calls = 0

    def interrupt_after_two(*args: Any, **kwargs: Any) -> str:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 3:
            raise KeyboardInterrupt
        return original_save(*args, **kwargs)

    monkeypatch.setattr(
        campaign._result_store,
        "save_derived_evidence",
        interrupt_after_two,
    )
    with pytest.raises(KeyboardInterrupt):
        campaign.run(strategy_path=_REGISTERED_CAMPAIGN, arm_ids=("TRAINING_SELECTED",))

    with Session(campaign._engine) as session:
        primary = session.query(BacktestTrial).filter_by(
            trial_family="training_selected_pooled_oos"
        )
        assert Counter(row.status for row in primary) == {
            "completed": 2,
            "running": 3,
        }

    monkeypatch.setattr(
        campaign._result_store,
        "save_derived_evidence",
        original_save,
    )
    manifest_hash = campaign._manifest_store.store(manifest).sha256
    resumed = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=manifest_hash,
        arm_ids=("TRAINING_SELECTED",),
    )

    assert selector_calls == [1, 1]
    assert resumed.failures_this_run == 0
    with Session(campaign._engine) as session:
        primary = session.query(BacktestTrial).filter_by(
            trial_family="training_selected_pooled_oos"
        )
        assert primary.count() == 5
        assert {row.status for row in primary} == {"completed"}
        assert (
            session.query(BacktestResult)
            .filter(BacktestResult.backtest_id.in_([row.trial_id for row in primary]))
            .count()
            == 5
        )


def test_completed_primary_checkpoint_rejects_recomputed_evidence_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    original = _unit_orchestration_result()
    _stub_primary_execution(campaign, monkeypatch, result=original)
    summary = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        arm_ids=("TRAINING_SELECTED",),
    )
    changed = replace(
        original,
        windows=(
            replace(
                original.windows[0],
                selected_registry_index=(original.windows[0].selected_registry_index + 1),
            ),
            *original.windows[1:],
        ),
    )
    primary_specs = [
        spec for spec in manifest["trial_registry"] if spec["arm_id"] == "TRAINING_SELECTED"
    ]
    with Session(campaign._engine) as session:
        trial_ids = {
            row.sequence: row.trial_id
            for row in session.query(BacktestTrial).filter_by(
                trial_family="training_selected_pooled_oos"
            )
        }

    with pytest.raises(
        ValueError,
        match="derived evidence content hash cannot be changed",
    ):
        campaign._persist_primary_result(
            changed,
            primary_specs=primary_specs,
            trial_ids=trial_ids,
            experiment_id=summary.experiment_id,
            manifest=manifest,
            dataset_audits={
                "BTC-USDC": {"ohlcv_sha256": "a" * 64},
                "ETH-USDC": {"ohlcv_sha256": "b" * 64},
            },
        )

    with Session(campaign._engine) as session:
        assert (
            session.query(BacktestResult)
            .filter(BacktestResult.backtest_id.in_(trial_ids.values()))
            .count()
            == 5
        )


@pytest.mark.parametrize(
    "runner_kinds",
    [
        {"scoring_binding_ledger_component", "scoring_binding_ledger_pooled"},
        {"equal_weight_benchmark_portfolio", "pooled_oos_benchmark_aggregate"},
        {"prospective_power_study"},
        {"conditional_sizing_oos_component", "conditional_sizing_pooled_oos"},
    ],
)
def test_derived_groups_reject_partial_selection_before_execution(
    runner_kinds: set[str],
) -> None:
    protocol = _protocol()
    manifest = _manifest(protocol)
    all_specs = [spec for spec in manifest["trial_registry"] if spec["runner_kind"] in runner_kinds]

    with pytest.raises(RuntimeError, match="partial group filters are prohibited"):
        StrategyValidationCampaign._require_complete_group_selection(
            all_specs[:1],
            all_specs=manifest["trial_registry"],
        )


def test_scoring_group_replays_only_persisted_primary_ledgers_and_keeps_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    _persist_scoring_source_folds(
        campaign,
        manifest,
    )
    monkeypatch.setattr(
        campaign,
        "_build_strategy",
        lambda *_args, **_kwargs: pytest.fail("scoring replay must not rerun the core"),
    )

    summary = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=frozen.manifest_hash,
        arm_ids=("SCORING_BINDING_LEDGER",),
    )

    assert summary.failures_this_run == 0
    with Session(campaign._engine) as session:
        payloads = [
            row.meta["evidence_payload"]
            for row in session.query(BacktestResult)
            if str(row.meta.get("evidence_kind", "")).startswith("scoring_binding_ledger")
        ]
    assert len(payloads) == 3
    pooled = next(payload for payload in payloads if len(payload["scoring_binding_ledger"]) == 8)
    assert pooled["thresholds_evaluated"] is False
    assert pooled["execution_evaluated"] is False
    assert pooled["coverage"] == {
        "source_signals": 8,
        "replayed_signals": 8,
        "missing_signals": 0,
        "duplicate_signals": 0,
    }
    first_binding = pooled["scoring_binding_ledger"][0]["binding_replays"][0]
    assert first_binding["binding"]["activation_disposition"] == "inactive_binding"
    assert first_binding["binding"]["approval_disposition"] == "manual_approval_required"
    assert first_binding["entry_requirements"]["disposition"] == "blocked"
    assert first_binding["entry_requirements"]["blocked_reason"] == (
        "explicit_scoring_inputs_required"
    )


def test_scoring_group_fails_closed_on_duplicate_primary_signal_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    _persist_scoring_source_folds(
        campaign,
        manifest,
        duplicate_ids=True,
    )

    summary = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=frozen.manifest_hash,
        arm_ids=("SCORING_BINDING_LEDGER",),
    )

    assert summary.failures_this_run == 3
    with Session(campaign._engine) as session:
        scoring_trials = session.query(BacktestTrial).filter_by(
            trial_family="pipeline_reconciliation"
        )
        assert {row.status for row in scoring_trials} == {"failed"}
        assert not any(
            str(row.meta.get("evidence_kind", "")).startswith("scoring_binding_ledger")
            for row in session.query(BacktestResult)
        )


def test_power_group_zero_trades_completes_not_applicable_without_core_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    _persist_baseline_full_panel_reports(campaign, manifest)
    monkeypatch.setattr(
        campaign,
        "_build_strategy",
        lambda *_args, **_kwargs: pytest.fail("power design must not rerun the core"),
    )
    power_arm_ids = tuple(
        spec["arm_id"]
        for spec in manifest["trial_registry"]
        if spec["runner_kind"] == "prospective_power_study"
    )

    first = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=frozen.manifest_hash,
        arm_ids=power_arm_ids,
    )
    second = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=frozen.manifest_hash,
        arm_ids=power_arm_ids,
    )

    assert first.failures_this_run == second.failures_this_run == 0
    with Session(campaign._engine) as session:
        payloads = [
            row.meta["evidence_payload"]
            for row in session.query(BacktestResult)
            if row.meta.get("evidence_kind") == "prospective_power_study"
        ]
    assert len(payloads) == 3
    assert {payload["status"] for payload in payloads} == {"not_applicable_due_to_zero_trades"}
    assert {payload["not_applicable_reason"] for payload in payloads} == {
        "zero_closed_trades_in_baseline_expected_cost_full_panel"
    }
    assert all(payload["evidence_complete"] is True for payload in payloads)
    assert {payload["historical_hierarchy_implication"] for payload in payloads} == {"RETIRE"}
    assert all(payload["operationally_impractical"] is True for payload in payloads)
    with Session(campaign._engine) as session:
        source = session.query(BacktestResult).filter(BacktestResult.engine == "internal").first()
        assert source is not None
        source.trades_json = [{"tampered": True}]
        session.commit()
    with pytest.raises(RuntimeError, match="stored content does not match its hash"):
        campaign.run(
            strategy_path=_REGISTERED_CAMPAIGN,
            manifest_hash=frozen.manifest_hash,
            arm_ids=power_arm_ids,
        )


def test_power_group_uses_complete_trade_ledger_and_observed_holding_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    btc_trade = Trade(
        symbol="BTC-USDC",
        side="long",
        entry_ts=datetime(2023, 6, 1, tzinfo=UTC),
        exit_ts=datetime(2023, 6, 10, tzinfo=UTC),
        entry_price=100.0,
        exit_price=110.0,
        quantity=2.0,
        fees=1.0,
        pnl=19.0,
        gross_pnl=20.0,
    )
    eth_trade = Trade(
        symbol="ETH-USDC",
        side="long",
        entry_ts=datetime(2023, 6, 5, tzinfo=UTC),
        exit_ts=datetime(2023, 6, 12, tzinfo=UTC),
        entry_price=50.0,
        exit_price=55.0,
        quantity=4.0,
        fees=1.0,
        pnl=19.0,
        gross_pnl=20.0,
    )
    _persist_baseline_full_panel_reports(
        campaign,
        manifest,
        trades_by_product={"BTC-USDC": [btc_trade], "ETH-USDC": [eth_trade]},
        terminal_products=frozenset({"BTC-USDC", "ETH-USDC"}),
    )
    observed_inputs: list[Any] = []

    class _Search:
        minimum_duration_years = 1.0

    class _Design:
        search = _Search()

        @staticmethod
        def to_dict() -> dict[str, Any]:
            return {"minimum_duration_years": 1.0}

    def capture(observed: Any, **_kwargs: Any) -> _Design:
        observed_inputs.append(observed)
        return _Design()

    monkeypatch.setattr(
        campaign_derived_module,
        "search_prospective_duration_from_observed_trades",
        capture,
    )
    power_arm_ids = tuple(
        spec["arm_id"]
        for spec in manifest["trial_registry"]
        if spec["runner_kind"] == "prospective_power_study"
    )

    summary = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=frozen.manifest_hash,
        arm_ids=power_arm_ids,
    )

    assert summary.failures_this_run == 0
    assert len(observed_inputs) == 3
    assert all(item.matured_outcomes == 2 for item in observed_inputs)
    assert all(item.filled_entries == 4 for item in observed_inputs)
    assert all(item.right_censored_entries == 2 for item in observed_inputs)
    assert all(item.holding_overlap.maximum_concurrent_trades == 2 for item in observed_inputs)
    assert all(item.observed_independence_ratio < 1.0 for item in observed_inputs)
    with Session(campaign._engine) as session:
        payload = next(
            row.meta["evidence_payload"]
            for row in session.query(BacktestResult)
            if row.meta.get("evidence_kind") == "prospective_power_study"
        )
    outcomes = payload["completed_trade_outcomes"]
    assert [row["net_return"] for row in outcomes] == pytest.approx([0.095, 0.095])
    assert outcomes[0]["economic_entry_period_date"] == "2023-05-31"
    assert payload["cadence_source"]["terminal_position_treatment"] == (
        "filled_entry_reported_but_right_censored_outcome_excluded_from_matured_cadence_and_power"
    )


def test_conditional_sizing_abandons_every_trial_on_exact_raw_gate_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    failed_gate = "median_oos_fold_return_positive"
    _persist_robustness_gate(
        campaign,
        manifest,
        experiment_id=frozen.experiment_id,
        failed_gate_ids=(failed_gate,),
    )
    sizing_arm_ids = tuple(arm["id"] for arm in _protocol()["conditional_sizing_trials"]["arms"])

    summary = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=frozen.manifest_hash,
        arm_ids=sizing_arm_ids,
    )

    assert summary.failures_this_run == 0
    with Session(campaign._engine) as session:
        rows = list(session.query(BacktestTrial).filter_by(trial_family="conditional_sizing"))
    assert {row.status for row in rows} == {"abandoned"}
    assert {row.error_context for row in rows} == {
        "conditional_sizing_abandoned:baseline_quantitative_gate_ineligible:" + failed_gate
    }


def test_conditional_sizing_composes_domain_and_provider_evidence_from_real_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise policy composition only; this is not historical performance evidence."""

    campaign, manifest = _campaign(tmp_path, monkeypatch)
    fixture = load_market_fixture(
        _REPO / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json"
    )
    assert fixture["source"] == "coinbase_exchange_public"
    assert fixture["bar_count"] == len(fixture["bars"]) == 1501
    real_bars = bars_from_ohlcv(
        fixture["bars"],
        symbol="BTC-USDC",
        timeframe="1m",
        source=str(fixture["source"]),
    )
    dataset = next(
        item for item in manifest["data"]["datasets"] if item["requested_product"] == "BTC-USDC"
    )
    registered_specs = {
        str(spec["arm_id"]): deepcopy(spec)
        for spec in manifest["trial_registry"]
        if spec["runner_kind"] == "conditional_sizing_oos_component"
        and spec["dataset_id"] == dataset["dataset_id"]
        and spec["fold_id"] == "oos-2025"
    }
    assert set(registered_specs) == {
        "SIZE_CURRENT_BINDING_FIXED_EQUITY_1PCT",
        "SIZE_INITIAL_STOP_RISK_1PCT",
        "SIZE_TRAINING_VOL_TARGET_5PCT",
    }

    evidence: dict[str, tuple[Any, Any, dict[str, object] | None]] = {}
    for arm_id, spec in registered_specs.items():
        # The tracked fixture is later than the retired protocol window. Move
        # only this unit boundary beyond the fixture so no fabricated bars are
        # passed into the calibration path.
        spec["evaluation_start"] = "2026-06-12T00:00:00+00:00"
        evidence[arm_id] = campaign._conditional_sizing_policy(
            spec,
            dataset=dataset,
            bars=real_bars,
            annualization_factor=float(manifest["inference"]["annualization_factor"]),
        )

    for arm_id, (policy, rules, calibration) in evidence.items():
        assert policy.policy_id == arm_id
        assert policy.pre_broker_quantity_rounding.value == "price_tiered_decimals"
        assert rules.source == "frozen_coinbase_product_metadata:BTC-USDC"
        assert rules.minimum_order_notional == policy.minimum_position_notional
        if arm_id == "SIZE_TRAINING_VOL_TARGET_5PCT":
            assert calibration is not None
            assert calibration["observations_used"] > 0
            assert calibration["training_data_hash"]
        else:
            assert calibration is None


def test_conditional_sizing_zero_exposure_uses_exact_abandonment_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    _persist_robustness_gate(
        campaign,
        manifest,
        experiment_id=frozen.experiment_id,
        raw_baseline_zero_trades_or_exposure=True,
    )
    sizing_arm_ids = tuple(arm["id"] for arm in _protocol()["conditional_sizing_trials"]["arms"])

    campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=frozen.manifest_hash,
        arm_ids=sizing_arm_ids,
    )

    with Session(campaign._engine) as session:
        rows = list(session.query(BacktestTrial).filter_by(trial_family="conditional_sizing"))
    assert {row.status for row in rows} == {"abandoned"}
    assert {row.error_context for row in rows} == {"raw_baseline_zero_trades_or_exposure"}


def test_registration_only_manifest_cannot_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch, allow_execution=False)

    with pytest.raises(RuntimeError, match="registration-only"):
        campaign.run(
            strategy_path=_REGISTERED_CAMPAIGN,
            arm_ids=(manifest["historical_disposition_policy"]["baseline_arm_id"],),
        )
    with Session(campaign._engine) as session:
        rows = list(session.query(BacktestTrial))
        abandoned = _abandoned_count(manifest)
        assert Counter(row.status for row in rows) == Counter(
            {"abandoned": abandoned, "registered": len(rows) - abandoned}
        )


def test_execute_trial_builds_registered_sizing_and_explicit_fill_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    protocol = _protocol()
    config = json.loads((_REGISTERED_CAMPAIGN / "config.json").read_text())
    spec = next(
        row
        for row in manifest["trial_registry"]
        if row["arm_id"] == manifest["historical_disposition_policy"]["baseline_arm_id"]
        and row["fold_id"] == "full-panel"
        and row["cost_scenario"] == "expected"
    )
    monkeypatch.setattr(
        campaign._historical_prices,
        "load_validated_bars",
        lambda *_args, **_kwargs: ([], _Audit()),
    )
    captured: dict[str, Any] = {}

    def capture_run(_engine: BacktestEngine, strategy: Any, bars: Any, backtest_config: Any) -> str:
        captured.update(strategy=strategy, bars=bars, config=backtest_config)
        return "report"

    monkeypatch.setattr(BacktestEngine, "run_consolidated", capture_run)
    report, audit = campaign._execute_trial(
        spec,
        manifest=manifest,
        runtime_config=config,
        core_class=load_pure_strategy_core(
            _REGISTERED_CAMPAIGN,
            expected_class_name=str(protocol["strategy"]["core_class"]),
        ),
    )

    assert report == "report"
    assert audit == {"ohlcv_sha256": "a" * 64}
    assert (
        captured["config"].size_pct
        == protocol["portfolio_assumptions"]["primary_fixed_exposure_pct"]
    )
    assert captured["config"].fill_policy.timestamp_contract == "explicit_interval"
    assert captured["config"].require_flat_model_boundary is True


def test_semantic_manual_close_uses_registered_fill_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    spec = {
        "parameters": {
            "execution_policy_override": ("next_open_market_only_ignore_emitted_protective_stop")
        }
    }

    policy = campaign._fill_policy(spec, manifest=manifest)

    assert policy.policy_id == "next_open_market_only_ignore_emitted_protective_stop"
    assert policy.protective_orders is False
    assert policy.gap_through_at_open is False
    assert policy.ambiguity_policy == "ignore"
    assert policy.timestamp_contract == "explicit_interval"


def test_volatility_benchmark_calibration_cannot_see_evaluation_prices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    spec = next(
        row
        for row in manifest["trial_registry"]
        if row["arm_id"] == "BENCHMARK_VOL_TARGET_BUY_HOLD"
        and row["fold_id"] == "oos-2022"
        and row["cost_scenario"] == "expected"
    )
    cutoff = datetime.fromisoformat(str(spec["parameters"]["calibration_end_exclusive"]))
    source_start = cutoff - timedelta(days=450)

    def bars(*, future_multiplier: float) -> list[Bar]:
        values: list[Bar] = []
        for index in range(550):
            timestamp = source_start + timedelta(days=index)
            normalized_timestamp = timestamp + timedelta(days=1)
            base = 100.0 + index * 0.05 + (index % 7) * 0.2
            close = base if normalized_timestamp < cutoff else base * future_multiplier
            values.append(
                Bar(
                    symbol="BTC-USDC",
                    timestamp=timestamp,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1.0,
                    timeframe="1d",
                    source="deterministic_unit_contract_no_market_evidence",
                    metadata={"timestamp_semantics": "period_start"},
                )
            )
        return values

    baseline = campaign._benchmark_definition(
        spec["parameters"],
        bars=bars(future_multiplier=1.0),
        annualization_factor=365.0,
    )
    shocked = campaign._benchmark_definition(
        spec["parameters"],
        bars=bars(future_multiplier=100.0),
        annualization_factor=365.0,
    )

    assert baseline.volatility_calibration is not None
    assert shocked.volatility_calibration is not None
    assert baseline.volatility_calibration.training_end < cutoff
    assert (
        baseline.volatility_calibration.training_data_hash
        == shocked.volatility_calibration.training_data_hash
    )
    assert baseline.exposure == shocked.exposure


def test_benchmark_portfolio_family_persists_full_fold_and_pooled_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    component_calls = _stub_benchmark_component_execution(
        campaign,
        manifest,
        monkeypatch,
    )

    first = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        arm_ids=("BENCHMARK_CASH",),
    )
    second = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=first.manifest_hash,
        arm_ids=("BENCHMARK_CASH",),
    )

    selected = [spec for spec in manifest["trial_registry"] if spec["arm_id"] == "BENCHMARK_CASH"]
    components = [
        spec for spec in selected if spec["runner_kind"] == "executable_benchmark_component"
    ]
    assert len(component_calls) == len(components)
    assert first.failures_this_run == second.failures_this_run == 0
    with Session(campaign._engine) as session:
        selected_sequences = {int(spec["sequence"]) for spec in selected}
        trials = [
            row for row in session.query(BacktestTrial).all() if row.sequence in selected_sequences
        ]
        assert len(trials) == len(selected)
        assert {row.status for row in trials} == {"completed"}
        payloads = [
            row.meta["evidence_payload"]
            for row in session.query(BacktestResult)
            if isinstance(row.meta, dict) and "evidence_kind" in row.meta
        ]
    full_panel = next(
        payload
        for payload in payloads
        if payload.get("benchmark_portfolio", {}).get("mode") == "full_panel"
    )
    oos_fold = next(
        payload
        for payload in payloads
        if payload.get("benchmark_portfolio", {}).get("mode") == "oos_fold"
    )
    pooled = next(payload for payload in payloads if "pooled_oos" in payload)
    assert full_panel["benchmark_portfolio"]["terminal_exit_costs"] == []
    assert full_panel["oos_terminal_cost_scenario"] == ("not_applicable_unliquidated_full_panel")
    assert oos_fold["oos_terminal_cost_scenario"] == "stressed"
    assert pooled["oos_terminal_cost_scenario"] == "stressed"
    assert [row["fold_id"] for row in pooled["source_folds"]] == [
        "oos-2022",
        "oos-2023",
        "oos-2024",
        "oos-2025",
    ]


def test_fully_completed_resume_audits_verified_component_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    _stub_benchmark_component_execution(campaign, manifest, monkeypatch)
    first = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, arm_ids=("BENCHMARK_CASH",))

    with Session(campaign._engine) as session:
        row = session.query(BacktestResult).filter(BacktestResult.engine == "internal").first()
        assert row is not None
        assert row.trades_json == []
        row.trades_json = [{"tampered": True}]
        session.commit()

    with pytest.raises(RuntimeError, match="stored content does not match its hash"):
        campaign.run(
            strategy_path=_REGISTERED_CAMPAIGN,
            manifest_hash=first.manifest_hash,
            arm_ids=("BENCHMARK_CASH",),
        )


def test_benchmark_portfolio_group_resumes_and_revalidates_source_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    component_calls = _stub_benchmark_component_execution(
        campaign,
        manifest,
        monkeypatch,
    )
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    specs = _cash_expected_benchmark_specs(manifest)
    trial_ids = _trial_ids_by_sequence(campaign)
    original_save = campaign._result_store.save_derived_evidence
    monkeypatch.setattr(
        campaign._result_store,
        "save_derived_evidence",
        _interrupt_second_call(original_save),
    )
    with pytest.raises(KeyboardInterrupt):
        campaign._execute_benchmark_portfolio_group(
            specs,
            all_specs=manifest["trial_registry"],
            trial_ids=trial_ids,
            experiment_id=frozen.experiment_id,
            manifest=manifest,
            report_cache={},
            report_audit_cache={},
        )
    with Session(campaign._engine) as session:
        selected_trial_ids = {trial_ids[int(spec["sequence"])] for spec in specs}
        rows = [
            row for row in session.query(BacktestTrial).all() if row.trial_id in selected_trial_ids
        ]
        assert Counter(row.status for row in rows) == {"completed": 1, "running": 5}

    monkeypatch.setattr(
        campaign._result_store,
        "save_derived_evidence",
        original_save,
    )
    failures = campaign._execute_benchmark_portfolio_group(
        specs,
        all_specs=manifest["trial_registry"],
        trial_ids=trial_ids,
        experiment_id=frozen.experiment_id,
        manifest=manifest,
        report_cache={},
        report_audit_cache={},
    )

    assert failures == 0
    assert len(component_calls) == 20
    with Session(campaign._engine) as session:
        rows = [
            row for row in session.query(BacktestTrial).all() if row.trial_id in selected_trial_ids
        ]
        assert {row.status for row in rows} == {"completed"}


def test_benchmark_portfolio_resume_rejects_mutated_source_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    _stub_benchmark_component_execution(campaign, manifest, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    specs = _cash_expected_benchmark_specs(manifest)
    trial_ids = _trial_ids_by_sequence(campaign)
    original_save = campaign._result_store.save_derived_evidence
    monkeypatch.setattr(
        campaign._result_store,
        "save_derived_evidence",
        _interrupt_second_call(original_save),
    )
    with pytest.raises(KeyboardInterrupt):
        campaign._execute_benchmark_portfolio_group(
            specs,
            all_specs=manifest["trial_registry"],
            trial_ids=trial_ids,
            experiment_id=frozen.experiment_id,
            manifest=manifest,
            report_cache={},
            report_audit_cache={},
        )
    component_spec = next(
        spec
        for spec in manifest["trial_registry"]
        if spec["arm_id"] == "BENCHMARK_CASH"
        and spec["cost_scenario"] == "expected"
        and spec["runner_kind"] == "executable_benchmark_component"
    )
    with Session(campaign._engine) as session:
        trial = session.execute(
            select(BacktestTrial).where(
                BacktestTrial.trial_id == trial_ids[int(component_spec["sequence"])]
            )
        ).scalar_one()
        assert trial.result_id is not None
        result = session.get(BacktestResult, trial.result_id)
        assert result is not None
        assert isinstance(result.equity_curve, list)
        changed_curve = deepcopy(result.equity_curve)
        changed_curve[0][1] = float(changed_curve[0][1]) + 1.0
        result.equity_curve = changed_curve
        session.commit()
    monkeypatch.setattr(
        campaign._result_store,
        "save_derived_evidence",
        original_save,
    )

    with pytest.raises(RuntimeError, match="stored content does not match its hash"):
        campaign._execute_benchmark_portfolio_group(
            specs,
            all_specs=manifest["trial_registry"],
            trial_ids=trial_ids,
            experiment_id=frozen.experiment_id,
            manifest=manifest,
            report_cache={},
            report_audit_cache={},
        )


def test_phase_one_capability_error_lists_every_unsupported_runner() -> None:
    validator = object.__new__(StrategyValidationCampaign)

    with pytest.raises(RuntimeError) as exc_info:
        validator._require_phase_one_runner_capabilities(
            [
                {"runner_kind": "pooled_oos_benchmark_aggregate"},
                {"runner_kind": "equal_weight_benchmark_portfolio"},
                {"runner_kind": "registered_cscv_pbo"},
                {"runner_kind": "robustness_concentration_inference"},
                {"runner_kind": "registered_redesign_candidate_inference"},
                {"runner_kind": "unknown_derived_runner"},
            ]
        )

    assert str(exc_info.value).endswith("unknown_derived_runner")


def test_phase_one_dispatches_derived_evidence_in_dependency_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = object.__new__(StrategyValidationCampaign)
    calls: list[str] = []
    individual_runners = (
        "production_core_direct",
        "production_core_semantic_diagnostic",
        "executable_benchmark_component",
    )
    group_runners = (
        "anchored_joint_asset_selector",
        "scoring_binding_ledger_component",
        "prospective_power_study",
        "equal_weight_benchmark_portfolio",
        "registered_cscv_pbo",
        "robustness_concentration_inference",
        "registered_redesign_candidate_inference",
        "conditional_sizing_oos_component",
        "historical_strategy_disposition",
    )
    all_specs = [
        {
            "sequence": sequence,
            "runner_kind": runner_kind,
            "arm_id": "BENCHMARK_CASH" if "benchmark" in runner_kind else runner_kind,
            "cost_scenario": "expected",
        }
        for sequence, runner_kind in enumerate((*individual_runners, *group_runners))
    ]
    validator._trial_store = SimpleNamespace(mark_running=lambda _trial_id: None)
    monkeypatch.setattr(
        validator,
        "_trial_status",
        lambda _trial_id: "registered",
    )

    def execute_individual(
        spec: Mapping[str, Any],
        **_kwargs: Any,
    ) -> tuple[object, dict[str, Any]]:
        calls.append(str(spec["runner_kind"]))
        return object(), {}

    monkeypatch.setattr(validator, "_execute_trial", execute_individual)
    monkeypatch.setattr(validator, "_save_report_result", lambda *_args, **_kwargs: None)
    dispatch = {
        "_execute_primary_group": "primary",
        "_execute_scoring_ledger_group": "scoring",
        "_execute_power_group": "power",
        "_execute_benchmark_portfolio_group": "benchmark_portfolio",
        "_execute_cscv_group": "cscv",
        "_execute_robustness_group": "robustness",
        "_execute_redesign_group": "redesign",
        "_execute_conditional_sizing_group": "sizing",
        "_execute_historical_disposition_group": "disposition",
    }
    for method_name, label in dispatch.items():
        monkeypatch.setattr(
            validator,
            method_name,
            lambda *_args, _label=label, **_kwargs: calls.append(_label) or 0,
        )

    failures = validator._execute_phase_one(
        all_specs,
        all_specs=all_specs,
        trial_ids={int(spec["sequence"]): f"trial-{spec['sequence']}" for spec in all_specs},
        experiment_id=1,
        manifest={},
        runtime_config={},
        core_class=object,
    )

    assert failures == 0
    assert calls == [
        *individual_runners,
        "primary",
        "scoring",
        "power",
        "benchmark_portfolio",
        "cscv",
        "robustness",
        "redesign",
        "sizing",
        "disposition",
    ]
