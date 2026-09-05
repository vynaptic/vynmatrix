"""Tests for the packaged ``vmdev strategy validate`` surface."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from dev_cli.main import cli

strategy_command = importlib.import_module("dev_cli.commands.strategy")
correctness_registration = importlib.import_module("dev_cli.validation.correctness_registration")
cost_measurement = importlib.import_module("dev_cli.validation.providers.coinbase_execution_costs")
validation_evidence = importlib.import_module("dev_cli.validation.evidence")


def test_strategy_validate_is_registered() -> None:
    result = CliRunner().invoke(cli, ["strategy", "validate", "--help"])

    assert result.exit_code == 0
    assert "--freeze-only" in result.output
    assert "--audit-only" in result.output
    assert "--manifest-hash" in result.output
    assert "--execution-environment" in result.output
    assert "--upstream-selection-ledger" in result.output
    assert "--execution-cost-measurement" in result.output
    assert "--data-parity-attestation" in result.output
    assert "--correctness-attestation" in result.output
    assert "--arm" in result.output


def test_strategy_validate_audit_only_requires_manifest_and_cannot_execute() -> None:
    runner = CliRunner()
    missing_manifest = runner.invoke(
        cli,
        [
            "strategy",
            "validate",
            "RetiredStrategy",
            "--database-url",
            "postgresql://validation.invalid/isolated",
            "--audit-only",
        ],
    )
    executable_selection = runner.invoke(
        cli,
        [
            "strategy",
            "validate",
            "RetiredStrategy",
            "--database-url",
            "postgresql://validation.invalid/isolated",
            "--manifest-hash",
            "a" * 64,
            "--audit-only",
            "--arm",
            "B0",
        ],
    )

    assert missing_manifest.exit_code == 2
    assert "--audit-only requires --manifest-hash" in missing_manifest.output
    assert executable_selection.exit_code == 2
    assert "cannot select arms/folds" in executable_selection.output


def test_strategy_validate_rejects_external_correctness_on_resume(tmp_path: Path) -> None:
    replacement = tmp_path / "replacement.json"
    replacement.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "strategy",
            "validate",
            "RegisteredCampaign",
            "--database-url",
            "postgresql://validation.invalid/isolated",
            "--manifest-hash",
            "a" * 64,
            "--correctness-attestation",
            str(replacement),
            "--freeze-only",
        ],
    )

    assert result.exit_code == 2
    assert "cannot amend a frozen --manifest-hash" in result.output


def test_strategy_attest_is_registered() -> None:
    result = CliRunner().invoke(cli, ["strategy", "attest", "--help"])

    assert result.exit_code == 0
    assert "--container-image" in result.output
    assert "--output" in result.output


def test_strategy_correctness_attestation_is_registered() -> None:
    result = CliRunner().invoke(cli, ["strategy", "attest-correctness", "--help"])

    assert result.exit_code == 0
    assert "--file ID=PATH" in result.output
    assert "--output" in result.output


def test_strategy_correctness_cli_delegates_and_renders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    destination = repo_root / ".artifacts" / "registered.json"
    digest = "a" * 64
    payload = {"attestation_sha256": digest, "status": "verified"}
    observed: list[dict[str, object]] = []

    def create_attestation(**kwargs: object) -> tuple[dict[str, object], Path]:
        observed.append(kwargs)
        return payload, destination

    monkeypatch.setattr(strategy_command, "get_project_root", lambda: repo_root)
    monkeypatch.setattr(
        correctness_registration,
        "create_registered_correctness_attestation",
        create_attestation,
    )

    result = CliRunner().invoke(
        cli,
        [
            "strategy",
            "attest-correctness",
            "GenericMomentum",
            "--file",
            "strategy.current=core.py",
        ],
    )

    assert result.exit_code == 0, result.output
    assert observed == [
        {
            "repo_root": repo_root.resolve(),
            "strategy_name": "GenericMomentum",
            "file_values": ("strategy.current=core.py",),
            "output": None,
        }
    ]
    assert f"sha256={digest}" in result.output
    assert destination.name in result.output


def test_strategy_correctness_cli_reports_registration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_registration(**_kwargs: object) -> tuple[dict[str, object], Path]:
        raise ValueError("registered correctness contract differs")

    monkeypatch.setattr(
        correctness_registration,
        "create_registered_correctness_attestation",
        fail_registration,
    )

    result = CliRunner().invoke(
        cli,
        [
            "strategy",
            "attest-correctness",
            "GenericMomentum",
            "--file",
            "strategy.current=core.py",
        ],
    )

    assert result.exit_code == 1
    assert "registered correctness contract differs" in result.output


def test_strategy_cost_measurement_is_registered_and_requires_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    strategy_path = repo_root / "strategies" / "indicator" / "RegisteredCampaign"
    strategy_path.mkdir(parents=True)
    fixture_protocol = (
        Path(__file__).resolve().parents[3]
        / "tests/fixtures/strategy_validation/registered_campaign/validation_protocol.json"
    )
    (strategy_path / "validation_protocol.json").write_bytes(fixture_protocol.read_bytes())
    monkeypatch.setattr(strategy_command, "get_project_root", lambda: repo_root)

    runner = CliRunner()
    help_result = runner.invoke(cli, ["strategy", "measure-costs", "--help"])

    assert help_result.exit_code == 0
    assert "--expected-notional-usd" in help_result.output
    assert "--stressed-notional-usd" in help_result.output
    missing_credentials = runner.invoke(
        cli,
        ["strategy", "measure-costs", "RegisteredCampaign"],
        env={"COINBASE_API_KEY": "", "COINBASE_API_SECRET": ""},
    )
    assert missing_credentials.exit_code == 2
    assert "COINBASE_API_KEY and COINBASE_API_SECRET are required" in (missing_credentials.output)

    unregistered = runner.invoke(
        cli,
        [
            "strategy",
            "measure-costs",
            "RegisteredCampaign",
            "--expected-notional-usd",
            "9999",
        ],
        env={"COINBASE_API_KEY": "test-key", "COINBASE_API_SECRET": "test-secret"},
    )
    assert unregistered.exit_code == 1
    assert "must exactly match validation_protocol.json" in unregistered.output


def test_strategy_data_parity_measurement_is_registered() -> None:
    result = CliRunner().invoke(cli, ["strategy", "measure-data-parity", "--help"])

    assert result.exit_code == 0
    assert "--request-interval-seconds" in result.output
    assert "--output" in result.output


def test_strategy_cost_measurement_delegates_and_renders_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "costs.json"
    observed: dict[str, object] = {}
    monkeypatch.setattr(strategy_command, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(validation_evidence, "load_json_object", lambda _path: {})

    def create_measurement(
        _protocol: object,
        **kwargs: object,
    ) -> tuple[dict[str, object], Path]:
        observed.update(kwargs)
        return {"measurement_sha256": "a" * 64}, destination

    monkeypatch.setattr(
        cost_measurement,
        "create_registered_coinbase_execution_cost_measurement",
        create_measurement,
    )

    result = CliRunner().invoke(
        cli,
        [
            "strategy",
            "measure-costs",
            "RegisteredCampaign",
            "--samples",
            "2",
            "--sample-interval-seconds",
            "1",
            "--json-output",
        ],
        env={"COINBASE_API_KEY": "test-key", "COINBASE_API_SECRET": "test-secret"},
    )

    assert result.exit_code == 0, result.output
    assert observed["strategy_name"] == "RegisteredCampaign"
    assert observed["samples"] == 2
    assert observed["output"] is None
    assert '"measurement_sha256": "' in result.output


def test_strategy_attest_rejects_unverified_digest_text_as_an_image_reference() -> None:
    runner = CliRunner()
    fabricated = runner.invoke(
        cli,
        [
            "strategy",
            "attest",
            "RegisteredCampaign",
            "--container-image",
            f"indicator-runner=sha256:{'a' * 64}",
        ],
    )

    assert fabricated.exit_code == 1
    assert "intended local repository" in fabricated.output


def test_strategy_validate_requires_explicit_postgres() -> None:
    runner = CliRunner()

    missing = runner.invoke(cli, ["strategy", "validate", "RegisteredCampaign"])
    sqlite = runner.invoke(
        cli,
        [
            "strategy",
            "validate",
            "RegisteredCampaign",
            "--database-url",
            "sqlite:///:memory:",
        ],
    )

    assert missing.exit_code == 2
    assert "isolated validation database" in missing.output
    assert sqlite.exit_code == 2
    assert "requires an explicit PostgreSQL" in sqlite.output


def test_strategy_validate_resume_rejects_external_cost_measurement(tmp_path: Path) -> None:
    measurement = tmp_path / "cost.json"
    measurement.write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "strategy",
            "validate",
            "RegisteredCampaign",
            "--database-url",
            "postgresql://validation.invalid/db",
            "--manifest-hash",
            "a" * 64,
            "--execution-cost-measurement",
            str(measurement),
        ],
    )

    assert result.exit_code == 2
    assert "resume uses the embedded artifact" in result.output
