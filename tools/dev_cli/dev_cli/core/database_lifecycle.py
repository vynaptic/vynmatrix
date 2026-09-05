"""The supported Compose lifecycle, with one maintenance slot and no hidden services."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


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

    def stop_runtime(self) -> None:
        self.command("stop", "--timeout", "60", "workers", "application")
        running = set(self.command("ps", "--status", "running", "--services").split())
        if running - {"postgres"}:
            msg = (
                "Unexpected running services remain; "
                "stop or explicitly retire them before maintenance"
            )
            raise ValueError(msg)

    def start_runtime(self) -> None:
        services = ["application", "workers"] if self.group == "application" else ["application"]
        self.command("up", "-d", "--wait", *services)

    def bootstrap(self, owner_input: str, *, start_runtime: bool) -> None:
        self.stop_runtime()
        self.command("up", "-d", "--wait", "postgres")
        self.command("run", "--rm", "--no-deps", "-T", "bootstrap", input_text=owner_input)
        if start_runtime:
            self.start_runtime()
