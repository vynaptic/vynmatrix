"""Runtime, manifest, retirement-audit, and registration campaign contracts."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sys
import zipfile
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from importlib.metadata import PathDistribution
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from campaign_test_support import (
    _REGISTERED_CAMPAIGN,
    _REPO,
    _abandoned_count,
    _campaign,
    _complete_retired_disposition,
    _engine,
    _family_counts,
    _manifest,
    _protocol,
    _registered_retired_derived_trial,
    _runner_counts,
    _runtime_distribution_environment_fields,
    _store_content_addressed_protocol,
    _stub_benchmark_component_execution,
    _trial_ids_by_sequence,
)
from sqlalchemy.orm import Session

from dev_cli.validation import campaign_contracts as campaign_module
from dev_cli.validation import campaign_environment as campaign_environment_module
from dev_cli.validation import campaign_protocol_validation as campaign_protocol_module
from dev_cli.validation.backtest.engine import (
    BacktestEngine,
)
from dev_cli.validation.campaign import StrategyValidationCampaign
from dev_cli.validation.correctness import (
    verify_registered_strategy_correctness_attestation,
)
from dev_cli.validation.cscv import TimestampedDailyReturn
from dev_cli.validation.execution_environment import (
    installed_wheel_payload_sha256,
)
from dev_cli.validation.providers.coinbase_execution_costs import (
    verify_coinbase_execution_cost_measurement,
)
from lib_application.db.models import (
    BacktestExperiment,
    BacktestResult,
    BacktestTrial,
    UserStrategyBinding,
    UserStrategyConfig,
)
from lib_strategy.signals.loading import (
    load_pure_strategy_core,
)

_TEST_VMDEV_PAYLOAD = {
    "distribution": "vmdev",
    "version": "0.1.0",
    "console_entry_point": "dev_cli.main:cli",
    "file_count": 1,
    "files": {"validation/campaign.py": "a" * 64},
    "payload_sha256": "b" * 64,
}


def _attach_validation_protocol(root: Path, installed: dict[str, Any]) -> None:
    protocol = root / "validation_protocol.json"
    protocol.write_text("{}\n", encoding="utf-8")
    installed["validation_protocol_path"] = protocol.relative_to(root).as_posix()
    installed["validation_protocol_sha256"] = hashlib.sha256(protocol.read_bytes()).hexdigest()


def _attach_prior_diagnostic_attempts(
    protocol: dict[str, Any],
    *,
    repo_root: Path,
    arm_ids: tuple[str, ...] = ("PRIOR_BASELINE", "PRIOR_NEIGHBOR"),
) -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "arm_ids": list(arm_ids),
        "arms": list(arm_ids),
        "authority": "selection_contaminated_non_authoritative",
    }
    internal_sha256 = hashlib.sha256(
        json.dumps(artifact, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    artifact["screen_sha256"] = internal_sha256
    artifact_bytes = (
        json.dumps(artifact, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()
    artifact_name = f"prior-screen-{internal_sha256}.json"
    artifact_path = repo_root / artifact_name
    artifact_path.write_bytes(artifact_bytes)
    fixture = protocol["correctness_attestation"]["files"][0]
    fixture["location"] = artifact_name
    fixture["sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
    contract = {
        "arm_ids": list(arm_ids),
        "attempt_count": len(arm_ids),
        "correctness_fixture_id": fixture["id"],
        "raw_artifact_sha256": fixture["sha256"],
        "content_addressed_artifact_sha256": internal_sha256,
        "content_sha256_field": "screen_sha256",
        "selection_contaminated": True,
        "authoritative": False,
        "sharpe_dispersion_policy": "count_only_no_finite_sharpe_dispersion",
    }
    multiple_testing = protocol["multiple_testing"]
    multiple_testing["prior_diagnostic_attempts"] = contract
    combination = multiple_testing["deflated_sharpe_combination"]
    combination["upstream_attempted_trial_count"] += len(arm_ids)
    combination["effective_trial_count"] += len(arm_ids)
    return contract


def test_registered_campaign_fixture_declares_its_evidence_boundary() -> None:
    protocol = _protocol()

    assert protocol["fixture_provenance"] == {
        "derived_from": "generic_validation_unit_contract",
        "purpose": "exercise shared campaign orchestration without strategy-performance claims",
        "production_authority": False,
        "provider_fixture_integrity_evidence": True,
        "strategy_performance_evidence": False,
    }
    data = protocol["data"]
    assert (
        data["candle_endpoint_kind"],
        data["provider_granularity"],
        data["strategy_resolution"],
        data["product_metadata_observed_at_utc"],
        data["minute_parity"]["sample_policy"],
    ) == (
        "public_advanced_trade",
        "ONE_DAY",
        "1d_utc",
        "2026-07-21T19:39:38Z",
        "pre_registered_stratified_year_and_regime_windows",
    )


def test_registered_redesign_accepts_only_named_coupled_factor_changes() -> None:
    protocol = _protocol()
    protocol["baseline_parameters"].update({"lower_quantile": 0.2, "upper_quantile": 0.8})
    protocol["signal_rule_arms"][1] = {
        "id": "NEIGHBOR",
        "factor_name": "symmetric_quantile_pair",
        "overrides": {"lower_quantile": 0.15, "upper_quantile": 0.85},
    }
    validator = object.__new__(StrategyValidationCampaign)

    assert validator._registered_redesign_change_contracts(protocol) == [
        {
            "candidate_id": "NEIGHBOR",
            "parameter_name": "symmetric_quantile_pair",
            "baseline_value": {"lower_quantile": 0.2, "upper_quantile": 0.8},
            "candidate_value": {"lower_quantile": 0.15, "upper_quantile": 0.85},
        }
    ]

    del protocol["signal_rule_arms"][1]["factor_name"]
    with pytest.raises(ValueError, match="factor_name"):
        validator._registered_redesign_change_contracts(protocol)


def test_protocol_accepts_exact_volume_dependent_parity() -> None:
    protocol = _protocol()
    parity = protocol["data"]["minute_parity"]
    parity["strategy_field_dependency"]["uses_volume"] = True
    summary = parity["exact_attestation"]["summary"]
    summary["full_ohlcv_matches"] = summary["sampled_daily_bars"]
    summary["volume_only_mismatches"] = 0
    summary["status"] = "exact_full_ohlcv_match"
    summary["campaign_disposition"] = "eligible_for_volume_dependent_ohlcv_rules"
    observed = parity["observed_reconciliation"]
    observed["full_ohlcv_matches"] = summary["sampled_daily_bars"]
    observed["volume_only_mismatches"] = 0
    observed["status"] = "exact_full_ohlcv_match"
    validator = object.__new__(StrategyValidationCampaign)

    validator._validate_data_parity_contract(
        protocol,
        products={"BTC-USDC", "ETH-USDC"},
    )


def test_protocol_rejects_volume_mismatch_for_volume_dependent_strategy() -> None:
    protocol = _protocol()
    parity = protocol["data"]["minute_parity"]
    parity["strategy_field_dependency"]["uses_volume"] = True
    validator = object.__new__(StrategyValidationCampaign)

    with pytest.raises(ValueError, match="exact summary is incomplete or blocking"):
        validator._validate_data_parity_contract(
            protocol,
            products={"BTC-USDC", "ETH-USDC"},
        )


def test_expected_daily_timestamps_use_attested_common_calendar() -> None:
    campaign = object.__new__(StrategyValidationCampaign)
    start = datetime(2020, 1, 2, tzinfo=UTC)
    spec = {
        "evaluation_start": start.isoformat(),
        "evaluation_end_exclusive": (start + timedelta(days=4)).isoformat(),
    }
    ledger = [
        {
            "period_start": (start - timedelta(days=1) + timedelta(days=index)).isoformat(),
            "included": included,
        }
        for index, included in enumerate((True, False, True, False))
    ]
    manifest = {
        "data_parity_attestation": {
            "common_calendar": {
                "candidate_days": 4,
                "included_day_count": 2,
                "excluded_day_count": 2,
                "ledger": ledger,
            }
        }
    }

    assert campaign._expected_daily_timestamps(spec, manifest=manifest) == (
        start,
        start + timedelta(days=2),
    )

    fallback = {"data_parity_attestation": {}}
    assert campaign._expected_daily_timestamps(spec, manifest=fallback) == tuple(
        start + timedelta(days=index) for index in range(4)
    )

    malformed = deepcopy(manifest)
    malformed["data_parity_attestation"]["common_calendar"]["included_day_count"] = 3
    with pytest.raises(ValueError, match="included count"):
        campaign._expected_daily_timestamps(spec, manifest=malformed)


def test_protocol_validation_accepts_only_the_exact_legacy_zero_activity_contract() -> None:
    protocol = _protocol()
    legacy_contract = {
        "metric_callable": "lib_strategy.backtest.metrics.sharpe_ratio",
        "complete_aligned_zero_return_zero_variance_annualized_sharpe": 0.0,
        "missing_corrupt_or_misaligned": "BLOCKED_INSUFFICIENT_EVIDENCE",
        "b0_zero_activity_disposition_gate": "separate_RETIRE_gate",
    }
    protocol["multiple_testing"]["zero_activity_current_arm_sharpe"] = legacy_contract
    validator = object.__new__(StrategyValidationCampaign)

    validator._validate_upstream_selection_contract(protocol)

    legacy_contract["complete_aligned_zero_return_zero_variance_annualized_sharpe"] = 0.1
    with pytest.raises(ValueError, match="zero-activity current-arm Sharpe contract differs"):
        validator._validate_upstream_selection_contract(protocol)


def test_prior_diagnostic_attempts_are_counted_without_changing_the_upstream_ledger(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    ledger_contract = deepcopy(protocol["multiple_testing"]["upstream_selection_ledger_contract"])
    _attach_prior_diagnostic_attempts(protocol, repo_root=tmp_path)
    validator = object.__new__(StrategyValidationCampaign)
    validator._repo_root = tmp_path

    validator._validate_upstream_selection_contract(protocol)

    assert protocol["multiple_testing"]["upstream_selection_ledger_contract"] == ledger_contract
    combination = protocol["multiple_testing"]["deflated_sharpe_combination"]
    assert combination["upstream_attempted_trial_count"] == 6
    assert combination["effective_trial_count"] == 9


def test_prior_diagnostic_attempts_require_exact_unique_arm_count(tmp_path: Path) -> None:
    protocol = _protocol()
    contract = _attach_prior_diagnostic_attempts(
        protocol,
        repo_root=tmp_path,
        arm_ids=("PRIOR_BASELINE", "PRIOR_BASELINE"),
    )
    validator = object.__new__(StrategyValidationCampaign)
    validator._repo_root = tmp_path

    with pytest.raises(ValueError, match="must be non-empty and unique"):
        validator._validate_upstream_selection_contract(protocol)

    contract["arm_ids"] = ["PRIOR_BASELINE", "PRIOR_NEIGHBOR"]
    contract["attempt_count"] = 1
    with pytest.raises(ValueError, match="attempt_count differs"):
        validator._validate_upstream_selection_contract(protocol)


def test_prior_diagnostic_attempts_require_artifact_arm_identity(tmp_path: Path) -> None:
    protocol = _protocol()
    contract = _attach_prior_diagnostic_attempts(protocol, repo_root=tmp_path)
    contract["arm_ids"] = ["PRIOR_BASELINE", "PRIOR_REPLACEMENT"]
    validator = object.__new__(StrategyValidationCampaign)
    validator._repo_root = tmp_path

    with pytest.raises(ValueError, match="artifact arm IDs differ"):
        validator._validate_upstream_selection_contract(protocol)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("selection_contaminated", False, "evidence authority contract differs"),
        ("authoritative", True, "evidence authority contract differs"),
        (
            "sharpe_dispersion_policy",
            "include_finite_values",
            "evidence authority contract differs",
        ),
        ("raw_artifact_sha256", "invalid", "raw_artifact_sha256 is invalid"),
        (
            "content_addressed_artifact_sha256",
            "invalid",
            "content_addressed_artifact_sha256 is invalid",
        ),
    ],
)
def test_prior_diagnostic_attempts_reject_ambiguous_authority_or_hashes(
    field: str,
    value: object,
    message: str,
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    contract = _attach_prior_diagnostic_attempts(protocol, repo_root=tmp_path)
    contract[field] = value
    validator = object.__new__(StrategyValidationCampaign)
    validator._repo_root = tmp_path

    with pytest.raises(ValueError, match=message):
        validator._validate_upstream_selection_contract(protocol)


def test_prior_diagnostic_attempts_require_exact_correctness_fixture_binding(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    contract = _attach_prior_diagnostic_attempts(protocol, repo_root=tmp_path)
    validator = object.__new__(StrategyValidationCampaign)
    validator._repo_root = tmp_path

    contract["raw_artifact_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="differs from its correctness fixture binding"):
        validator._validate_upstream_selection_contract(protocol)

    contract["raw_artifact_sha256"] = protocol["correctness_attestation"]["files"][0]["sha256"]
    contract["correctness_fixture_id"] = "fixture.missing"
    with pytest.raises(ValueError, match="no exact correctness fixture binding"):
        validator._validate_upstream_selection_contract(protocol)


def test_prior_diagnostic_attempts_verify_internal_content_digest(tmp_path: Path) -> None:
    protocol = _protocol()
    contract = _attach_prior_diagnostic_attempts(protocol, repo_root=tmp_path)
    fixture = protocol["correctness_attestation"]["files"][0]
    artifact_path = tmp_path / fixture["location"]
    artifact = json.loads(artifact_path.read_text())
    artifact["authority"] = "tampered"
    artifact_bytes = (
        json.dumps(artifact, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()
    artifact_path.write_bytes(artifact_bytes)
    raw_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    fixture["sha256"] = raw_sha256
    contract["raw_artifact_sha256"] = raw_sha256
    validator = object.__new__(StrategyValidationCampaign)
    validator._repo_root = tmp_path

    with pytest.raises(ValueError, match="canonical content SHA-256 differs"):
        validator._validate_upstream_selection_contract(protocol)


def test_prior_diagnostic_attempts_require_registered_content_digest_field(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    contract = _attach_prior_diagnostic_attempts(protocol, repo_root=tmp_path)
    contract["content_sha256_field"] = "unregistered_sha256"
    validator = object.__new__(StrategyValidationCampaign)
    validator._repo_root = tmp_path

    with pytest.raises(ValueError, match="internal content SHA-256 differs"):
        validator._validate_upstream_selection_contract(protocol)


def test_protocol_without_prior_diagnostics_preserves_legacy_trial_arithmetic() -> None:
    protocol = _protocol()
    validator = object.__new__(StrategyValidationCampaign)

    validator._validate_upstream_selection_contract(protocol)

    combination = protocol["multiple_testing"]["deflated_sharpe_combination"]
    assert combination["upstream_attempted_trial_count"] == 4
    assert combination["effective_trial_count"] == 7


def test_pre_1_4_dataset_asset_class_resolves_only_from_frozen_habitat() -> None:
    validator = object.__new__(StrategyValidationCampaign)
    manifest = {"habitat": {"asset_class_db": "crypto"}}

    assert validator._dataset_asset_class({}, manifest=manifest) == "crypto"
    assert validator._dataset_asset_class({"asset_class": "equity"}, manifest=manifest) == "equity"
    with pytest.raises(ValueError, match=r"dataset\.asset_class must be a non-empty string"):
        validator._dataset_asset_class({}, manifest={"habitat": {}})


def test_execution_attestation_checks_caller_supplied_interpreter_digest() -> None:
    validator = object.__new__(StrategyValidationCampaign)

    with pytest.raises(ValueError, match="interpreter digest differs"):
        validator._validate_installed_artifacts(
            _REGISTERED_CAMPAIGN,
            {
                "schema_version": "1.0",
                "venv_python": sys.executable,
                "venv_python_sha256": "0" * 64,
            },
        )


def test_execution_attestation_rejects_a_different_venv_symlink_to_same_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first-venv" / "bin" / "python"
    second = tmp_path / "second-venv" / "bin" / "python"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.symlink_to(Path(sys.executable).resolve())
    second.symlink_to(Path(sys.executable).resolve())
    monkeypatch.setattr(sys, "executable", str(second))
    validator = object.__new__(StrategyValidationCampaign)

    with pytest.raises(ValueError, match="exact attested validation virtual environment"):
        validator._validate_installed_artifacts(
            _REGISTERED_CAMPAIGN,
            {
                "schema_version": "1.0",
                "venv_python": str(first),
                "venv_python_sha256": validator._file_sha256(first),
            },
        )


def test_execution_attestation_rejects_container_tag_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_path = tmp_path / "strategies" / "indicator" / "RegisteredCampaign"
    strategy_path.mkdir(parents=True)
    payloads = {
        "core": b"class RegisteredCampaignCore: pass\n",
        "config": b"{}\n",
        "protocol": b'{"strategy":{"core_class":"RegisteredCampaignCore"}}\n',
    }
    source_names = {
        "core": "core.py",
        "config": "config.json",
        "protocol": "validation_protocol.json",
    }
    for key, filename in source_names.items():
        (strategy_path / filename).write_bytes(payloads[key])

    wheel_names = (
        "lib_application",
        "lib_common",
        "lib_data",
        "lib_indicators",
        "lib_infrastructure",
        "lib_strategy",
        "vynmatrix_indicator",
    )
    wheels: list[dict[str, str]] = []
    for name in wheel_names:
        wheel = tmp_path / "build" / "wheels" / f"{name}-0.1.0-py3-none-any.whl"
        wheel.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(wheel, "w") as archive:
            if name == "vynmatrix_indicator":
                for key in ("core", "config"):
                    filename = source_names[key]
                    archive.writestr(f"RegisteredCampaign/{filename}", payloads[key])
        wheels.append(
            {
                "name": name,
                "path": str(wheel),
                "sha256": StrategyValidationCampaign._file_sha256(wheel),
                "installed_payload_sha256": "b" * 64,
            }
        )

    installed_core = tmp_path / "site-packages" / "RegisteredCampaign" / "core.py"
    installed_core.parent.mkdir(parents=True)
    installed_core.write_bytes(payloads["core"])
    installed_module_name = "test_attested_registered_campaign_core"
    monkeypatch.setitem(
        sys.modules,
        installed_module_name,
        SimpleNamespace(__file__=str(installed_core)),
    )
    installed_class = type(
        "RegisteredCampaignCore",
        (),
        {"__module__": installed_module_name},
    )
    monkeypatch.setattr(
        campaign_environment_module,
        "installed_wheel_payload_sha256",
        lambda *_args, **_kwargs: "b" * 64,
    )
    monkeypatch.setattr(
        campaign_environment_module,
        "load_installed_pure_strategy_core",
        lambda *_args, **_kwargs: installed_class,
    )
    monkeypatch.setattr(
        campaign_environment_module,
        "installed_vmdev_payload",
        lambda *_args, **_kwargs: deepcopy(_TEST_VMDEV_PAYLOAD),
    )

    expected_digest = f"sha256:{'1' * 64}"
    validator = object.__new__(StrategyValidationCampaign)
    validator._repo_root = tmp_path
    monkeypatch.setattr(
        validator,
        "_local_container_image_id",
        lambda _reference: expected_digest,
    )
    attestation = {
        "schema_version": "1.0",
        "venv_python": sys.executable,
        "venv_python_sha256": validator._file_sha256(Path(sys.executable)),
        "vmdev": deepcopy(_TEST_VMDEV_PAYLOAD),
        "wheels": wheels,
        "strategy_payload_paths": {
            "core": "RegisteredCampaign/core.py",
            "config": "RegisteredCampaign/config.json",
            "protocol": "RegisteredCampaign/validation_protocol.json",
        },
        "strategy_payload_sha256": {
            key: hashlib.sha256(payload).hexdigest() for key, payload in payloads.items()
        },
        "container_image_digests": {"indicator-runner": expected_digest},
        "container_image_references": {"indicator-runner": "vynmatrix/platform:validation"},
    }

    hash_drift = deepcopy(attestation)
    hash_drift["strategy_payload_sha256"]["protocol"] = "0" * 64
    with pytest.raises(ValueError, match="payload digest differs"):
        validator._validate_installed_artifacts(strategy_path, hash_drift)

    validated = validator._validate_installed_artifacts(strategy_path, attestation)
    assert validated["validation_protocol_path"].endswith("validation_protocol.json")
    assert (
        validated["validation_protocol_sha256"] == hashlib.sha256(payloads["protocol"]).hexdigest()
    )
    retired_image = deepcopy(attestation)
    retired_image["container_image_references"]["indicator-runner"] = (
        "vynmatrix/indicator-runner:validation"
    )
    with pytest.raises(ValueError, match="vynmatrix/platform"):
        validator._validate_installed_artifacts(strategy_path, retired_image)

    monkeypatch.setattr(
        validator,
        "_local_container_image_id",
        lambda _reference: f"sha256:{'2' * 64}",
    )
    with pytest.raises(ValueError, match="local container image digest differs"):
        validator._validate_installed_artifacts(strategy_path, attestation)


def test_resumed_campaign_rejects_mutated_installed_wheel_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    venv = tmp_path / "strategy-validation"
    site = venv / "lib" / "python3.11" / "site-packages"
    package = site / "lib_common"
    package.mkdir(parents=True)
    package_payload = b'VALUE = "installed"\n'
    (package / "__init__.py").write_bytes(package_payload)
    dist_info = site / "lib_common-0.1.0.dist-info"
    dist_info.mkdir()
    metadata_payload = b"Metadata-Version: 2.1\nName: lib-common\nVersion: 0.1.0\n"
    wheel_payload = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    (dist_info / "METADATA").write_bytes(metadata_payload)
    (dist_info / "WHEEL").write_bytes(wheel_payload)
    distribution = PathDistribution(dist_info)
    monkeypatch.setattr(importlib.metadata, "distribution", lambda _name: distribution)
    monkeypatch.setattr(sys, "prefix", str(venv))

    wheel = tmp_path / "build" / "wheels" / "lib_common-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir(parents=True)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("lib_common/__init__.py", package_payload)
        archive.writestr("lib_common-0.1.0.dist-info/METADATA", metadata_payload)
        archive.writestr("lib_common-0.1.0.dist-info/WHEEL", wheel_payload)
        archive.writestr("lib_common-0.1.0.dist-info/RECORD", "")
    payload_sha256 = installed_wheel_payload_sha256(
        wheel,
        "lib_common",
        installation_root=venv,
    )
    installed_core = venv / "frozen-core.py"
    installed_core.write_text("CORE = True\n")
    interpreter = Path(sys.executable).absolute()
    validator = object.__new__(StrategyValidationCampaign)
    validator._repo_root = tmp_path
    container_digest = f"sha256:{'c' * 64}"
    monkeypatch.setattr(
        validator,
        "_local_container_image_id",
        lambda _reference: container_digest,
    )
    monkeypatch.setattr(
        campaign_environment_module,
        "installed_vmdev_payload",
        lambda *_args, **_kwargs: deepcopy(_TEST_VMDEV_PAYLOAD),
    )
    manifest = {
        "environment": {
            **_runtime_distribution_environment_fields(monkeypatch),
            "execution_authorized": True,
            "execution_installation": "installed_wheels_and_pinned_container",
            "container_artifact_attested": True,
            "installed_artifacts": {
                "venv_python": str(interpreter),
                "venv_python_sha256": validator._file_sha256(interpreter),
                "vmdev": deepcopy(_TEST_VMDEV_PAYLOAD),
                "wheels": [
                    {
                        "name": "lib_common",
                        "path": str(wheel.relative_to(tmp_path)),
                        "sha256": validator._file_sha256(wheel),
                        "installed_payload_sha256": payload_sha256,
                    }
                ],
                "installed_strategy_core_path": str(installed_core),
                "installed_strategy_core_sha256": validator._file_sha256(installed_core),
                "container_image_digests": {"indicator-runner": container_digest},
                "container_image_references": {"indicator-runner": "vynmatrix/platform:validation"},
            },
        }
    }
    _attach_validation_protocol(tmp_path, manifest["environment"]["installed_artifacts"])

    validator._require_execution_environment(manifest)
    (package / "__init__.py").write_text("TAMPERED = True\n")

    with pytest.raises(RuntimeError, match="installed wheel payload differs"):
        validator._require_execution_environment(manifest)


def test_resumed_campaign_rejects_container_tag_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreter = Path(sys.executable).absolute()
    installed_core = tmp_path / "site-packages" / "RegisteredCampaign" / "core.py"
    installed_core.parent.mkdir(parents=True)
    installed_core.write_text("CORE = True\n")
    expected_digest = f"sha256:{'d' * 64}"
    observed_digest = expected_digest
    observed_vmdev = deepcopy(_TEST_VMDEV_PAYLOAD)
    validator = object.__new__(StrategyValidationCampaign)
    validator._repo_root = tmp_path
    monkeypatch.setattr(
        validator,
        "_local_container_image_id",
        lambda _reference: observed_digest,
    )
    monkeypatch.setattr(
        campaign_environment_module,
        "installed_vmdev_payload",
        lambda *_args, **_kwargs: deepcopy(observed_vmdev),
    )
    manifest = {
        "environment": {
            **_runtime_distribution_environment_fields(monkeypatch),
            "execution_authorized": True,
            "execution_installation": "installed_wheels_and_pinned_container",
            "container_artifact_attested": True,
            "installed_artifacts": {
                "venv_python": str(interpreter),
                "venv_python_sha256": validator._file_sha256(interpreter),
                "vmdev": deepcopy(_TEST_VMDEV_PAYLOAD),
                "wheels": [],
                "installed_strategy_core_path": str(installed_core),
                "installed_strategy_core_sha256": validator._file_sha256(installed_core),
                "container_image_digests": {"indicator-runner": expected_digest},
                "container_image_references": {"indicator-runner": "vynmatrix/platform:validation"},
            },
        }
    }
    _attach_validation_protocol(tmp_path, manifest["environment"]["installed_artifacts"])

    validator._require_execution_environment(manifest)
    retired_image = deepcopy(manifest)
    retired_image["environment"]["installed_artifacts"]["container_image_references"][
        "indicator-runner"
    ] = "vynmatrix/indicator-runner:validation"
    with pytest.raises(RuntimeError, match="vynmatrix/platform"):
        validator._require_execution_environment(retired_image)
    observed_vmdev["payload_sha256"] = "c" * 64
    with pytest.raises(RuntimeError, match="active vmdev runner differs"):
        validator._require_execution_environment(manifest)
    observed_vmdev = deepcopy(_TEST_VMDEV_PAYLOAD)
    observed_digest = f"sha256:{'e' * 64}"

    with pytest.raises(RuntimeError, match="local container image changed"):
        validator._require_execution_environment(manifest)


def test_runtime_distribution_snapshot_canonicalizes_and_rejects_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = SimpleNamespace(metadata={"Name": "Alpha_Beta"}, version="1.2.3")
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: (first,))

    rows, digest = campaign_module._runtime_distribution_snapshot()

    assert rows == ("alpha-beta==1.2.3",)
    assert digest == hashlib.sha256(b"alpha-beta==1.2.3").hexdigest()

    duplicate = SimpleNamespace(metadata={"Name": "alpha-beta"}, version="1.2.3")
    monkeypatch.setattr(
        importlib.metadata,
        "distributions",
        lambda: (first, duplicate),
    )
    with pytest.raises(ValueError, match="duplicate distribution: alpha-beta"):
        campaign_module._runtime_distribution_snapshot()


@pytest.mark.parametrize(
    "rows",
    [
        ["Alpha_Beta==1.2.3"],
        ["alpha-beta==1.2.3", "alpha-beta==1.2.3"],
        ["zeta==1.0", "alpha==1.0"],
    ],
)
def test_frozen_runtime_distribution_lock_rejects_noncanonical_rows(
    rows: list[str],
) -> None:
    digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()

    with pytest.raises((TypeError, ValueError), match=r"canonical|duplicate-free"):
        campaign_module._validated_frozen_runtime_distribution_lock(
            {
                "runtime_distributions": rows,
                "runtime_distribution_lock_sha256": digest,
            }
        )


def test_frozen_runtime_distribution_lock_uses_distribution_name_order() -> None:
    rows = (
        "grpcio==1.81.1",
        "grpcio-status==1.81.1",
        "pyasn1==0.6.3",
        "pyasn1-modules==0.4.2",
    )
    digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()

    assert campaign_module._validated_frozen_runtime_distribution_lock(
        {
            "runtime_distributions": list(rows),
            "runtime_distribution_lock_sha256": digest,
        }
    ) == (rows, digest)

    duplicate_name_rows = ["grpcio==1.81.1", "grpcio==1.81.2"]
    duplicate_digest = hashlib.sha256("\n".join(duplicate_name_rows).encode("utf-8")).hexdigest()
    with pytest.raises(ValueError, match="duplicate-free"):
        campaign_module._validated_frozen_runtime_distribution_lock(
            {
                "runtime_distributions": duplicate_name_rows,
                "runtime_distribution_lock_sha256": duplicate_digest,
            }
        )


def test_decision_returns_are_keyed_to_registered_economic_period() -> None:
    decision_returns = (
        TimestampedDailyReturn(datetime(2022, 1, 2, tzinfo=UTC), 0.01),
        TimestampedDailyReturn(datetime(2022, 1, 3, tzinfo=UTC), -0.02),
    )

    economic_returns = StrategyValidationCampaign._economic_period_returns(
        decision_returns,
        date_rule="normalized_close_timestamp_minus_one_calendar_day",
    )

    assert tuple(item.timestamp for item in economic_returns) == (
        datetime(2022, 1, 1, tzinfo=UTC),
        datetime(2022, 1, 2, tzinfo=UTC),
    )
    assert tuple(item.daily_return for item in economic_returns) == (0.01, -0.02)
    with pytest.raises(ValueError, match="unsupported economic-period"):
        StrategyValidationCampaign._economic_period_returns(
            decision_returns,
            date_rule="decision_close_date",
        )


def test_adaptive_returns_verify_raw_sources_before_date_adaptation() -> None:
    decision_returns = (
        TimestampedDailyReturn(datetime(2022, 1, 2, tzinfo=UTC), 0.01),
        TimestampedDailyReturn(datetime(2022, 1, 3, tzinfo=UTC), -0.02),
    )

    economic_returns = StrategyValidationCampaign._verified_economic_period_returns(
        decision_returns,
        tuple(decision_returns),
        date_rule="normalized_close_timestamp_minus_one_calendar_day",
        field="adaptive pooled evidence",
    )

    assert tuple(item.timestamp for item in economic_returns) == (
        datetime(2022, 1, 1, tzinfo=UTC),
        datetime(2022, 1, 2, tzinfo=UTC),
    )
    assert tuple(item.timestamp for item in decision_returns) == (
        datetime(2022, 1, 2, tzinfo=UTC),
        datetime(2022, 1, 3, tzinfo=UTC),
    )
    with pytest.raises(RuntimeError, match="adaptive pooled evidence differs"):
        StrategyValidationCampaign._verified_economic_period_returns(
            decision_returns,
            decision_returns[:-1],
            date_rule="normalized_close_timestamp_minus_one_calendar_day",
            field="adaptive pooled evidence",
        )


def test_resumed_campaign_rejects_runtime_distribution_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreter = Path(sys.executable).absolute()
    installed_core = tmp_path / "site-packages" / "RegisteredCampaign" / "core.py"
    installed_core.parent.mkdir(parents=True)
    installed_core.write_text("CORE = True\n")
    container_digest = f"sha256:{'f' * 64}"
    validator = object.__new__(StrategyValidationCampaign)
    validator._repo_root = tmp_path
    monkeypatch.setattr(
        validator,
        "_local_container_image_id",
        lambda _reference: container_digest,
    )
    monkeypatch.setattr(
        campaign_environment_module,
        "installed_vmdev_payload",
        lambda *_args, **_kwargs: deepcopy(_TEST_VMDEV_PAYLOAD),
    )
    runtime_fields = _runtime_distribution_environment_fields(monkeypatch)
    frozen_rows = tuple(runtime_fields["runtime_distributions"])
    frozen_digest = str(runtime_fields["runtime_distribution_lock_sha256"])
    manifest = {
        "environment": {
            **runtime_fields,
            "execution_authorized": True,
            "execution_installation": "installed_wheels_and_pinned_container",
            "container_artifact_attested": True,
            "installed_artifacts": {
                "venv_python": str(interpreter),
                "venv_python_sha256": validator._file_sha256(interpreter),
                "vmdev": deepcopy(_TEST_VMDEV_PAYLOAD),
                "wheels": [],
                "installed_strategy_core_path": str(installed_core),
                "installed_strategy_core_sha256": validator._file_sha256(installed_core),
                "container_image_digests": {"indicator-runner": container_digest},
                "container_image_references": {"indicator-runner": "vynmatrix/platform:validation"},
            },
        }
    }
    _attach_validation_protocol(tmp_path, manifest["environment"]["installed_artifacts"])

    validator._require_execution_environment(manifest)
    drifted_rows = (*frozen_rows, "zz-runtime-drift==1.0")
    drifted_digest = hashlib.sha256("\n".join(drifted_rows).encode("utf-8")).hexdigest()
    monkeypatch.setattr(
        campaign_environment_module,
        "_runtime_distribution_snapshot",
        lambda: (drifted_rows, drifted_digest),
    )

    with pytest.raises(RuntimeError, match="runtime distributions differ"):
        validator._require_execution_environment(manifest)


def test_environment_manifest_hashes_only_current_validation_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_path = tmp_path / "strategies" / "indicator" / "RegisteredCampaign"
    strategy_path.mkdir(parents=True)
    for name in ("core.py", "config.json", "validation_protocol.json"):
        (strategy_path / name).write_text(f"{name}-v1", encoding="utf-8")
    validation_root = tmp_path / "tools" / "dev_cli" / "dev_cli" / "validation"
    provider_root = validation_root / "providers"
    provider_root.mkdir(parents=True)
    campaign_source = validation_root / "campaign.py"
    campaign_source.write_text("campaign-v1", encoding="utf-8")
    (provider_root / "coinbase.py").write_text("provider-v1", encoding="utf-8")
    ignored_cache = validation_root / "__pycache__"
    ignored_cache.mkdir()
    (ignored_cache / "ignored.py").write_text("ignored", encoding="utf-8")

    validator = StrategyValidationCampaign(
        _engine(),
        repo_root=tmp_path,
        artifact_root=tmp_path / ".artifacts",
        execution_cost_measurement_verifier=lambda _payload: None,
        data_parity_attestation_verifier=lambda _payload: None,
    )
    monkeypatch.setattr(validator, "_git_output", lambda _args: "commit-id")
    monkeypatch.setattr(
        campaign_environment_module,
        "_runtime_distribution_snapshot",
        lambda: (("dependency==1.0",), "d" * 64),
    )

    first = validator._environment_manifest(strategy_path, execution_environment=None)
    first_sources = first["source_files"]
    assert isinstance(first_sources, dict)
    assert "tools/dev_cli/dev_cli/validation/campaign.py" in first_sources
    assert "tools/dev_cli/dev_cli/validation/providers/coinbase.py" in first_sources
    assert all("__pycache__" not in path for path in first_sources)
    assert all(
        "libs/python/lib_strategy/lib_strategy/backtest" not in path for path in first_sources
    )
    assert all("strategy_validation_campaign.py" not in path for path in first_sources)

    campaign_source.write_text("campaign-v2", encoding="utf-8")
    second = validator._environment_manifest(strategy_path, execution_environment=None)
    second_sources = second["source_files"]
    assert isinstance(second_sources, dict)
    assert (
        second_sources["tools/dev_cli/dev_cli/validation/campaign.py"]
        != first_sources["tools/dev_cli/dev_cli/validation/campaign.py"]
    )


def test_retired_campaign_audit_uses_only_attested_protocol_and_db_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    _stub_benchmark_component_execution(campaign, manifest, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, arm_ids=("BENCHMARK_CASH",))
    monkeypatch.setattr(
        campaign,
        "_load_json_object",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("audit must not read a checked-in protocol or config")
        ),
    )
    monkeypatch.setattr(
        campaign,
        "_load_and_validate_core",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("audit must not load retired source")
        ),
    )
    monkeypatch.setattr(
        campaign,
        "_require_frozen_runtime_environment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("audit must not require a pruned runtime")
        ),
    )

    audited = campaign.audit(
        manifest_hash=frozen.manifest_hash,
        expected_strategy_name="registered_campaign",
    )

    assert audited.manifest_hash == frozen.manifest_hash
    assert audited.experiment_id == frozen.experiment_id
    assert audited.total_trials == len(manifest["trial_registry"])
    assert audited.selected_trials == 0
    assert audited.status_counts == frozen.status_counts
    assert audited.failures_this_run == 0


def test_retired_registry_accepts_only_the_exact_legacy_portfolio_identity() -> None:
    manifest = _manifest(_protocol())
    spec = next(row for row in manifest["trial_registry"] if row.get("component_datasets"))
    spec["dataset_id"] = spec["dataset_id"].replace(
        "derived:equal-weight-portfolio:",
        "synthetic:equal-weight-portfolio:",
        1,
    )
    validator = object.__new__(StrategyValidationCampaign)

    assert validator._validated_frozen_trial_specs_for_audit(manifest)

    spec["dataset_id"] = spec["dataset_id"].replace("synthetic:", "unknown:", 1)
    with pytest.raises(ValueError, match="derived dataset identity differs"):
        validator._validated_frozen_trial_specs_for_audit(manifest)


def test_retired_campaign_audit_rejects_protocol_with_invalid_content_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _manifest_fixture = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    protocol = _protocol()
    invalid_digest = "0" * 64
    path = (
        tmp_path
        / ".artifacts"
        / "research"
        / "protocols"
        / "sha256"
        / invalid_digest[:2]
        / f"{invalid_digest}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(protocol), encoding="utf-8")

    with pytest.raises(ValueError, match="protocol artifact content address is invalid"):
        campaign.audit(manifest_hash=frozen.manifest_hash)


def test_retired_campaign_audit_rejects_missing_db_trial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _manifest_fixture = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    with Session(campaign._engine) as session:
        row = session.query(BacktestTrial).order_by(BacktestTrial.sequence.desc()).first()
        assert row is not None
        session.connection().exec_driver_sql(
            "DELETE FROM backtest_trials WHERE trial_id = ?",
            (row.trial_id,),
        )
        session.commit()

    with pytest.raises(RuntimeError, match="stored trial count differs"):
        campaign.audit(manifest_hash=frozen.manifest_hash)


def test_retired_campaign_audit_accepts_frozen_family_absent_from_current_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    manifest_hash, experiment_id, _trial_id = _registered_retired_derived_trial(
        campaign,
        manifest,
    )
    _store_content_addressed_protocol(tmp_path, _protocol())
    monkeypatch.setattr(
        campaign,
        "_build_trial_registry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("retired audit must not rebuild the executable registry")
        ),
    )

    summary = campaign.audit(manifest_hash=manifest_hash)

    assert summary.manifest_hash == manifest_hash
    assert summary.experiment_id == experiment_id
    assert summary.total_trials == len(manifest["trial_registry"])
    assert summary.status_counts["completed"] == 1


def test_retired_campaign_audit_verifies_stored_verdict_without_current_rederivation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    manifest_hash, experiment_id, _retired_trial_id = _registered_retired_derived_trial(
        campaign,
        manifest,
    )
    _complete_retired_disposition(campaign, manifest, experiment_id=experiment_id)
    _store_content_addressed_protocol(tmp_path, _protocol())
    monkeypatch.setattr(
        campaign,
        "_reaudit_completed_historical_disposition",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("retired audit must not rederive with pruned policy code")
        ),
    )

    summary = campaign.audit(manifest_hash=manifest_hash)

    assert summary.historical_disposition["completion_state"] == "COMPLETE"
    assert summary.historical_disposition["economic_disposition"] == "RETIRE"


def test_completed_retired_audit_accepts_current_data_parity_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    manifest_hash, experiment_id, _retired_trial_id = _registered_retired_derived_trial(
        campaign,
        manifest,
    )
    _complete_retired_disposition(campaign, manifest, experiment_id=experiment_id)
    _store_content_addressed_protocol(tmp_path, _protocol())

    def reject_current_source(_payload: Mapping[str, object]) -> None:
        raise ValueError("data-parity attestation differs from recomputed source evidence")

    campaign._data_parity_attestation_verifier = reject_current_source

    summary = campaign.audit(manifest_hash=manifest_hash)

    assert summary.historical_disposition["completion_state"] == "COMPLETE"
    assert summary.historical_disposition["economic_disposition"] == "RETIRE"


def test_retired_audit_rejects_unit_evidence_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    attestation = manifest["data_parity_attestation"]
    attestation["evidence_sha256"] = "0" * 64
    reference = campaign._manifest_store.store(manifest)
    _store_content_addressed_protocol(tmp_path, _protocol())

    with pytest.raises(ValueError, match="unit evidence differs"):
        campaign.audit(manifest_hash=reference.sha256)


def test_incomplete_audit_still_rejects_current_data_parity_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _manifest_fixture = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)

    def reject_current_source(_payload: Mapping[str, object]) -> None:
        raise ValueError("data-parity attestation differs from recomputed source evidence")

    campaign._data_parity_attestation_verifier = reject_current_source

    with pytest.raises(ValueError, match="recomputed source evidence"):
        campaign.audit(manifest_hash=frozen.manifest_hash)


def test_normal_resume_rejects_current_data_parity_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _manifest_fixture = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)

    def reject_current_source(_payload: Mapping[str, object]) -> None:
        raise ValueError("data-parity attestation differs from recomputed source evidence")

    campaign._data_parity_attestation_verifier = reject_current_source

    with pytest.raises(ValueError, match="recomputed source evidence"):
        campaign.run(
            strategy_path=_REGISTERED_CAMPAIGN,
            manifest_hash=frozen.manifest_hash,
            freeze_only=True,
        )


def test_retired_campaign_audit_rejects_tampered_retired_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    manifest_hash, _experiment_id, retired_trial_id = _registered_retired_derived_trial(
        campaign,
        manifest,
    )
    _store_content_addressed_protocol(tmp_path, _protocol())
    with Session(campaign._engine) as session:
        trial = session.get(BacktestTrial, retired_trial_id)
        assert trial is not None
        assert trial.result_id is not None
        result = session.get(BacktestResult, trial.result_id)
        assert result is not None
        assert isinstance(result.meta, dict)
        changed = deepcopy(result.meta)
        changed["evidence_payload"]["historical_only"] = False
        result.meta = changed
        session.commit()

    with pytest.raises(RuntimeError, match="stored content does not match its hash"):
        campaign.audit(manifest_hash=manifest_hash)


def test_retired_campaign_audit_rejects_malformed_frozen_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    del manifest["trial_registry"][0]["runner_kind"]
    reference = campaign._manifest_store.store(manifest)
    _store_content_addressed_protocol(tmp_path, _protocol())

    with pytest.raises(ValueError, match="invalid fields: missing runner_kind"):
        campaign.audit(manifest_hash=reference.sha256)


def test_production_core_loader_is_shared_and_class_pinned() -> None:
    loaded = load_pure_strategy_core(
        _REGISTERED_CAMPAIGN,
        expected_class_name="RegisteredCampaignCore",
    )

    assert loaded.__name__ == "RegisteredCampaignCore"
    with pytest.raises(RuntimeError, match="Expected PureSignalStrategy"):
        load_pure_strategy_core(_REGISTERED_CAMPAIGN, expected_class_name="ResearchOnlyCopy")


def test_freeze_only_registers_complete_ledger_and_resumes_same_experiment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)

    first = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    second = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=first.manifest_hash,
        freeze_only=True,
    )

    assert first.experiment_id == second.experiment_id
    protocol = _protocol()
    dataset_count = len(manifest["data"]["datasets"])
    fold_count = len(protocol["folds"])
    window_count = fold_count + 1
    cost_count = len(protocol["cost_scenarios"])
    arm_count = len(protocol["signal_rule_arms"])
    benchmark_count = len(protocol["benchmarks"])
    executable_semantics = sum(
        arm["execution"] == "expected_cost_full_panel_and_direct_oos_diagnostic"
        for arm in protocol["semantic_diagnostic_arms"]
    )
    abandoned_semantics = len(protocol["semantic_diagnostic_arms"]) - executable_semantics
    power_count = len(protocol["power"]["minimum_standardized_effect_sensitivities"])
    sizing_count = len(protocol["conditional_sizing_trials"]["arms"])
    abandoned_count = abandoned_semantics * dataset_count
    total_trials = len(manifest["trial_registry"])
    assert [row["sequence"] for row in manifest["trial_registry"]] == list(range(total_trials))
    assert first.total_trials == total_trials
    assert Counter(first.status_counts) == Counter(
        {
            "abandoned": abandoned_count,
            "registered": total_trials - abandoned_count,
        }
    )
    assert first.historical_disposition == {
        "status": "registered",
        "trial_id": _trial_ids_by_sequence(campaign)[total_trials - 1],
        "completion_state": None,
        "economic_disposition": None,
        "frozen_arm_id": None,
        "blocker_codes": [],
        "reason_codes": [],
        "authority": {
            "paper_trading_authorized": False,
            "live_trading_authorized": False,
            "automatic_parameter_deployment_authorized": False,
        },
    }
    assert _family_counts(manifest) == Counter(
        {
            "baseline": dataset_count * cost_count * window_count,
            "one_factor": (arm_count - 1) * dataset_count * cost_count * window_count,
            "training_selected_pooled_oos": fold_count + 1,
            "executable_benchmark": benchmark_count
            * cost_count
            * ((dataset_count + 1) * window_count + 1),
            "semantic_diagnostic": executable_semantics * dataset_count * window_count
            + abandoned_count,
            "derived_diagnostic": 3,
            "prospective_power_design": power_count,
            "pipeline_reconciliation": dataset_count + 1,
            "conditional_sizing": sizing_count * (dataset_count * fold_count + 1),
            "historical_disposition": 1,
        }
    )
    assert _runner_counts(manifest) == Counter(
        {
            "production_core_direct": arm_count * dataset_count * cost_count * window_count,
            "anchored_joint_asset_selector": fold_count,
            "pooled_oos_primary_aggregate": 1,
            "executable_benchmark_component": (
                benchmark_count * cost_count * window_count * dataset_count
            ),
            "equal_weight_benchmark_portfolio": benchmark_count * cost_count * window_count,
            "pooled_oos_benchmark_aggregate": benchmark_count * cost_count,
            "production_core_semantic_diagnostic": (
                executable_semantics * dataset_count * window_count
            ),
            "abandoned_asset_mismatch": abandoned_count,
            "robustness_concentration_inference": 1,
            "registered_cscv_pbo": 1,
            "registered_redesign_candidate_inference": 1,
            "prospective_power_study": power_count,
            "scoring_binding_ledger_component": dataset_count,
            "scoring_binding_ledger_pooled": 1,
            "conditional_sizing_oos_component": sizing_count * dataset_count * fold_count,
            "conditional_sizing_pooled_oos": sizing_count,
            "historical_strategy_disposition": 1,
        }
    )
    direct_oos = [
        row
        for row in manifest["trial_registry"]
        if row["runner_kind"] == "production_core_direct" and row["fold_id"] != "full-panel"
    ]
    primary = [row for row in manifest["trial_registry"] if row["arm_id"] == "TRAINING_SELECTED"]
    assert {row["evidence_role"] for row in direct_oos} == {"retrospective_diagnostic"}
    assert len(primary) == 5
    assert {row["evidence_role"] for row in primary} == {
        "primary_joint_asset_purged_oos",
        "primary_pooled_purged_oos_aggregate",
    }
    assert all(row["dataset_id"].startswith("derived:equal-weight-portfolio:") for row in primary)
    assert all(len(row["component_datasets"]) == 2 for row in primary)
    assert all(
        f"{component['dataset_id']}@sha256={component['ohlcv_sha256']}" in primary[0]["dataset_id"]
        for component in primary[0]["component_datasets"]
    )
    expected_helpers = {
        "event_interval_builder": "build_report_event_intervals",
        "terminal_liquidation_estimator": "estimate_terminal_liquidation",
        "terminal_liquidation_policy_builder": ("build_terminal_liquidation_cost_policy"),
    }
    helper_runners = {
        "anchored_joint_asset_selector",
        "pooled_oos_primary_aggregate",
        "executable_benchmark_component",
        "equal_weight_benchmark_portfolio",
        "pooled_oos_benchmark_aggregate",
        "robustness_concentration_inference",
        "registered_cscv_pbo",
        "registered_redesign_candidate_inference",
        "conditional_sizing_oos_component",
        "conditional_sizing_pooled_oos",
    }
    assert all(
        row["parameters"]["execution_helpers"] == expected_helpers
        for row in manifest["trial_registry"]
        if row["runner_kind"] in helper_runners
    )
    assert manifest["historical_disposition_gates"] == protocol["historical_disposition_gates"]
    assert manifest["allowed_dispositions"] == protocol["allowed_dispositions"]
    assert manifest["promotion"] == protocol["promotion"]
    robustness_trial = next(
        row
        for row in manifest["trial_registry"]
        if row["runner_kind"] == "robustness_concentration_inference"
    )
    assert (
        robustness_trial["parameters"]["historical_disposition_gates"]
        == protocol["historical_disposition_gates"]
    )
    assert (
        robustness_trial["parameters"]["allowed_dispositions"] == protocol["allowed_dispositions"]
    )
    assert robustness_trial["parameters"]["promotion"] == protocol["promotion"]
    with Session(campaign._engine) as session:
        assert session.query(BacktestExperiment).count() == 1
        assert session.query(BacktestTrial).count() == total_trials
        assert all(row.manifest_hash == first.manifest_hash for row in session.query(BacktestTrial))


def test_downstream_registry_consumes_fixed_baseline_and_freezes_rounding() -> None:
    manifest = _manifest(_protocol())
    baseline_arm_id = manifest["historical_disposition_policy"]["baseline_arm_id"]
    downstream = {
        "robustness_concentration_inference",
        "prospective_power_study",
        "scoring_binding_ledger_component",
        "scoring_binding_ledger_pooled",
        "conditional_sizing_oos_component",
        "conditional_sizing_pooled_oos",
    }
    specs = [spec for spec in manifest["trial_registry"] if spec["runner_kind"] in downstream]

    assert specs
    for spec in specs:
        parameters = spec["parameters"]
        source_key = (
            "cadence_source_arm_id"
            if spec["runner_kind"] == "prospective_power_study"
            else "source_arm_id"
        )
        assert parameters[source_key] == baseline_arm_id
        if spec["runner_kind"].startswith("conditional_sizing_"):
            assert parameters["pre_broker_quantity_rounding"] == "price_tiered_decimals"
    robustness = next(
        spec for spec in specs if spec["runner_kind"] == "robustness_concentration_inference"
    )
    assert robustness["parameters"]["adaptive_selector_diagnostic_arm_id"] == ("TRAINING_SELECTED")
    cscv = next(
        spec for spec in manifest["trial_registry"] if spec["runner_kind"] == "registered_cscv_pbo"
    )
    full_panel = next(
        spec
        for spec in manifest["trial_registry"]
        if spec["runner_kind"] == "production_core_direct"
        and spec["arm_id"] == baseline_arm_id
        and spec["fold_id"] == "full-panel"
    )
    assert (
        cscv["evaluation_start"],
        cscv["evaluation_end_exclusive"],
    ) == (
        full_panel["evaluation_start"],
        full_panel["evaluation_end_exclusive"],
    )
    assert cscv["evaluation_start"] != robustness["evaluation_start"]


def test_new_manifest_requires_upstream_selection_ledger_before_data_access(
    tmp_path: Path,
) -> None:
    campaign = StrategyValidationCampaign(
        _engine(),
        repo_root=_REPO,
        artifact_root=tmp_path / ".artifacts",
        execution_cost_measurement_verifier=verify_coinbase_execution_cost_measurement,
        data_parity_attestation_verifier=lambda _payload: None,
    )
    protocol = _protocol()
    runtime_config = json.loads((_REGISTERED_CAMPAIGN / "config.json").read_text())

    with pytest.raises(ValueError, match="upstream_selection_ledger is required"):
        campaign._build_manifest(
            _REGISTERED_CAMPAIGN,
            protocol=protocol,
            runtime_config=runtime_config,
            execution_environment=None,
            upstream_selection_ledger=None,
            execution_cost_measurement=None,
        )


def test_new_manifest_requires_execution_cost_measurement_before_data_access(
    tmp_path: Path,
) -> None:
    campaign = StrategyValidationCampaign(
        _engine(),
        repo_root=_REPO,
        artifact_root=tmp_path / ".artifacts",
        execution_cost_measurement_verifier=verify_coinbase_execution_cost_measurement,
        data_parity_attestation_verifier=lambda _payload: None,
    )

    with pytest.raises(ValueError, match="execution_cost_measurement is required"):
        campaign._build_manifest(
            _REGISTERED_CAMPAIGN,
            protocol=_protocol(),
            runtime_config=json.loads((_REGISTERED_CAMPAIGN / "config.json").read_text()),
            execution_environment=None,
            upstream_selection_ledger=None,
            execution_cost_measurement=None,
        )


def test_new_manifest_requires_data_parity_attestation_before_data_access(
    tmp_path: Path,
) -> None:
    campaign = StrategyValidationCampaign(
        _engine(),
        repo_root=_REPO,
        artifact_root=tmp_path / ".artifacts",
        execution_cost_measurement_verifier=verify_coinbase_execution_cost_measurement,
        data_parity_attestation_verifier=lambda _payload: None,
    )

    with pytest.raises(ValueError, match="data_parity_attestation is required"):
        campaign._build_manifest(
            _REGISTERED_CAMPAIGN,
            protocol=_protocol(),
            runtime_config=json.loads((_REGISTERED_CAMPAIGN / "config.json").read_text()),
            execution_environment=None,
            upstream_selection_ledger=None,
            execution_cost_measurement=None,
            data_parity_attestation=None,
        )


def test_new_manifest_requires_correctness_attestation_before_data_access(
    tmp_path: Path,
) -> None:
    campaign = StrategyValidationCampaign(
        _engine(),
        repo_root=_REPO,
        artifact_root=tmp_path / ".artifacts",
        execution_cost_measurement_verifier=verify_coinbase_execution_cost_measurement,
        data_parity_attestation_verifier=lambda _payload: None,
    )

    with pytest.raises(ValueError, match="correctness_attestation is required"):
        campaign._build_manifest(
            _REGISTERED_CAMPAIGN,
            protocol=_protocol(),
            runtime_config=json.loads((_REGISTERED_CAMPAIGN / "config.json").read_text()),
            execution_environment=None,
            upstream_selection_ledger=tmp_path / "selection.csv",
            execution_cost_measurement=tmp_path / "costs.json",
            data_parity_attestation=tmp_path / "parity.json",
            correctness_attestation=None,
        )


def test_resume_rejects_external_upstream_selection_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _ = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)

    with pytest.raises(ValueError, match="resume uses the embedded attested ledger"):
        campaign.run(
            strategy_path=_REGISTERED_CAMPAIGN,
            manifest_hash=frozen.manifest_hash,
            upstream_selection_ledger=tmp_path / "replacement.csv",
            freeze_only=True,
        )


def test_resume_rejects_external_execution_cost_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _ = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)

    with pytest.raises(ValueError, match="resume uses the embedded attested artifact"):
        campaign.run(
            strategy_path=_REGISTERED_CAMPAIGN,
            manifest_hash=frozen.manifest_hash,
            execution_cost_measurement=tmp_path / "replacement.json",
            freeze_only=True,
        )


def test_resume_rejects_external_data_parity_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _ = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)

    with pytest.raises(ValueError, match="resume uses the embedded attested artifact"):
        campaign.run(
            strategy_path=_REGISTERED_CAMPAIGN,
            manifest_hash=frozen.manifest_hash,
            data_parity_attestation=tmp_path / "replacement.json",
            freeze_only=True,
        )


def test_resume_rejects_external_correctness_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _ = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)

    with pytest.raises(ValueError, match="resume uses the embedded attested artifact"):
        campaign.run(
            strategy_path=_REGISTERED_CAMPAIGN,
            manifest_hash=frozen.manifest_hash,
            correctness_attestation=tmp_path / "replacement.json",
            freeze_only=True,
        )


def test_correctness_attestation_is_reverified_on_freeze_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _ = _campaign(tmp_path, monkeypatch)
    calls: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    public_verifier = verify_registered_strategy_correctness_attestation

    def record_verification(
        payload: Mapping[str, object],
        contract: Mapping[str, object],
    ) -> Any:
        calls.append((payload, contract))
        return public_verifier(payload, contract)

    monkeypatch.setattr(
        campaign_protocol_module,
        "verify_registered_strategy_correctness_attestation",
        record_verification,
    )
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=frozen.manifest_hash,
        freeze_only=True,
    )

    assert len(calls) == 2
    assert all(payload["attestation_sha256"] for payload, _contract in calls)


def test_frozen_correctness_attestation_tampering_fails_closed() -> None:
    protocol = _protocol()
    manifest = _manifest(protocol)
    artifact = manifest["correctness_attestation"]
    assert isinstance(artifact, dict)
    artifact["attestation_sha256"] = "0" * 64
    validator = object.__new__(StrategyValidationCampaign)

    with pytest.raises(ValueError, match="content hash differs"):
        validator._validate_manifest_correctness_provenance(
            manifest,
            protocol=protocol,
        )


def test_unregistered_arm_is_refused_before_experiment_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _ = _campaign(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="Unregistered parameter arm"):
        campaign.run(strategy_path=_REGISTERED_CAMPAIGN, arm_ids=("POST_HOC_GRID",))
    with Session(campaign._engine) as session:
        assert session.query(BacktestExperiment).count() == 0
        assert session.query(BacktestTrial).count() == 0


def test_protocol_validation_requires_explicit_data_timeframe() -> None:
    protocol = _protocol()
    del protocol["data"]["timeframe"]
    validator = object.__new__(StrategyValidationCampaign)

    with pytest.raises(ValueError, match=r"data\.timeframe"):
        validator._validate_protocol(protocol)


def test_operational_state_contract_is_versioned_and_complete() -> None:
    validator = object.__new__(StrategyValidationCampaign)
    legacy_protocol = _protocol()
    validator._validate_operational_state_contract(legacy_protocol)

    protocol = _protocol()
    strategy = protocol["strategy"]
    strategy.update(
        {
            "operational_state_contract_version": 1,
            "paper_binding_expected_active": False,
            "paper_binding_expected_autopilot": False,
        }
    )
    with pytest.raises(TypeError, match="paper_strategy_config_expected_active"):
        validator._validate_operational_state_contract(protocol)

    strategy.update(
        {
            "paper_strategy_config_expected_active": False,
            "require_stop_loss": True,
        }
    )
    validator._validate_operational_state_contract(protocol)

    strategy["paper_binding_expected_active"] = "false"
    with pytest.raises(TypeError, match="paper_binding_expected_active"):
        validator._validate_operational_state_contract(protocol)


def test_resumed_manifest_rejects_operational_binding_or_guard_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _ = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    with Session(campaign._engine) as session:
        config = session.get(UserStrategyConfig, "validation-campaign-config")
        assert config is not None
        config.parameters = {
            "require_explicit_scoring_inputs": False,
            "require_stop_loss": True,
        }
        session.commit()

    with pytest.raises(
        ValueError,
        match=r"protocol expectation|changed after manifest freeze",
    ):
        campaign.run(
            strategy_path=_REGISTERED_CAMPAIGN,
            manifest_hash=frozen.manifest_hash,
            freeze_only=True,
        )


@pytest.mark.parametrize(
    ("protocol_field", "snapshot_field", "expected"),
    [
        ("paper_binding_expected_active", "active", False),
        ("paper_binding_expected_autopilot", "autopilot", False),
        (
            "paper_strategy_config_expected_active",
            "strategy_config_active",
            False,
        ),
        ("require_stop_loss", "require_stop_loss", True),
        (
            "require_explicit_scoring_inputs",
            "require_explicit_scoring_inputs",
            True,
        ),
    ],
)
def test_manifest_preflight_enforces_declared_operational_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol_field: str,
    snapshot_field: str,
    expected: bool,
) -> None:
    campaign, _ = _campaign(tmp_path, monkeypatch)
    strategy = {
        "strategy_id": "registered_campaign_v1",
        "operational_state_contract_version": 1,
        "paper_binding_expected_active": False,
        "paper_binding_expected_autopilot": False,
        "paper_strategy_config_expected_active": True,
        "require_stop_loss": True,
        "require_explicit_scoring_inputs": True,
        protocol_field: expected,
    }
    with Session(campaign._engine) as session:
        binding = session.get(UserStrategyBinding, 1)
        config = session.get(UserStrategyConfig, "validation-campaign-config")
        assert binding is not None
        assert config is not None
        if protocol_field == "paper_binding_expected_active":
            binding.is_active = not expected
        elif protocol_field == "paper_binding_expected_autopilot":
            binding.autopilot = not expected
        elif protocol_field == "paper_strategy_config_expected_active":
            config.is_active = not expected
        else:
            config.parameters = {
                **config.parameters,
                protocol_field: not expected,
            }
        session.commit()

    with pytest.raises(
        ValueError,
        match=f"field={snapshot_field} expected={expected}",
    ):
        campaign._operational_binding_snapshot(strategy)


def test_manifest_preflight_rejects_active_exact_config_without_a_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _ = _campaign(tmp_path, monkeypatch)
    strategy = {
        "strategy_id": "registered_campaign_v1",
        "operational_state_contract_version": 1,
        "paper_binding_expected_active": False,
        "paper_binding_expected_autopilot": False,
        "paper_strategy_config_expected_active": False,
        "require_stop_loss": True,
        "require_explicit_scoring_inputs": True,
    }
    with Session(campaign._engine) as session:
        config = session.get(UserStrategyConfig, "validation-campaign-config")
        assert config is not None
        config.is_active = False
        session.add(
            UserStrategyConfig(
                config_id="orphan-active-config",
                user_id="user-without-binding",
                strategy_id="registered_campaign_v1",
                execution_mode="paper",
                is_active=True,
                parameters={
                    "require_explicit_scoring_inputs": True,
                    "require_stop_loss": True,
                },
            )
        )
        session.commit()

    with pytest.raises(
        ValueError,
        match=r"exact user strategy config remains active.*expected=False",
    ):
        campaign._operational_binding_snapshot(strategy)


def test_registry_validation_rejects_omitted_extra_and_mutated_specs() -> None:
    protocol = _protocol()
    manifest = _manifest(protocol)
    validator = object.__new__(StrategyValidationCampaign)

    assert validator._validated_trial_specs(manifest, protocol) == manifest["trial_registry"]

    missing = deepcopy(manifest)
    missing["trial_registry"].pop()
    with pytest.raises(ValueError, match="family coverage"):
        validator._validated_trial_specs(missing, protocol)

    extra = deepcopy(manifest)
    extra["trial_registry"].append(deepcopy(extra["trial_registry"][-1]))
    with pytest.raises(ValueError, match="family coverage"):
        validator._validated_trial_specs(extra, protocol)

    mutated = deepcopy(manifest)
    mutation_index = next(
        index
        for index, row in enumerate(mutated["trial_registry"])
        if row["runner_kind"] != "production_core_direct"
    )
    mutated["trial_registry"][mutation_index]["runner_kind"] = "research_only_copy"
    with pytest.raises(ValueError, match="runner_kind"):
        validator._validated_trial_specs(mutated, protocol)


def test_extended_arm_and_fold_filters_are_prevalidated_from_protocol() -> None:
    protocol = _protocol()
    validator = object.__new__(StrategyValidationCampaign)

    validator._prevalidate_filters(
        protocol,
        arm_ids=(
            "BENCHMARK_CASH",
            "DIAGNOSTIC",
            "CSCV_PBO",
            "POWER_EFFECT_0p1",
            "SCORING_BINDING_LEDGER",
            "SIZE_INITIAL_STOP_RISK_1PCT",
        ),
        fold_ids=("pooled-oos", "asset-mismatch", "cscv", "prospective-design"),
    )


def test_unsupported_benchmark_or_semantic_name_is_rejected() -> None:
    validator = object.__new__(StrategyValidationCampaign)
    unsupported_benchmark = _protocol()
    unsupported_benchmark["benchmarks"].append("private_beta_clone")
    with pytest.raises(ValueError, match="executable benchmark implementations"):
        validator._benchmark_contracts(unsupported_benchmark)

    unsupported_semantic = _protocol()
    unsupported_semantic["semantic_diagnostic_arms"] = [
        {
            "id": "SEMANTIC_UNIT_CONTRACT",
            "correctness_finding_id": "missing.strategy.finding",
            "overrides": {},
            "execution": "expected_cost_full_panel_and_direct_oos_diagnostic",
        }
    ]
    with pytest.raises(ValueError, match="no exact strategy finding"):
        validator._semantic_contracts(unsupported_semantic)

    duplicate_finding = _protocol()
    finding = duplicate_finding["correctness_attestation"]["findings"][0]
    finding["finding_class"] = "strategy_semantic"
    duplicate_finding["semantic_diagnostic_arms"] = [
        {
            "id": "SEMANTIC_ONE",
            "correctness_finding_id": finding["id"],
            "overrides": {},
            "execution": "expected_cost_full_panel_and_direct_oos_diagnostic",
        },
        {
            "id": "SEMANTIC_TWO",
            "correctness_finding_id": finding["id"],
            "overrides": {"unit_contract": True},
            "execution": "expected_cost_full_panel_and_direct_oos_diagnostic",
        },
    ]
    with pytest.raises(
        ValueError,
        match="semantic arm, correctness-finding, and execution signatures must be unique",
    ):
        validator._semantic_contracts(duplicate_finding)


def test_regime_assignment_rejects_unimplemented_fields() -> None:
    validator = object.__new__(StrategyValidationCampaign)
    protocol = _protocol()
    robustness = protocol["robustness"]

    assert validator._validated_regime_assignment(robustness) == (robustness["regime_assignment"])
    robustness["regime_assignment"]["full_panel_volatility_thresholds"] = (
        "unimplemented_expanding_quantiles"
    )
    with pytest.raises(ValueError, match="implemented contract"):
        validator._validated_regime_assignment(robustness)


def test_missing_engine_capability_leaves_all_trials_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    monkeypatch.setattr(BacktestEngine, "validation_capabilities", {})

    with pytest.raises(RuntimeError, match="missing capabilities"):
        campaign.run(
            strategy_path=_REGISTERED_CAMPAIGN,
            arm_ids=(manifest["historical_disposition_policy"]["baseline_arm_id"],),
        )
    with Session(campaign._engine) as session:
        rows = session.query(BacktestTrial).all()
        assert len(rows) == len(manifest["trial_registry"])
        abandoned = _abandoned_count(manifest)
        assert Counter(row.status for row in rows) == Counter(
            {
                "abandoned": abandoned,
                "registered": len(rows) - abandoned,
            }
        )


def test_trial_exception_is_checkpointed_and_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    monkeypatch.setattr(campaign, "_require_engine_capabilities", lambda: None)

    def fail_trial(*_args: Any, **_kwargs: Any) -> None:
        message = "postgresql://user:secret@example.invalid/db cannot execute"
        raise RuntimeError(message)

    monkeypatch.setattr(campaign, "_execute_trial", fail_trial)
    first = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        arm_ids=(manifest["historical_disposition_policy"]["baseline_arm_id"],),
        fold_ids=("full-panel",),
    )
    second = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=first.manifest_hash,
        arm_ids=(manifest["historical_disposition_policy"]["baseline_arm_id"],),
        fold_ids=("full-panel",),
    )

    selected = [
        row
        for row in manifest["trial_registry"]
        if row["arm_id"] == manifest["historical_disposition_policy"]["baseline_arm_id"]
        and row["fold_id"] == "full-panel"
    ]
    assert first.failures_this_run == len(selected)
    assert second.failures_this_run == 0
    abandoned = _abandoned_count(manifest)
    assert Counter(first.status_counts) == Counter(
        {
            "abandoned": abandoned,
            "failed": len(selected),
            "registered": len(manifest["trial_registry"]) - len(selected) - abandoned,
        }
    )
    with Session(campaign._engine) as session:
        failed = session.query(BacktestTrial).filter_by(status="failed").all()
        assert {row.error_class for row in failed} == {"RuntimeError"}
        assert all("secret" not in str(row.error_context) for row in failed)
        assert all("<redacted>" in str(row.error_context) for row in failed)
