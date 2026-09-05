"""Maintenance uses exec slots and streams archives without credentials in arguments."""

import hashlib
import subprocess
import sys
from subprocess import CompletedProcess

import pytest

from dev_cli.core.database_backup import DatabaseBackup
from dev_cli.core.database_lifecycle import PlatformLifecycle


def lifecycle(tmp_path):
    return PlatformLifecycle(
        tmp_path,
        {
            "DB_PASSWORD": "unit_admin",
            "COMPOSE_PROFILES": "workers",
            "MIGRATION_DATABASE_URL": "postgresql://unit_migrator:unit_secret@postgres/unit_database",
        },
    )


def test_backup_streams_owner_only_file_in_existing_service(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write(b"PGDMPunit-test-archive")
        return CompletedProcess(command, 0)

    path = tmp_path / "archive.dump"
    DatabaseBackup(lifecycle(tmp_path), run=run).backup(path)
    command, kwargs = calls[0]
    assert "exec" in command
    assert "run" not in command
    assert "unit_secret" not in " ".join(command)
    assert kwargs["env"]["PGPASSWORD"] == "unit_secret"
    assert "--no-privileges" not in command
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        DatabaseBackup(lifecycle(tmp_path), run=run).backup(path)


def test_failed_backup_removes_only_its_partial_output(tmp_path):
    path = tmp_path / "failed.dump"

    def run(command, **kwargs):
        kwargs["stdout"].write(b"partial")
        return CompletedProcess(command, 1)

    with pytest.raises(RuntimeError, match="backup failed"):
        DatabaseBackup(lifecycle(tmp_path), run=run).backup(path)
    assert not path.exists()


def test_restore_validates_archive_before_stopping_and_keeps_atomic_restore(tmp_path, monkeypatch):
    platform = lifecycle(tmp_path)
    events = []
    monkeypatch.setattr(platform, "stop_runtime", lambda: events.append("stop"))

    def run(command, **kwargs):
        events.append(command)
        return CompletedProcess(command, 0)

    path = tmp_path / "archive.dump"
    path.write_bytes(b"bad")
    with pytest.raises(ValueError, match="PostgreSQL custom archive"):
        DatabaseBackup(platform, run=run).restore(path)
    assert events == []
    path.write_bytes(b"PGDMPunit-test-archive")
    DatabaseBackup(platform, run=run).restore(path)
    assert events[0] == "stop"
    assert "--single-transaction" in events[1]
    assert "--no-owner" in events[1]
    assert "--no-privileges" not in events[1]


@pytest.mark.parametrize("payload_size", [32, 16384])
def test_restore_passes_complete_archive_through_subprocess_stdin(
    tmp_path, monkeypatch, payload_size
):
    platform = lifecycle(tmp_path)
    monkeypatch.setattr(platform, "stop_runtime", lambda: None)
    archive = b"PGDMP" + bytes(index % 256 for index in range(payload_size))
    source = tmp_path / "archive.dump"
    source.write_bytes(archive)

    def run(command, **kwargs):
        assert "pg_restore" in command
        # The real child reads the inherited OS descriptor, not Python's buffer.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())",
            ],
            stdin=kwargs["stdin"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == hashlib.sha256(archive).hexdigest()
        return CompletedProcess(command, 0)

    DatabaseBackup(platform, run=run).restore(source)
