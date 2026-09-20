"""Unit tests for what ``vmdev deploy`` decides, against fakes instead of Docker."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dev_cli.core.deployment import artefact, pipeline
from dev_cli.core.deployment.record import INSTALL, UPGRADE


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _archive(tmp_path: Path, content: bytes = b"PGDMP\x00rest") -> Path:
    target = tmp_path / "snapshot.dump"
    target.write_bytes(content)
    return target


# --------------------------------------------------------------------- tagging


@pytest.mark.parametrize(
    ("dirty", "expected"),
    [(False, "sha-0123456789ab"), (True, "sha-0123456789ab-dirty")],
)
def test_the_tag_names_the_commit_and_marks_an_uncommitted_tree(dirty: bool, expected: str) -> None:
    state = artefact.SourceState(commit="0123456789abcdef" * 2, dirty=dirty)

    assert state.tag == expected
    assert state.short == "0123456789ab"


def test_source_state_reads_the_checkout(tmp_path: Path) -> None:
    def run(args: list[str], **_: Any) -> _Result:
        if args[1] == "rev-parse":
            return _Result(stdout="abcdef0123456789\n")
        return _Result(stdout=" M docs/spec.md\n")

    state = artefact.source_state(tmp_path, run=run)

    assert state.commit == "abcdef0123456789"
    assert state.dirty is True


# ------------------------------------------------------------ rollback decision


@pytest.mark.parametrize("stage", ["preflight", "build", "snapshot", "record"])
def test_a_failure_before_the_runtime_stops_undoes_nothing(stage: str, tmp_path: Path) -> None:
    assert pipeline.rollback_decision(stage, mode=UPGRADE, snapshot=_archive(tmp_path)) == "none"


@pytest.mark.parametrize("stage", ["stop", "provision", "account", "start", "verify"])
def test_a_failure_after_the_runtime_stops_restores_the_snapshot(
    stage: str, tmp_path: Path
) -> None:
    assert pipeline.rollback_decision(stage, mode=UPGRADE, snapshot=_archive(tmp_path)) == "restore"


@pytest.mark.parametrize("stage", ["stop", "provision", "start", "verify"])
def test_a_fresh_install_has_nothing_to_roll_back_to(stage: str) -> None:
    assert pipeline.rollback_decision(stage, mode=INSTALL, snapshot=None) == "none"


def test_a_missing_snapshot_refuses_to_restore_rather_than_guessing() -> None:
    assert pipeline.rollback_decision("provision", mode=UPGRADE, snapshot=None) == "refuse"


def test_a_truncated_snapshot_is_not_a_snapshot(tmp_path: Path) -> None:
    corrupt = _archive(tmp_path, b"not-a-dump")

    assert pipeline.snapshot_is_restorable(corrupt) is False
    assert pipeline.rollback_decision("provision", mode=UPGRADE, snapshot=corrupt) == "refuse"


def test_an_unknown_stage_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="Unknown deployment stage"):
        pipeline.rollback_decision("teleport", mode=UPGRADE, snapshot=None)


# ------------------------------------------------------------------- retention


def test_snapshot_retention_keeps_the_newest_five(tmp_path: Path) -> None:
    for index in range(8):
        (tmp_path / f"snapshot-2026092{index}T000000Z-sha-a.dump").write_bytes(b"PGDMP")

    removed = pipeline.prune_snapshots(tmp_path)

    assert len(removed) == 3
    assert len(list(tmp_path.glob("*.dump"))) == pipeline.SNAPSHOT_RETENTION


def test_image_retention_keeps_the_current_and_previous_generations() -> None:
    listing = "\n".join(
        [
            "sha-000000000005\tsha256:5\t2026-09-20 05:00:00 +0000 UTC",
            "sha-000000000004\tsha256:4\t2026-09-20 04:00:00 +0000 UTC",
            "sha-000000000003\tsha256:3\t2026-09-20 03:00:00 +0000 UTC",
            "sha-000000000002\tsha256:2\t2026-09-20 02:00:00 +0000 UTC",
            "latest\tsha256:5\t2026-09-20 05:00:00 +0000 UTC",
        ]
    )
    removed_refs: list[str] = []

    def run(args: list[str], **_: Any) -> _Result:
        if args[:3] == ["docker", "image", "ls"]:
            return _Result(stdout=listing)
        removed_refs.append(args[-1])
        return _Result()

    removed = artefact.prune_generations(
        artefact.PLATFORM_REPOSITORY, keep={"sha-000000000002"}, run=run
    )

    # The newest three are retained, plus the pinned rollback target.
    assert removed == []
    assert removed_refs == []
    assert artefact.RETAINED_GENERATIONS == 3


def test_image_retention_removes_only_superseded_stamped_generations() -> None:
    listing = "\n".join(
        f"sha-00000000000{index}\tsha256:{index}\t2026-09-2{index} 00:00:00 +0000 UTC"
        for index in reversed(range(6))
    )
    removed_refs: list[str] = []

    def run(args: list[str], **_: Any) -> _Result:
        if args[:3] == ["docker", "image", "ls"]:
            return _Result(stdout=listing)
        removed_refs.append(args[-1])
        return _Result()

    removed = artefact.prune_generations(
        artefact.PLATFORM_REPOSITORY, keep={"sha-000000000000"}, run=run
    )

    assert removed == ["sha-000000000002", "sha-000000000001"]
    assert all(
        reference.startswith(f"{artefact.PLATFORM_REPOSITORY}:") for reference in removed_refs
    )


# ---------------------------------------------------------------- compose facts


def test_compose_output_is_read_as_an_array_or_as_json_lines() -> None:
    rows = [
        {"Service": "application", "State": "running", "Health": "healthy", "Image": "a:1"},
        {"Service": "workers", "State": "running", "Health": "starting", "Image": "a:1"},
        {"Service": "postgres", "State": "exited", "Health": "", "Image": "postgres:16-alpine"},
    ]

    as_array = artefact.compose_services(json.dumps(rows))
    as_lines = artefact.compose_services("\n".join(json.dumps(row) for row in rows))

    assert as_array == as_lines == rows
    assert artefact.running_images(rows) == {"application": "a:1", "workers": "a:1"}
    assert artefact.unhealthy_services(rows) == ["workers"]


def test_an_image_without_a_registry_digest_reports_its_local_identity() -> None:
    inspected = {
        "Id": "sha256:abc",
        "RepoDigests": [],
        "Config": {
            "Labels": {
                artefact.SOURCE_COMMIT_LABEL: "abcdef0123456789",
                artefact.SOURCE_DIRTY_LABEL: "false",
            }
        },
    }

    identity = artefact.image_identity(
        "vynmatrix/platform:sha-abcdef012345",
        run=lambda *_a, **_k: _Result(stdout=json.dumps(inspected)),
    )

    assert identity is not None
    assert identity["image_digest"] == "sha256:abc"
    assert identity["source_commit"] == "abcdef0123456789"


def test_a_missing_image_is_reported_as_absent_not_as_an_error() -> None:
    assert (
        artefact.image_identity(
            "vynmatrix/platform:nope",
            run=lambda *_a, **_k: _Result(returncode=1, stderr="No such image"),
        )
        is None
    )


def test_the_real_runner_is_the_default() -> None:
    assert artefact.inspect_image.__defaults__ is None
    assert artefact.source_state.__kwdefaults__ == {"run": subprocess.run}
