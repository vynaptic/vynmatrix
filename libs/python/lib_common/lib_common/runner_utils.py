"""Shared helpers for strategy runner process managers."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def build_strategy_core_parameters(
    config: Mapping[str, Any],
    *,
    signal_source: str | None = None,
) -> dict[str, Any]:
    """Compose the one core-parameter contract shared by runtime and research."""

    raw_parameters = config.get("parameters")
    if raw_parameters is None:
        parameters: dict[str, Any] = {}
    elif isinstance(raw_parameters, Mapping):
        parameters = dict(raw_parameters)
    else:
        message = "strategy parameters must be an object"
        raise TypeError(message)

    strategy_version = config.get("strategy_version")
    if (
        not isinstance(strategy_version, str)
        or not strategy_version.strip()
        or strategy_version != strategy_version.strip()
    ):
        message = "strategy config requires a canonical top-level strategy_version"
        raise ValueError(message)
    parameters["strategy_version"] = strategy_version

    trade_direction = config.get("trade_direction_mode")
    if trade_direction is not None:
        if (
            not isinstance(trade_direction, str)
            or not trade_direction.strip()
            or trade_direction != trade_direction.strip()
        ):
            message = "trade_direction_mode must be a canonical non-blank string"
            raise ValueError(message)
        parameters["trade_direction_mode"] = trade_direction

    raw_feedback = config.get("feedback_loop")
    if raw_feedback is not None:
        if not isinstance(raw_feedback, Mapping):
            message = "feedback_loop must be an object"
            raise TypeError(message)
        evaluation_horizon = raw_feedback.get("evaluation_horizon")
        if evaluation_horizon is not None:
            if (
                not isinstance(evaluation_horizon, str)
                or not evaluation_horizon.strip()
                or evaluation_horizon != evaluation_horizon.strip()
            ):
                message = "evaluation_horizon must be a canonical non-blank string"
                raise ValueError(message)
            parameters["evaluation_horizon"] = evaluation_horizon

    if signal_source is not None:
        if not signal_source.strip() or signal_source != signal_source.strip():
            message = "signal_source must be a canonical non-blank string"
            raise ValueError(message)
        parameters["signal_source"] = signal_source
    return parameters


class ConfigValidationUnavailableError(RuntimeError):
    """Raised when strategy-config schema validation cannot be constructed.

    Fail closed: a runner that silently skips validation would happily start a
    strategy on a malformed config, so a missing jsonschema dependency, a
    missing schema file, or unparseable schema JSON must stop startup instead
    of downgrading to a warning.
    """


def build_config_validator(schema_path: Path, schema_label: str, logger: Any) -> Any:
    """Build the JSON schema validator; validation is mandatory (fail closed)."""
    try:
        from jsonschema import Draft7Validator  # noqa: PLC0415
    except ImportError as exc:
        msg = (
            f"jsonschema is required for {schema_label} config validation; "
            "install the indicator-runner runtime profile"
        )
        logger.exception(msg)
        raise ConfigValidationUnavailableError(msg) from exc

    if not schema_path.exists():
        msg = f"{schema_label} config schema not found: {schema_path}"
        logger.error(msg)
        raise ConfigValidationUnavailableError(msg)
    try:
        schema = json.loads(schema_path.read_text())
    except json.JSONDecodeError as exc:
        msg = f"Invalid {schema_label} config schema JSON: {schema_path}"
        logger.exception(msg)
        raise ConfigValidationUnavailableError(msg) from exc
    return Draft7Validator(schema)


def validate_strategy_config(
    config: dict[str, Any],
    strategy_name: str,
    validator: Any,
    logger: Any,
    max_errors: int = 5,
) -> bool:
    """Validate strategy config against the mandatory schema."""
    if not validator:
        msg = f"Strategy config validation for {strategy_name} requires a validator"
        raise ConfigValidationUnavailableError(msg)
    errors = sorted(validator.iter_errors(config), key=lambda e: e.path)
    if not errors:
        return True
    for err in errors[:max_errors]:
        path = ".".join(str(p) for p in err.path) or "<root>"
        logger.error("Config validation error for %s at %s: %s", strategy_name, path, err.message)
    if len(errors) > max_errors:
        remaining = len(errors) - max_errors
        logger.error("Config validation error for %s: %d more errors", strategy_name, remaining)
    return False


def resolve_run_mode(
    config: dict[str, Any],
    strategy_name: str,
    default_mode: str | None,
    logger: Any,
    allow_exec_mode_env: bool = False,
) -> str | None:
    """Resolve runtime mode for a strategy, preferring env override."""
    config_mode = (config.get("runtime") or {}).get("mode")
    env_mode = os.getenv("RUN_MODE")
    exec_mode = os.getenv("EXECUTION_MODE") if allow_exec_mode_env else None
    if exec_mode and exec_mode.lower() not in {"backtest", "paper", "live"}:
        exec_mode = None
    mode = (env_mode or exec_mode or config_mode or default_mode or "backtest").lower()
    if mode not in {"backtest", "paper", "live"}:
        logger.error(
            "Invalid RUN_MODE '%s' for %s. Must be backtest|paper|live",
            mode,
            strategy_name,
        )
        return None
    return mode


@dataclass(frozen=True)
class StrategyFilter:
    """Parsed strategy selection filters from environment."""

    target_strategy: str | None = None
    strategy_list: set[str] = field(default_factory=set)

    @property
    def mode(self) -> str:
        if self.target_strategy:
            return "single"
        if self.strategy_list:
            return "bundle"
        return "all"

    def matches(self, strategy_name: str) -> bool:
        if self.target_strategy:
            return strategy_name == self.target_strategy
        if self.strategy_list:
            return strategy_name in self.strategy_list
        return True


def iter_strategy_dirs(
    strategies_dir: Path,
    *,
    include_hidden: bool = False,
) -> Iterable[Path]:
    """Yield strategy directories, skipping hidden/metadata folders by default."""
    for strategy_dir in strategies_dir.iterdir():
        if not strategy_dir.is_dir():
            continue
        if not include_hidden and strategy_dir.name.startswith(("_", ".")):
            continue
        yield strategy_dir


def environment_permitted(config: dict[str, Any], current_environment: str | None) -> bool:
    """Whether a strategy config permits running in ``current_environment``.

    ``environments`` is a required, non-empty allowlist. Missing or malformed
    configuration and an unknown deployment environment fail closed. Matching is
    case-insensitive.
    """
    allowed = config.get("environments")
    if not isinstance(allowed, list) or not allowed or not current_environment:
        return False
    current = current_environment.strip().lower()
    if not current:
        return False
    return any(isinstance(env, str) and env.strip().lower() == current for env in allowed)


def load_strategy_config(  # noqa: PLR0911 - linear guard-clause validator (missing/disabled/env/invalid/ok)
    *,
    strategy_dir: Path,
    strategy_name: str,
    validator: Any | None,
    logger: Any,
    default_mode: str | None,
    allow_exec_mode_env: bool,
    max_validation_errors: int = 5,
    current_environment: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """
    Load and validate a strategy config.json.

    Returns:
        Tuple of (config, status):
        - (dict, "ok")
        - (None, "disabled")
        - (None, "env_excluded")  # config.environments excludes current_environment
        - (None, "missing")
        - (None, "invalid")
    """
    config_file = strategy_dir / "config.json"
    if not config_file.exists():
        logger.warning("No config.json found for %s, skipping", strategy_name)
        return None, "missing"

    try:
        with config_file.open() as f:
            config: dict[str, Any] = json.load(f)
    except json.JSONDecodeError:
        logger.exception("Invalid config.json for %s", strategy_name)
        return None, "missing"

    if not config.get("enabled", True):
        logger.info("Strategy %s is disabled, skipping", strategy_name)
        return None, "disabled"

    if not environment_permitted(config, current_environment):
        logger.info(
            "Strategy %s not permitted in environment '%s' (environments=%s), skipping",
            strategy_name,
            current_environment,
            config.get("environments"),
        )
        return None, "env_excluded"

    if not validate_strategy_config(
        config,
        strategy_name,
        validator,
        logger,
        max_validation_errors,
    ):
        return None, "invalid"

    run_mode = resolve_run_mode(
        config,
        strategy_name,
        default_mode=default_mode,
        logger=logger,
        allow_exec_mode_env=allow_exec_mode_env,
    )
    if run_mode is None:
        return None, "invalid"
    config.setdefault("runtime", {})["mode"] = run_mode

    return config, "ok"


@dataclass(frozen=True)
class RestartPolicy:
    """Restart policy for supervised processes."""

    max_restarts: int = 5
    cooldown_seconds: int = 60
    backoff_base_seconds: int = 60
    backoff_cap_seconds: int = 300

    def can_restart(
        self,
        restart_count: int,
        last_restart: datetime | None,
    ) -> bool:
        """Return True if restart is allowed under the policy."""
        if restart_count >= self.max_restarts:
            return False
        if last_restart:
            elapsed = (datetime.now(tz=UTC) - last_restart).total_seconds()
            if elapsed < self.cooldown_seconds:
                return False
        return True

    def backoff_seconds(self, restart_count: int) -> int:
        """Compute backoff seconds for a given restart attempt."""
        attempt = max(1, restart_count)
        return min(self.backoff_base_seconds * attempt, self.backoff_cap_seconds)
