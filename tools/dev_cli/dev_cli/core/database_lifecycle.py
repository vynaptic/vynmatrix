"""The supported Compose lifecycle, with one maintenance slot and no hidden services."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

#: Host directories Compose bind-mounts that a fresh clone does not contain.
#: Docker Engine on Linux creates a missing bind source as root, after which the
#: deploying user cannot write its own pre-upgrade snapshots there, so each one
#: is created as that user before any container exists.
CREATED_BIND_SOURCES = (Path(".artifacts"),)
#: Compose subcommands that create containers and therefore bind mounts.
_CREATES_CONTAINERS = frozenset({"create", "run", "up"})


class PlatformLifecycle:
    """Select only declared services; all commands run against one explicit project."""

    def __init__(
        self,
        root: Path,
        environment: Mapping[str, str],
        *,
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.root = root
        self.env = dict(environment)
        self.run = run
        self._research: Any = None
        if (
            self.env.get("EXECUTION_MODE", "paper") != "paper"
            or self.env.get("EXECUTION_ENGINE_ALLOW_LIVE", "false").lower() != "false"
        ):
            msg = "The supported lifecycle requires paper mode with live execution disabled"
            raise ValueError(msg)
        profiles = {
            part.strip() for part in self.env.get("COMPOSE_PROFILES", "").split(",") if part.strip()
        }
        if profiles - {"workers"}:
            msg = "COMPOSE_PROFILES may contain only workers; maintenance is selected explicitly"
            raise ValueError(msg)
        self.group = self.env.get("PLATFORM_APPLICATION_GROUP", "application")
        if self.group not in {"application", "all"} or (self.group == "all" and profiles):
            msg = "Use application with the workers profile, or all with no profiles"
            raise ValueError(msg)
        if self.group == "application" and profiles != {"workers"}:
            msg = "The application layout requires COMPOSE_PROFILES=workers"
            raise ValueError(msg)
        if not self.env.get("DB_PASSWORD") or self.env["DB_PASSWORD"] == "CHANGE_ME_BEFORE_USE":
            msg = "An explicit non-placeholder DB_PASSWORD is required"
            raise ValueError(msg)
        self.env["EXECUTION_MODE"] = "paper"
        self.env["EXECUTION_ENGINE_ALLOW_LIVE"] = "false"
        self.prefix = [
            "docker",
            "compose",
            "--env-file",
            str(root / ".env") if (root / ".env").is_file() else os.devnull,
            "-f",
            str(root / "docker/docker-compose.stack.yml"),
        ]

    def command(self, *args: str, input_text: str | None = None) -> str:
        """Capture subprocess errors so rendered environment values cannot leak secrets."""
        if args and args[0] in _CREATES_CONTAINERS:
            self.prepare_bind_sources()
        result = self.run(
            [*self.prefix, *args],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            input=input_text,
            check=False,
        )
        if result.returncode:
            msg = (
                f"Compose {args[0]} stage failed ({result.returncode}); "
                "runtime remains stopped after failed maintenance"
            )
            if "bootstrap" in args:
                markers = re.findall(
                    r"^\[bootstrap\] stage=(owner|catalogue|provision|migration|roles)$",
                    result.stdout or "",
                    re.MULTILINE,
                )
                stage = markers[-1] if markers else "startup"
                msg = (
                    f"Compose bootstrap failed during {stage} ({result.returncode}); "
                    "runtime remains stopped. Inspect migration state before retrying."
                )
            raise RuntimeError(msg)
        return str(result.stdout)

    def prepare_bind_sources(self) -> None:
        """Create each missing bind source as this user; Docker would create it as root."""
        for relative in CREATED_BIND_SOURCES:
            (self.root / relative).mkdir(exist_ok=True)

    def _new_research_maintenance(self) -> Any:
        from dev_cli.core.research.maintenance import (  # noqa: PLC0415
            MaintenanceStore,
            ResearchMaintenance,
        )

        return ResearchMaintenance(store=MaintenanceStore(self.env))

    def begin_research_maintenance(self) -> bool:
        """Return ownership so nested backup/restore cannot release an outer fence."""
        if self._research is not None:
            return False
        guard = self._new_research_maintenance()
        guard.begin()
        self._research = guard
        return True

    def refence_research_maintenance(self) -> None:
        if self._research is not None:
            self._research.after_restore()

    def complete_research_maintenance(self) -> None:
        guard, self._research = self._research, None
        if guard is not None:
            guard.complete()

    def abort_research_maintenance(self) -> None:
        guard, self._research = self._research, None
        if guard is not None:
            guard.abort()

    def finish_standalone_backup(self) -> None:
        guard = self._research
        if guard is not None and (
            guard.previous is None or (not guard.previous["maintenance"] and guard.stack_was_active)
        ):
            self.complete_research_maintenance()
        else:
            self.abort_research_maintenance()

    def stop_runtime(self) -> None:
        self.begin_research_maintenance()
        succeeded = False
        try:
            self.command("stop", "--timeout", "60", "workers", "application")
            running = set(self.command("ps", "--status", "running", "--services").split())
            if running - {"postgres"}:
                msg = (
                    "Unexpected running services remain; "
                    "stop or explicitly retire them before maintenance"
                )
                raise ValueError(msg)
            succeeded = True
        finally:
            if not succeeded:
                self.abort_research_maintenance()

    def start_runtime(self) -> None:
        services = ["application", "workers"] if self.group == "application" else ["application"]
        self.command("up", "-d", "--wait", *services)

    def bootstrap(self, owner_input: str, *, start_runtime: bool) -> None:
        succeeded = False
        try:
            self.command("up", "-d", "--wait", "postgres")
            self.stop_runtime()
            self.command("run", "--rm", "--no-deps", "-T", "bootstrap", input_text=owner_input)
            self.refence_research_maintenance()
            if start_runtime:
                self.start_runtime()
                self.complete_research_maintenance()
            else:
                self.abort_research_maintenance()
            succeeded = True
        finally:
            if not succeeded:
                self.abort_research_maintenance()
