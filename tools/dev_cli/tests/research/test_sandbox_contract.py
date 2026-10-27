"""The trusted runner admits only its declared container and current-bar signals."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

JOB = "a85a9daa-990d-4d4c-91c7-b650b8580117"
GENERATION = "7df58e14-23fa-426d-b3b5-30f270d569d3"
IMAGE = "sha256:" + "a" * 64
AT = datetime(2026, 6, 10, 12, tzinfo=UTC)


def module():
    return importlib.import_module("dev_cli.core.research.sandbox")


def spec():
    return module().SandboxSpec(JOB, GENERATION, IMAGE, "b" * 64)


def inspection():
    return {
        "Image": IMAGE,
        "Config": {
            "User": "vmuser",
            "Env": [
                "EXECUTION_MODE=paper",
                "EXECUTION_ENGINE_ALLOW_LIVE=false",
                "OPENBLAS_NUM_THREADS=1",
                "OMP_NUM_THREADS=1",
                "MKL_NUM_THREADS=1",
                "NUMEXPR_NUM_THREADS=1",
            ],
            "Entrypoint": None,
            "Cmd": [
                "/opt/service/bin/python",
                "-I",
                "-B",
                "/app/tools/dev_cli/dev_cli/core/research/sandbox_entry.py",
            ],
            "Labels": {
                "com.vynmatrix.research.job": JOB,
                "com.vynmatrix.research.generation": GENERATION,
            },
        },
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": 32,
            "Memory": 1073741824,
            "NanoCpus": 1000000000,
            "Binds": None,
            "Tmpfs": {"/tmp": "size=64m,mode=1777"},
        },
        "Mounts": [],
    }


def test_declared_command_cannot_accept_mounts_or_caller_commands():
    command = module().sandbox_command(Path("/repo"), spec())
    assert command[:3] == ["docker", "compose", "--env-file"]
    assert command[-1] == "research"
    assert "--no-deps" in command
    assert "--pull" in command
    assert "never" in command
    assert "--volume" not in command
    assert "--privileged" not in command
    assert JOB in " ".join(command)


def test_container_admission_checks_actual_isolation():
    module().verify_container(inspection(), spec())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("NetworkMode", "bridge"),
        ("ReadonlyRootfs", False),
        ("Privileged", True),
        ("CapDrop", []),
        ("PidsLimit", -1),
        ("Memory", 0),
        ("NanoCpus", 0),
        ("SecurityOpt", ["seccomp=unconfined"]),
        ("Binds", ["/home:/host"]),
        ("SecurityOpt", ["no-new-privileges:true", 'seccomp={"defaultAction":"SCMP_ACT_ALLOW"}']),
        ("Tmpfs", {"/tmp": "mode=1777"}),
        ("Tmpfs", {"/tmp": "size=1g,mode=1777"}),
    ],
)
def test_weakened_isolation_is_refused(field, value):
    actual = inspection()
    actual["HostConfig"][field] = value
    with pytest.raises(ValueError, match="Sandbox"):
        module().verify_container(actual, spec())


def test_credentials_and_another_job_container_are_refused():
    actual = inspection()
    actual["Config"]["Env"].append("BACKEND_ADMIN_API_KEY=not-a-real-key")
    with pytest.raises(ValueError, match="environment"):
        module().verify_container(actual, spec())
    actual = inspection()
    actual["Config"]["Labels"]["com.vynmatrix.research.job"] = GENERATION
    with pytest.raises(ValueError, match="identity"):
        module().verify_container(actual, spec())


@pytest.mark.parametrize(
    ("field", "value"),
    [("Cmd", ["python", "-c", "print('fake result')"]), ("Entrypoint", ["/bin/sh", "-c"])],
)
def test_replaced_sandbox_program_is_refused(field, value):
    actual = inspection()
    actual["Config"][field] = value
    with pytest.raises(ValueError, match="program"):
        module().verify_container(actual, spec())


def test_unbounded_numerical_threads_are_refused():
    actual = inspection()
    actual["Config"]["Env"] = [
        item for item in actual["Config"]["Env"] if not item.startswith("OPENBLAS_NUM_THREADS=")
    ]
    with pytest.raises(ValueError, match="thread"):
        module().verify_container(actual, spec())


def message():
    return {
        "sequence": 7,
        "signals": [
            {
                "action": "LONG",
                "symbol": "BTCUSDC",
                "timestamp": AT.isoformat(),
                "confidence": 0.7,
                "entry_price": 100.0,
            }
        ],
    }


def test_current_bar_signal_becomes_canonical_deterministic_evidence():
    result = module().validate_signal_message(
        message(),
        sequence=7,
        symbol="BTCUSDC",
        timestamp=AT,
        strategy_id="draft_v1",
        asset_class="crypto",
    )
    assert len(result) == 1
    signal = result[0]
    assert signal.action.value == "LONG"
    assert signal.timestamp == AT
    assert signal.strategy_id == "draft_v1"
    assert signal.external_signal_id
    repeated = module().validate_signal_message(
        message(),
        sequence=7,
        symbol="BTCUSDC",
        timestamp=AT,
        strategy_id="draft_v1",
        asset_class="crypto",
    )
    assert signal.external_signal_id == repeated[0].external_signal_id


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "OTHER"},
        {"timestamp": "2026-06-11T12:00:00+00:00"},
        {"confidence": float("nan")},
        {"entry_price": float("inf")},
        {"action": "BUY"},
        {"strategy_id": "another"},
        pytest.param({"confidence": 10**400}, id="overflow-confidence"),
        pytest.param({"action": ["LONG"]}, id="action-array"),
    ],
)
def test_untrusted_signal_envelopes_fail_closed(changes):
    payload = message()
    payload["signals"][0].update(changes)
    with pytest.raises(ValueError, match=r"Signal|Sandbox signal"):
        module().validate_signal_message(
            payload,
            sequence=7,
            symbol="BTCUSDC",
            timestamp=AT,
            strategy_id="draft_v1",
            asset_class="crypto",
        )


def test_signal_flood_and_sequence_spoof_are_refused():
    payload = message()
    payload["signals"] *= 9
    with pytest.raises(ValueError, match="signal count exceeded"):
        module().validate_signal_message(
            payload,
            sequence=7,
            symbol="BTCUSDC",
            timestamp=AT,
            strategy_id="draft_v1",
            asset_class="crypto",
        )
    with pytest.raises(ValueError, match="response sequence/schema mismatch"):
        module().validate_signal_message(
            message(),
            sequence=8,
            symbol="BTCUSDC",
            timestamp=AT,
            strategy_id="draft_v1",
            asset_class="crypto",
        )
