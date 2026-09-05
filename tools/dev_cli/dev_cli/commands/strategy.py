"""Controlled strategy-research commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
from rich.console import Console

from dev_cli.utils.helpers import get_project_root

console = Console()


@click.group()
def strategy() -> None:
    """Validate production strategy cores against frozen protocols."""


@strategy.command("attest-correctness")
@click.argument("strategy_name")
@click.option(
    "--file",
    "file_values",
    multiple=True,
    required=True,
    metavar="ID=PATH",
    help="Exact source, package, or fixture binding; repeat for every protocol ID.",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Content-addressed output below .artifacts (defaults to validation storage).",
)
@click.option("--json-output", is_flag=True, help="Emit only the attestation JSON.")
def attest_strategy_correctness(
    strategy_name: str,
    file_values: tuple[str, ...],
    output: Path | None,
    json_output: bool,
) -> None:
    """Build the protocol-registered pre-outcome correctness attestation."""

    from dev_cli.validation.correctness_registration import (  # noqa: PLC0415
        create_registered_correctness_attestation,
    )

    repo_root = get_project_root().resolve()
    try:
        payload, destination = create_registered_correctness_attestation(
            repo_root=repo_root,
            strategy_name=strategy_name,
            file_values=file_values,
            output=output,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    digest = str(payload["attestation_sha256"])
    encoded = json.dumps(payload, sort_keys=True)
    if json_output:
        click.echo(encoded)
    else:
        console.print("[bold green]Strategy correctness attested[/bold green]")
        console.print(f"sha256={digest}", soft_wrap=True)
        console.print(str(destination), soft_wrap=True)


@strategy.command("measure-data-parity")
@click.argument("strategy_name")
@click.option(
    "--request-interval-seconds",
    type=click.FloatRange(min=0.0, max=5.0),
    default=0.1,
    show_default=True,
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Output JSON below .artifacts (defaults to the strategy-validation path).",
)
@click.option("--json-output", is_flag=True, help="Emit only the attestation JSON.")
def measure_strategy_data_parity(
    strategy_name: str,
    request_interval_seconds: float,
    output: Path | None,
    json_output: bool,
) -> None:
    """Attest Coinbase public 1m-to-1d parity on registered real windows."""

    import httpx  # noqa: PLC0415

    from dev_cli.validation.evidence import load_json_object  # noqa: PLC0415
    from dev_cli.validation.providers.coinbase_data_parity import (  # noqa: PLC0415
        create_registered_coinbase_data_parity_attestation,
    )

    repo_root = get_project_root().resolve()
    strategy_path = repo_root / "strategies" / "indicator" / strategy_name
    protocol_path = strategy_path / "validation_protocol.json"
    try:
        protocol = load_json_object(protocol_path)
        payload, destination = create_registered_coinbase_data_parity_attestation(
            protocol,
            repo_root=repo_root,
            strategy_name=strategy_name,
            request_interval_seconds=request_interval_seconds,
            output=output,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        raise click.ClickException(str(exc)) from exc

    encoded = json.dumps(payload, sort_keys=True)
    if json_output:
        click.echo(encoded)
    else:
        console.print("[bold green]Strategy daily data parity attested[/bold green]")
        console.print(str(destination), soft_wrap=True)


@strategy.command("measure-costs")
@click.argument("strategy_name")
@click.option("--samples", type=click.IntRange(min=2), default=20, show_default=True)
@click.option(
    "--sample-interval-seconds",
    type=click.FloatRange(min=1.0, max=5.0),
    default=1.1,
    show_default=True,
)
@click.option("--depth-levels", type=click.IntRange(min=1, max=1_000), default=100)
@click.option("--expected-notional-usd", type=str, default="10000", show_default=True)
@click.option("--stressed-notional-usd", type=str, default="100000", show_default=True)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Output JSON below .artifacts (defaults to the strategy-validation path).",
)
@click.option("--json-output", is_flag=True, help="Emit only the measurement JSON.")
def measure_strategy_costs(
    strategy_name: str,
    samples: int,
    sample_interval_seconds: float,
    depth_levels: int,
    expected_notional_usd: str,
    stressed_notional_usd: str,
    output: Path | None,
    json_output: bool,
) -> None:
    """Measure and hash-attest Coinbase fee, spread, and depth costs."""

    from dev_cli.validation.evidence import load_json_object  # noqa: PLC0415
    from dev_cli.validation.providers.coinbase_execution_costs import (  # noqa: PLC0415
        create_registered_coinbase_execution_cost_measurement,
    )

    api_key = os.environ.get("COINBASE_API_KEY", "").strip()
    api_secret = os.environ.get("COINBASE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        message = (
            "COINBASE_API_KEY and COINBASE_API_SECRET are required for an uncached "
            "book and sanitized account fee snapshot"
        )
        raise click.UsageError(message)
    repo_root = get_project_root().resolve()
    strategy_path = repo_root / "strategies" / "indicator" / strategy_name
    protocol_path = strategy_path / "validation_protocol.json"
    try:
        protocol = load_json_object(protocol_path)
        payload, destination = create_registered_coinbase_execution_cost_measurement(
            protocol,
            repo_root=repo_root,
            strategy_name=strategy_name,
            api_key=api_key,
            api_secret=api_secret,
            samples=samples,
            sample_interval_seconds=sample_interval_seconds,
            depth_levels=depth_levels,
            expected_notional_usd=expected_notional_usd,
            stressed_notional_usd=stressed_notional_usd,
            output=output,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    encoded = json.dumps(payload, sort_keys=True)
    if json_output:
        click.echo(encoded)
    else:
        console.print("[bold green]Strategy execution costs attested[/bold green]")
        console.print(str(destination), soft_wrap=True)


@strategy.command("attest")
@click.argument("strategy_name")
@click.option(
    "--container-image",
    "container_image_values",
    multiple=True,
    required=True,
    metavar="NAME=IMAGE_REF",
    help="Locally built image to inspect and pin; repeat for additional images.",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Output JSON below .artifacts (defaults to the strategy-validation path).",
)
@click.option("--json-output", is_flag=True, help="Emit only the attestation JSON.")
def attest_strategy(
    strategy_name: str,
    container_image_values: tuple[str, ...],
    output: Path | None,
    json_output: bool,
) -> None:
    """Attest installed wheels and containers for STRATEGY_NAME."""

    from dev_cli.validation.execution_environment import (  # noqa: PLC0415
        attest_execution_environment,
    )

    try:
        payload, destination = attest_execution_environment(
            strategy_name,
            container_image_values,
            output,
        )
    except (FileNotFoundError, ImportError, TypeError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    encoded = json.dumps(payload, sort_keys=True)
    if json_output:
        click.echo(encoded)
    else:
        console.print("[bold green]Strategy execution environment attested[/bold green]")
        console.print(str(destination), soft_wrap=True)


@strategy.command("validate")
@click.argument("strategy_name")
@click.option(
    "--database-url",
    envvar="DATABASE_URL",
    help="Explicit isolated validation PostgreSQL URL (or DATABASE_URL).",
)
@click.option(
    "--artifact-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(".artifacts"),
    show_default=True,
    help="Ignored root for immutable content-addressed manifests.",
)
@click.option(
    "--manifest-hash",
    help="Resume exactly this frozen manifest instead of freezing current inputs.",
)
@click.option(
    "--execution-environment",
    type=click.Path(path_type=Path, dir_okay=False, exists=True, readable=True),
    help=(
        "JSON attestation for the exact installed wheels, active venv, and immutable "
        "container digest; used only while freezing a new executable manifest."
    ),
)
@click.option(
    "--upstream-selection-ledger",
    type=click.Path(path_type=Path, dir_okay=False, exists=True, readable=True),
    help=(
        "Authoritative upstream catalogue CSV to hash, validate, and embed while "
        "freezing a new manifest; resumed manifests use their embedded ledger."
    ),
)
@click.option(
    "--execution-cost-measurement",
    type=click.Path(path_type=Path, dir_okay=False, exists=True, readable=True),
    help=(
        "Hash-attested Coinbase fee/book JSON to validate and embed while freezing "
        "a new manifest; resumed manifests use only their embedded artifact."
    ),
)
@click.option(
    "--data-parity-attestation",
    type=click.Path(path_type=Path, dir_okay=False, exists=True, readable=True),
    help=(
        "Hash-attested Coinbase public 1m/1d parity JSON to validate and embed while "
        "freezing a new manifest; resumed manifests use only their embedded artifact."
    ),
)
@click.option(
    "--correctness-attestation",
    type=click.Path(path_type=Path, dir_okay=False, exists=True, readable=True),
    help=(
        "Protocol-pinned source/correctness JSON to validate and embed while freezing "
        "a new manifest; resumed manifests use only their embedded artifact."
    ),
)
@click.option(
    "--arm",
    "arm_ids",
    multiple=True,
    help="Execute only a registered arm; repeat to select more than one.",
)
@click.option(
    "--fold",
    "fold_ids",
    multiple=True,
    help="Execute only a registered fold; repeat to select more than one.",
)
@click.option(
    "--freeze-only",
    is_flag=True,
    help="Freeze inputs and register every trial without executing any trial.",
)
@click.option(
    "--audit-only",
    is_flag=True,
    help=(
        "Read-only audit of a frozen manifest and its database evidence; permits a "
        "retired strategy whose checked-in source no longer exists."
    ),
)
@click.option("--json-output", is_flag=True, help="Emit only the machine-readable summary.")
def validate_strategy(
    strategy_name: str,
    database_url: str | None,
    artifact_root: Path,
    manifest_hash: str | None,
    execution_environment: Path | None,
    upstream_selection_ledger: Path | None,
    execution_cost_measurement: Path | None,
    data_parity_attestation: Path | None,
    correctness_attestation: Path | None,
    arm_ids: tuple[str, ...],
    fold_ids: tuple[str, ...],
    freeze_only: bool,
    audit_only: bool,
    json_output: bool,
) -> None:
    """Freeze or resume a bounded historical campaign for STRATEGY_NAME."""

    resolved_database_url = (database_url or os.environ.get("DATABASE_URL", "")).strip()
    if not resolved_database_url:
        message = "--database-url or DATABASE_URL is required; use an isolated validation database"
        raise click.UsageError(message)
    if not resolved_database_url.startswith(("postgresql://", "postgresql+")):
        message = "strategy validation requires an explicit PostgreSQL database URL"
        raise click.UsageError(message)
    _validate_audit_only_options(
        audit_only=audit_only,
        manifest_hash=manifest_hash,
        arm_ids=arm_ids,
        fold_ids=fold_ids,
        freeze_only=freeze_only,
    )
    if manifest_hash is not None and execution_environment is not None:
        message = "--execution-environment cannot amend a frozen --manifest-hash"
        raise click.UsageError(message)
    if manifest_hash is not None and upstream_selection_ledger is not None:
        message = (
            "--upstream-selection-ledger cannot amend a frozen --manifest-hash; "
            "resume uses the embedded ledger"
        )
        raise click.UsageError(message)
    if manifest_hash is not None and execution_cost_measurement is not None:
        message = (
            "--execution-cost-measurement cannot amend a frozen --manifest-hash; "
            "resume uses the embedded artifact"
        )
        raise click.UsageError(message)
    if manifest_hash is not None and data_parity_attestation is not None:
        message = (
            "--data-parity-attestation cannot amend a frozen --manifest-hash; "
            "resume uses the embedded artifact"
        )
        raise click.UsageError(message)
    if manifest_hash is not None and correctness_attestation is not None:
        message = (
            "--correctness-attestation cannot amend a frozen --manifest-hash; "
            "resume uses the embedded artifact"
        )
        raise click.UsageError(message)

    repo_root = get_project_root().resolve()
    strategy_path = repo_root / "strategies" / "indicator" / strategy_name
    resolved_artifact_root = (
        artifact_root if artifact_root.is_absolute() else repo_root / artifact_root
    )
    try:
        from dev_cli.validation.evidence import load_json_object  # noqa: PLC0415
        from dev_cli.validation.providers.coinbase_protocol import (  # noqa: PLC0415
            CoinbaseValidationClient,
        )

        execution_attestation = (
            load_json_object(execution_environment) if execution_environment is not None else None
        )
        from dev_cli.validation.campaign import (  # noqa: PLC0415
            StrategyValidationCampaign,
        )
        from dev_cli.validation.providers.coinbase_data_parity import (  # noqa: PLC0415
            verify_coinbase_daily_parity_attestation,
        )
        from dev_cli.validation.providers.coinbase_execution_costs import (  # noqa: PLC0415
            verify_coinbase_execution_cost_measurement,
        )
        from lib_application.db.session import create_engine_for_env  # noqa: PLC0415

        engine = create_engine_for_env(db_url=resolved_database_url)
        metadata_client = None if audit_only else CoinbaseValidationClient()
        try:
            campaign = StrategyValidationCampaign(
                engine,
                repo_root=repo_root,
                artifact_root=resolved_artifact_root,
                product_metadata_provider=(
                    None
                    if metadata_client is None
                    else lambda product: metadata_client.fetch_product_metadata(
                        product
                    ).to_manifest_dict()
                ),
                execution_cost_measurement_verifier=(verify_coinbase_execution_cost_measurement),
                data_parity_attestation_verifier=(verify_coinbase_daily_parity_attestation),
            )
            if audit_only:
                assert manifest_hash is not None
                summary = campaign.audit(
                    manifest_hash=manifest_hash,
                    expected_strategy_name=strategy_name,
                )
            else:
                summary = campaign.run(
                    strategy_path=strategy_path,
                    manifest_hash=manifest_hash,
                    arm_ids=arm_ids,
                    fold_ids=fold_ids,
                    freeze_only=freeze_only,
                    execution_environment=execution_attestation,
                    upstream_selection_ledger=upstream_selection_ledger,
                    execution_cost_measurement=execution_cost_measurement,
                    data_parity_attestation=data_parity_attestation,
                    correctness_attestation=correctness_attestation,
                )
        finally:
            if metadata_client is not None:
                metadata_client.close()
            engine.dispose()
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    payload = json.dumps(summary.to_dict(), indent=None if json_output else 2, sort_keys=True)
    if json_output:
        click.echo(payload)
    else:
        console.print("[bold green]Strategy validation checkpoint complete[/bold green]")
        console.print(payload)
    if summary.failures_this_run:
        raise click.exceptions.Exit(1)


def _validate_audit_only_options(
    *,
    audit_only: bool,
    manifest_hash: str | None,
    arm_ids: tuple[str, ...],
    fold_ids: tuple[str, ...],
    freeze_only: bool,
) -> None:
    if not audit_only:
        return
    if manifest_hash is None:
        message = "--audit-only requires --manifest-hash"
        raise click.UsageError(message)
    if arm_ids or fold_ids or freeze_only:
        message = "--audit-only cannot select arms/folds, freeze, register, or execute trials"
        raise click.UsageError(message)


__all__ = [
    "attest_strategy",
    "attest_strategy_correctness",
    "strategy",
    "validate_strategy",
]
