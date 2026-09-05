"""Stream PostgreSQL maintenance through the existing database container."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from dev_cli.core.database_lifecycle import PlatformLifecycle


def _require_backup_success(returncode: int) -> None:
    if returncode:
        msg = "PostgreSQL backup failed; partial output was removed"
        raise RuntimeError(msg)


class DatabaseBackup:
    """No sidecar containers, shell credentials, in-memory dumps, or implicit overwrite."""

    def __init__(
        self, lifecycle: PlatformLifecycle, *, run: Callable[..., Any] = subprocess.run
    ) -> None:
        self.lifecycle = lifecycle
        self.run = run
        try:
            url = make_url(lifecycle.env.get("MIGRATION_DATABASE_URL", ""))
        except SQLAlchemyError as exc:
            msg = "MIGRATION_DATABASE_URL must name the explicit maintenance target"
            raise ValueError(msg) from exc
        if (
            url.get_backend_name() != "postgresql"
            or not all((url.host, url.username, url.password, url.database))
            or (url.username or "").startswith("vm_")
            or url.query
        ):
            msg = (
                "Maintenance requires an explicit PostgreSQL owner URL without connection overrides"
            )
            raise ValueError(msg)
        self.database = str(url.database)
        self.arguments = [
            "--host",
            str(url.host),
            "--port",
            str(url.port or 5432),
            "--username",
            str(url.username),
            "--dbname",
            self.database,
        ]
        self.env = {**lifecycle.env, "PGPASSWORD": str(url.password)}

    def _command(self, program: str, *args: str, interactive: bool = False) -> list[str]:
        return [
            *self.lifecycle.prefix,
            "exec",
            *([] if interactive else ["-T"]),
            "-e",
            "PGPASSWORD",
            "postgres",
            program,
            *self.arguments,
            *args,
        ]

    def backup(self, target: Path) -> None:
        """Create a new owner-only custom archive, including grants and durable history."""
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        completed = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                result = self.run(
                    self._command("pg_dump", "--format=custom"),
                    cwd=self.lifecycle.root,
                    env=self.env,
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            _require_backup_success(result.returncode)
            completed = True
        finally:
            if not completed:
                target.unlink(missing_ok=True)

    def restore(self, source: Path) -> None:
        """Replace only the explicit target in one transaction, keeping runtime stopped."""
        # pg_restore inherits the OS descriptor, so validation must not read ahead
        # into a Python buffer whose seek would leave that descriptor advanced.
        with source.open("rb", buffering=0) as stream:
            if stream.read(5) != b"PGDMP":
                msg = "Restore requires a PostgreSQL custom archive"
                raise ValueError(msg)
            stream.seek(0)
            self.lifecycle.stop_runtime()
            result = self.run(
                self._command(
                    "pg_restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                ),
                cwd=self.lifecycle.root,
                env=self.env,
                stdin=stream,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
        if result.returncode:
            msg = (
                "PostgreSQL restore failed and its transaction was rolled back; "
                "runtime remains stopped"
            )
            raise RuntimeError(msg)

    def connect(self) -> None:
        """Open an explicitly privileged maintenance console in an existing slot."""
        result = self.run(
            self._command("psql", "--set", "ON_ERROR_STOP=1", interactive=True),
            cwd=self.lifecycle.root,
            env=self.env,
            check=False,
        )
        if result.returncode:
            msg = "PostgreSQL maintenance console failed"
            raise RuntimeError(msg)
