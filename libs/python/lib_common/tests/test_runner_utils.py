"""Unit tests for runner_utils helpers."""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lib_common.runner_utils import (
    RestartPolicy,
    environment_permitted,
    load_strategy_config,
)


def test_restart_policy_respects_max_restarts() -> None:
    policy = RestartPolicy(max_restarts=2, cooldown_seconds=0)

    assert policy.can_restart(0, None) is True
    assert policy.can_restart(1, None) is True
    assert policy.can_restart(2, None) is False


def test_restart_policy_respects_cooldown() -> None:
    now = datetime.now(tz=UTC)
    policy = RestartPolicy(max_restarts=3, cooldown_seconds=60)

    assert policy.can_restart(1, now) is False
    assert policy.can_restart(1, now - timedelta(seconds=120)) is True


def test_restart_policy_backoff_caps() -> None:
    base = 10
    cap = 25
    policy = RestartPolicy(backoff_base_seconds=base, backoff_cap_seconds=cap)

    assert policy.backoff_seconds(1) == base
    assert policy.backoff_seconds(2) == base * 2
    assert policy.backoff_seconds(3) == cap  # capped
    assert policy.backoff_seconds(10) == cap  # capped


# --- environment gating (local-only e2e strategies inert in cloud) -----------
def test_environment_permitted_requires_nonempty_allowlist() -> None:
    assert environment_permitted({}, "production") is False
    assert environment_permitted({"environments": []}, "production") is False
    assert environment_permitted({"environments": "production"}, "production") is False


def test_environment_permitted_requires_known_environment() -> None:
    assert environment_permitted({"environments": ["dev"]}, None) is False
    assert environment_permitted({"environments": ["dev"]}, "") is False
    assert environment_permitted({"environments": ["dev"]}, "   ") is False


def test_environment_permitted_allowlist_is_case_insensitive() -> None:
    assert environment_permitted({"environments": ["dev"]}, "DEV") is True
    assert environment_permitted({"environments": ["Dev"]}, "dev") is True


def test_environment_permitted_excludes_unlisted_environment() -> None:
    assert environment_permitted({"environments": ["dev"]}, "production") is False
    assert environment_permitted({"environments": ["dev", "staging"]}, "production") is False


def _write_config(tmp_path: Path, payload: dict) -> Path:
    (tmp_path / "config.json").write_text(json.dumps(payload))
    return tmp_path


def _load(tmp_path: Path, env: str | None):  # type: ignore[no-untyped-def]
    # Validation is mandatory (fail closed); these tests target the
    # enable/environment guards, so use an accept-everything schema.
    from jsonschema import Draft7Validator

    return load_strategy_config(
        strategy_dir=tmp_path,
        strategy_name="ScalpTest",
        validator=Draft7Validator({}),
        logger=logging.getLogger("test"),
        default_mode="paper",
        allow_exec_mode_env=False,
        current_environment=env,
    )


def test_load_strategy_config_env_excluded(tmp_path: Path) -> None:
    _write_config(tmp_path, {"strategy_id": "scalp_test_v1", "environments": ["dev"]})
    # dev-only strategy in cloud -> excluded; in dev -> ok.
    assert _load(tmp_path, "production") == (None, "env_excluded")
    config, status = _load(tmp_path, "dev")
    assert status == "ok"
    assert config is not None


def test_load_strategy_config_no_environments_fails_closed(tmp_path: Path) -> None:
    _write_config(tmp_path, {"strategy_id": "fleet_strat_v1"})
    assert _load(tmp_path, "production") == (None, "env_excluded")
