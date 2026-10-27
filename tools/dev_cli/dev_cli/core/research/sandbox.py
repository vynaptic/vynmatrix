"""Admission and wire validation for the declared research sandbox.

Editor output is untrusted. Only canonical current-bar signals cross back into
the trusted execution simulator; no report, command or path crosses this wire.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID

from lib_strategy.signals.normalization import normalize_signal_action
from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.signals.utils import compute_external_signal_id

MAX_MESSAGE_BYTES = 65_536
MAX_SIGNALS_PER_BAR = 8
MAX_DIAGNOSTIC_BYTES = 1_048_576
MAX_PROTOCOL_BYTES = 33_554_432
WALL_SECONDS = 300
SHA256_LENGTH = 64
IMAGE_PREFIX = "sha256:"
MAX_PIDS = 32
MAX_MEMORY_BYTES = 1_073_741_824
MAX_CPU_NANOS = 1_000_000_000
MAX_TMPFS_BYTES = 64 * 1024**2
MAX_HORIZON_LENGTH = 30
SANDBOX_PROGRAM = [
    "/opt/service/bin/python",
    "-I",
    "-B",
    "/app/tools/dev_cli/dev_cli/core/research/sandbox_entry.py",
]
LABEL_JOB = "com.vynmatrix.research.job"
LABEL_GENERATION = "com.vynmatrix.research.generation"
_ENVIRONMENT = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "PYTHON_VERSION",
        "PYTHON_PIP_VERSION",
        "PYTHON_SETUPTOOLS_VERSION",
        "LANG",
        "HOME",
        "GPG_KEY",
        "EXECUTION_MODE",
        "EXECUTION_ENGINE_ALLOW_LIVE",
        "RUN_MODE",
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "PYTHONHASHSEED",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
    }
)


def _refuse(message: str) -> NoReturn:
    """Normalize malformed protocol values into the runner's refusal contract."""
    raise ValueError(message)


@dataclass(frozen=True)
class SandboxSpec:
    job_id: str
    generation: str
    image_id: str
    bundle_hash: str

    def __post_init__(self) -> None:
        for value in (self.job_id, self.generation):
            if str(UUID(value)) != value:
                msg = "Sandbox identity must be a canonical UUID"
                raise ValueError(msg)
        if (
            not self.image_id.startswith(IMAGE_PREFIX)
            or len(self.image_id) != len(IMAGE_PREFIX) + SHA256_LENGTH
        ):
            msg = "Sandbox requires an exact image ID"
            raise ValueError(msg)
        for value in (self.image_id[len(IMAGE_PREFIX) :], self.bundle_hash):
            if len(value) != SHA256_LENGTH or any(char not in "0123456789abcdef" for char in value):
                msg = "Sandbox requires exact content identities"
                raise ValueError(msg)

    @property
    def container_name(self) -> str:
        return f"vynmatrix-research-{UUID(self.job_id).hex}"


def sandbox_command(root: Path, spec: SandboxSpec) -> list[str]:
    """Use only the declared service; code/config travel through bounded stdin."""
    return [
        "docker",
        "compose",
        "--env-file",
        str(root / ".env"),
        "-f",
        str(root / "docker/docker-compose.stack.yml"),
        "--profile",
        "research",
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--pull",
        "never",
        "--name",
        spec.container_name,
        "--label",
        f"{LABEL_JOB}={spec.job_id}",
        "--label",
        f"{LABEL_GENERATION}={spec.generation}",
        "research",
    ]


def verify_container(actual: Mapping[str, Any], spec: SandboxSpec) -> None:
    """Refuse to deliver source until Docker confirms the actual containment."""
    config = actual.get("Config", {})
    host = actual.get("HostConfig", {})
    labels = config.get("Labels") or {}
    if config.get("Entrypoint") or config.get("Cmd") != SANDBOX_PROGRAM:
        msg = "Sandbox program does not match the verified entrypoint"
        raise ValueError(msg)
    if (
        actual.get("Image") != spec.image_id
        or labels.get(LABEL_JOB) != spec.job_id
        or labels.get(LABEL_GENERATION) != spec.generation
    ):
        msg = "Sandbox container identity mismatch"
        raise ValueError(msg)
    if str(config.get("User") or "").split(":", 1)[0] in {"", "0", "root"}:
        msg = "Sandbox requires an unprivileged user"
        raise ValueError(msg)
    environment = dict(item.split("=", 1) for item in config.get("Env", []) if "=" in item)
    if (
        set(environment) - _ENVIRONMENT
        or environment.get("EXECUTION_ENGINE_ALLOW_LIVE") != "false"
        or environment.get("EXECUTION_MODE") != "paper"
    ):
        msg = "Sandbox environment is not the isolated paper allowlist"
        raise ValueError(msg)
    if any(
        environment.get(name) != "1"
        for name in (
            "OPENBLAS_NUM_THREADS",
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        )
    ):
        msg = "Sandbox numerical thread limits missing"
        raise ValueError(msg)
    security = host.get("SecurityOpt") or []
    no_new_privileges = any(
        item in {"no-new-privileges", "no-new-privileges:true"} for item in security
    )
    if not no_new_privileges or any(
        item not in {"no-new-privileges", "no-new-privileges:true"} for item in security
    ):
        msg = "Sandbox security profile weakened"
        raise ValueError(msg)
    if (
        host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("Privileged")
        or "ALL" not in (host.get("CapDrop") or [])
        or host.get("CapAdd")
        or host.get("Devices")
        or host.get("PidMode")
        or host.get("IpcMode") not in {None, "", "private"}
        or host.get("Binds")
        or actual.get("Mounts")
    ):
        msg = "Sandbox network/filesystem isolation weakened"
        raise ValueError(msg)
    if (
        not 0 < host.get("PidsLimit", 0) <= MAX_PIDS
        or not 0 < host.get("Memory", 0) <= MAX_MEMORY_BYTES
        or not 0 < host.get("NanoCpus", 0) <= MAX_CPU_NANOS
    ):
        msg = "Sandbox resource limit missing or exceeded"
        raise ValueError(msg)
    if set(host.get("Tmpfs") or {}) != {"/tmp"}:
        msg = "Sandbox requires its isolated temporary filesystem"
        raise ValueError(msg)
    sizes = [part[5:] for part in host["Tmpfs"]["/tmp"].split(",") if part.startswith("size=")]
    size = re.fullmatch(r"([0-9]+)([kmg]?)", sizes[0].lower()) if len(sizes) == 1 else None
    if (
        size is None
        or not 0
        < int(size[1]) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[size[2]]
        <= MAX_TMPFS_BYTES
    ):
        msg = "Sandbox temporary filesystem must be bounded to 64 MiB"
        raise ValueError(msg)


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        _refuse(f"Signal {field} must be finite numeric")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        msg = f"Signal {field} must be finite numeric"
        raise ValueError(msg) from exc
    if not math.isfinite(result) or (positive and result <= 0):
        msg = f"Signal {field} must be finite and within its bounds"
        raise ValueError(msg)
    return result


def validate_signal_message(
    payload: Any,
    *,
    sequence: int,
    symbol: str,
    timestamp: datetime,
    strategy_id: str,
    asset_class: str,
    strategy_version: str | None = None,
) -> list[Signal]:
    """Turn a single bounded response into authoritative canonical envelopes."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"sequence", "signals"}
        or type(payload["sequence"]) is not int
        or payload["sequence"] != sequence
    ):
        msg = "Sandbox response sequence/schema mismatch"
        raise ValueError(msg)
    signals = payload["signals"]
    if not isinstance(signals, list) or len(signals) > MAX_SIGNALS_PER_BAR:
        msg = "Sandbox signal count exceeded"
        raise ValueError(msg)
    allowed = {
        "action",
        "symbol",
        "timestamp",
        "confidence",
        "entry_price",
        "stop_loss",
        "take_profit",
        "expected_return",
        "predicted_risk",
        "horizon",
        "horizon_days",
    }
    required = {"action", "symbol", "timestamp", "confidence"}
    result = []
    for item in signals:
        if not isinstance(item, dict) or set(item) - allowed or required - set(item):
            msg = "Sandbox signal schema mismatch"
            raise ValueError(msg)
        if (
            item["symbol"] != symbol
            or not isinstance(item["action"], str)
            or item["action"] not in SignalAction.__members__
        ):
            msg = "Sandbox signal symbol/action mismatch"
            raise ValueError(msg)
        try:
            at = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            msg = "Sandbox signal timestamp invalid"
            raise ValueError(msg) from exc
        if at.tzinfo is None or at.astimezone(UTC) != timestamp.astimezone(UTC):
            msg = "Sandbox signal is outside the current callback"
            raise ValueError(msg)
        action = normalize_signal_action(item["action"])
        confidence = _number(item["confidence"], "confidence")
        if not 0 <= confidence <= 1:
            msg = "Signal confidence outside [0, 1]"
            raise ValueError(msg)
        kwargs: dict[str, Any] = {
            key: _number(
                item[key],
                key,
                positive=key in {"entry_price", "stop_loss", "take_profit", "horizon_days"},
            )
            for key in (
                "entry_price",
                "stop_loss",
                "take_profit",
                "expected_return",
                "predicted_risk",
                "horizon_days",
            )
            if item.get(key) is not None
        }
        horizon = item.get("horizon")
        if horizon is not None and (
            not isinstance(horizon, str) or len(horizon) > MAX_HORIZON_LENGTH
        ):
            msg = "Signal horizon invalid"
            raise ValueError(msg)
        signal = Signal(
            strategy_id=strategy_id,
            strategy_type="indicator",
            symbol=symbol,
            action=action,
            confidence=confidence,
            timestamp=timestamp,
            asset_class=asset_class,
            horizon=horizon,
            source="backtest",
            strategy_version=strategy_version,
            **kwargs,
        )
        external_id = compute_external_signal_id(
            strategy_id=strategy_id,
            symbol=symbol,
            bar_close_ts=timestamp,
            action=action,
            strategy_version=strategy_version,
        )
        result.append(replace(signal, external_signal_id=external_id))
    return result
