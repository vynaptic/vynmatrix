"""Bounded, cancellable transport to the one declared research service."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from .sandbox import (
    LABEL_GENERATION,
    LABEL_JOB,
    MAX_DIAGNOSTIC_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_PROTOCOL_BYTES,
    WALL_SECONDS,
    SandboxSpec,
    sandbox_command,
    verify_container,
)

MAX_CONTAINERS = 3
MAX_ORPHANS = 100
_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
_COMPOSE_PROJECT = "vynmatrix"
_PROJECT_FILTER = f"label={_COMPOSE_PROJECT_LABEL}={_COMPOSE_PROJECT}"


def _refuse(message: str) -> NoReturn:
    raise ValueError(message)


def encode_message(value: dict[str, Any]) -> bytes:
    raw = (
        json.dumps(value, allow_nan=False, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()
    if len(raw) > MAX_MESSAGE_BYTES:
        msg = "Research protocol message exceeds 64 KiB"
        raise ValueError(msg)
    return raw


def _nonfinite(_value: str) -> None:
    msg = "Research protocol requires finite JSON"
    raise ValueError(msg)


def _verify_project(actual: dict[str, Any]) -> None:
    labels = actual.get("Config", {}).get("Labels") or {}
    if labels.get(_COMPOSE_PROJECT_LABEL) != _COMPOSE_PROJECT:
        msg = "Docker inventory returned a container outside the declared Compose project"
        raise ValueError(msg)


class DockerBackend:
    """Fixed Docker operations; no caller-provided commands or execution paths."""

    @staticmethod
    def _run(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["docker", *arguments], capture_output=True, timeout=10, check=False)

    def inspect(self, target: str) -> dict[str, Any] | None:
        response = self._run(["container", "inspect", target])
        if response.returncode:
            # A daemon/permission failure must not masquerade as an absent container.
            if b"No such container" in response.stderr or b"No such object" in response.stderr:
                return None
            msg = "Docker container inspection unavailable"
            raise ValueError(msg)
        value = json.loads(response.stdout)
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            msg = "Docker returned an invalid container inspection"
            raise ValueError(msg)
        return value[0]

    def running(self) -> list[dict[str, Any]]:
        response = self._run(["ps", "--quiet", "--no-trunc", "--filter", _PROJECT_FILTER])
        if response.returncode:
            msg = "Docker capacity inspection unavailable"
            raise ValueError(msg)
        ids = response.stdout.decode().split()
        if len(ids) > MAX_CONTAINERS:
            msg = "Research requires the declared three-container layout"
            raise ValueError(msg)
        rows = []
        for container_id in ids:
            actual = self.inspect(container_id)
            if actual is not None:
                _verify_project(actual)
                rows.append(actual)
        return rows

    def sandboxes(self) -> list[dict[str, Any]]:
        """Include this project's exited sandboxes, whose names would block retries."""
        response = self._run(
            [
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                _PROJECT_FILTER,
                "--filter",
                f"label={LABEL_JOB}",
            ]
        )
        if response.returncode:
            msg = "Docker research reconciliation inspection unavailable"
            raise ValueError(msg)
        ids = response.stdout.decode().split()
        if len(ids) > MAX_ORPHANS:
            msg = "Research orphan inventory exceeds its bounded reconciliation limit"
            raise ValueError(msg)
        result = []
        for container_id in ids:
            actual = self.inspect(container_id)
            if actual is not None:
                _verify_project(actual)
                if (actual.get("Config", {}).get("Labels") or {}).get(
                    "com.docker.compose.service"
                ) != "research":
                    msg = "Docker research inventory returned a container from another service"
                    raise ValueError(msg)
                result.append(actual)
        return result

    def start(self, command: list[str]) -> subprocess.Popen[bytes]:
        response = self._run(["info", "--format", "{{json .SecurityOptions}}"])
        if response.returncode:
            msg = "Docker builtin seccomp profile inspection unavailable"
            raise ValueError(msg)
        try:
            options = json.loads(response.stdout)
        except (ValueError, UnicodeError) as exc:
            msg = "Docker builtin seccomp profile inspection invalid"
            raise ValueError(msg) from exc
        if (
            not isinstance(options, list)
            or not all(isinstance(item, str) for item in options)
            or [item for item in options if item.startswith("name=seccomp")]
            != ["name=seccomp,profile=builtin"]
        ):
            msg = "Research requires Docker's builtin default seccomp profile"
            raise ValueError(msg)
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )

    def remove(self, container_id: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            msg = "Cleanup requires the exact verified container ID"
            raise ValueError(msg)
        response = self._run(["container", "rm", "--force", container_id])
        if response.returncode and b"No such container" not in response.stderr:
            msg = "Unable to remove the exact research sandbox"
            raise ValueError(msg)
        if self.inspect(container_id) is not None:
            msg = "Research sandbox removal could not be verified"
            raise ValueError(msg)


def exact_identity(actual: dict[str, Any], spec: SandboxSpec) -> str:
    labels = actual.get("Config", {}).get("Labels") or {}
    container_id = actual.get("Id", "")
    if (
        actual.get("Image") != spec.image_id
        or actual.get("Name") != "/" + spec.container_name
        or labels.get(LABEL_JOB) != spec.job_id
        or labels.get(LABEL_GENERATION) != spec.generation
        or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
    ):
        msg = "Refusing cleanup of a container with another identity"
        raise ValueError(msg)
    return str(container_id)


def _running_startup_state(actual: dict[str, Any]) -> bool:
    """Wait only for a verified created container; every other state fails closed."""
    state = actual.get("State")
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("Status"), str)
        or state["Status"] not in {"created", "running"}
        or any(
            type(state.get(flag)) is not bool
            for flag in ("Running", "Paused", "Restarting", "Dead")
        )
        or state["Running"] is not (state["Status"] == "running")
        or any(state[flag] for flag in ("Paused", "Restarting", "Dead"))
    ):
        _refuse("Research sandbox startup state is missing or unusable")
    return bool(state["Running"])


class SandboxProcess:
    """Own one verified container and kill its entire PID namespace on any exit.

    Pipe threads keep writes, reads and diagnostics bounded while the controlling
    thread continues checking the durable cancellation fence and wall deadline.
    """

    def __init__(
        self,
        root: Path,
        spec: SandboxSpec,
        *,
        docker: Any = None,
        poll: Callable[[], None] = lambda: None,
        admit: Callable[[str], None] = lambda _container_id: None,
        wall_seconds: float = WALL_SECONDS,
    ) -> None:
        if not 0 < wall_seconds <= WALL_SECONDS:
            msg = "Research wall limit must be in (0, 300] seconds"
            raise ValueError(msg)
        self.root, self.spec = root, spec
        self.docker = docker if docker is not None else DockerBackend()
        self.poll = poll
        self.admit = admit
        self.deadline = time.monotonic() + wall_seconds
        self.child: Any = None
        self.container_id: str | None = None
        self.output: queue.Queue[bytes | None] = queue.Queue(maxsize=8)
        self.writes: queue.Queue[tuple[bytes, threading.Event] | None] = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self.violation: Exception | None = None
        self.protocol_bytes = 0
        self.diagnostics = bytearray()
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.finished = False

    def _check(self) -> None:
        if time.monotonic() >= self.deadline:
            msg = "Research sandbox exceeded its wall timeout"
            raise TimeoutError(msg)
        if self.violation is not None:
            raise self.violation
        self.poll()

    def _account(self, size: int) -> None:
        with self.lock:
            self.protocol_bytes += size
            if self.protocol_bytes > MAX_PROTOCOL_BYTES:
                msg = "Research protocol exceeds 32 MiB"
                raise ValueError(msg)

    def _put_output(self, value: bytes | None) -> None:
        while not self.stopped.is_set():
            try:
                self.output.put(value, timeout=0.05)
            except queue.Full:
                continue
            else:
                return

    def _read_stdout(self) -> None:
        try:
            while not self.stopped.is_set():
                raw = self.child.stdout.readline(MAX_MESSAGE_BYTES + 1)
                if not raw:
                    self._put_output(None)
                    return
                if len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
                    _refuse("Research protocol message exceeds its bound")
                self._account(len(raw))
                self._put_output(raw)
        except (OSError, ValueError) as exc:
            self.violation = exc

    def _read_stderr(self) -> None:
        try:
            while not self.stopped.is_set():
                raw = self.child.stderr.read(8192)
                if not raw:
                    return
                if len(self.diagnostics) + len(raw) > MAX_DIAGNOSTIC_BYTES:
                    _refuse("Research diagnostics exceed 1 MiB")
                self.diagnostics.extend(raw)
        except (OSError, ValueError) as exc:
            self.violation = exc

    def _write_stdin(self) -> None:
        try:
            while not self.stopped.is_set():
                try:
                    task = self.writes.get(timeout=0.05)
                except queue.Empty:
                    continue
                if task is None:
                    return
                raw, completed = task
                view = memoryview(raw)
                while view:
                    written = self.child.stdin.write(view)
                    if not written:
                        _refuse("Research sandbox closed its input")
                    view = view[written:]
                self.child.stdin.flush()
                completed.set()
        except (OSError, ValueError) as exc:
            self.violation = exc

    def __enter__(self) -> SandboxProcess:
        self.child = self.docker.start(sandbox_command(self.root, self.spec))
        admitted = False
        try:
            while True:
                self._check()
                actual = self.docker.inspect(self.spec.container_name)
                if actual is not None:
                    # Capture its exact identity before checking all containment fields,
                    # so a weakened but correctly labelled sandbox is still removed.
                    container_id = exact_identity(actual, self.spec)
                    if self.container_id is not None and self.container_id != container_id:
                        _refuse("Research sandbox changed identity during startup")
                    self.container_id = container_id
                    verify_container(actual, self.spec)
                    if _running_startup_state(actual):
                        self._check()
                        self.admit(self.container_id)
                        break
                if self.child.poll() is not None:
                    _refuse("Declared research sandbox failed to start")
                self._check()
                time.sleep(0.05)
            for target in (self._read_stdout, self._read_stderr, self._write_stdin):
                thread = threading.Thread(target=target, daemon=True)
                self.threads.append(thread)
                thread.start()
            admitted = True
        finally:
            if not admitted:
                self.close()
        return self

    def send(self, value: dict[str, Any]) -> None:
        self._check()
        raw = encode_message(value)
        self._account(len(raw))
        completed = threading.Event()
        self.writes.put((raw, completed))
        while not completed.wait(0.05):
            self._check()

    def receive(self) -> dict[str, Any]:
        while True:
            self._check()
            try:
                raw = self.output.get(timeout=0.05)
            except queue.Empty:
                continue
            if raw is None:
                msg = "Research sandbox ended before its expected response"
                raise ValueError(msg)
            try:
                value = json.loads(raw, parse_constant=_nonfinite)
            except RecursionError as exc:
                msg = "Research protocol JSON nesting exceeds the bounded schema"
                raise ValueError(msg) from exc
            if not isinstance(value, dict):
                msg = "Research protocol requires an object message"
                raise TypeError(msg)
            return value

    def finish(self) -> None:
        """Require clean EOF and exit after the final expected result."""
        while True:
            self._check()
            try:
                extra = self.output.get(timeout=0.05)
            except queue.Empty:
                continue
            if extra is not None:
                msg = "Unexpected output after the final research result"
                raise ValueError(msg)
            break
        while True:
            self._check()
            try:
                returncode = self.child.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                continue
        for thread in self.threads[:2]:
            thread.join(timeout=0.1)
        self._check()
        if returncode:
            msg = "Research sandbox exited unsuccessfully; diagnostics retained locally"
            raise ValueError(msg)
        self.finished = True

    def close(self) -> None:
        self.stopped.set()
        try:
            actual = self.docker.inspect(self.container_id or self.spec.container_name)
            if actual is not None:
                container_id = exact_identity(actual, self.spec)
                if self.container_id is not None and self.container_id != container_id:
                    msg = "Research container changed identity before cleanup"
                    raise ValueError(msg)
                self.docker.remove(container_id)
        finally:
            if self.child is not None and self.child.pid > 0 and self.child.poll() is None:
                # This is the new session created for our Docker CLI, never a host PID search.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait(timeout=5)
            for thread in self.threads:
                thread.join(timeout=0.1)

    def __exit__(self, *_args: Any) -> None:
        self.close()
