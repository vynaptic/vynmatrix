"""Run existing paper applications in bounded, supervised process groups.

Use ``python -m scripts.run_platform application|workers|all``. Explicit jobs
run through ``... job backfill|quality-compounder --timeout-seconds N`` inside
the existing worker container. Job locks are local to that single worker group;
this launcher does not authorize replicas, execute migrations, or create containers.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if (_SOURCE_ROOT / "libs/python/lib_common").is_dir():
    sys.path.insert(0, str(_SOURCE_ROOT / "libs/python/lib_common"))

from lib_common.logging import get_logger, setup_logging  # noqa: E402
from lib_common.runner_utils import RestartPolicy  # noqa: E402
from scripts.platform_processes import ProcessSpec, build_job, build_processes  # noqa: E402

logger = get_logger(__name__)
_MAX_PROBE_BYTES = 65536
_MAX_PORT = 65535
_MAX_JOB_SECONDS = 86400


@dataclass
class _Child:
    spec: ProcessSpec
    process: subprocess.Popen[bytes] | None = None
    group_id: int | None = None
    restarts: int = 0
    next_restart: float = 0.0
    last_exit_code: int | None = None
    exhausted: bool = False
    metrics_directory: TemporaryDirectory[str] | None = None


def _signal_group(group_id: int | None, signum: int) -> None:
    if group_id is not None:
        with suppress(ProcessLookupError):
            os.killpg(group_id, signum)


def _probe(port: int, path: str) -> tuple[bool, dict[str, Any]]:
    # Ignore proxy settings even in the supervisor: these are local listeners.
    request = Request(f"http://127.0.0.1:{port}{path}")
    try:
        response = build_opener(ProxyHandler({})).open(request, timeout=0.3)
    except HTTPError as error:
        response = error
    except (URLError, OSError, TimeoutError):
        return False, {"reason": "listener_unavailable"}
    with response:
        payload = response.read(_MAX_PROBE_BYTES + 1)
        if len(payload) > _MAX_PROBE_BYTES:
            return False, {"reason": "probe_response_too_large"}
        try:
            detail = json.loads(payload)
        except (ValueError, UnicodeError):
            return False, {"reason": "invalid_probe_response"}
        if not isinstance(detail, dict):
            return False, {"reason": "invalid_probe_response"}
        return response.status == HTTPStatus.OK, detail


class PlatformSupervisor:
    """Own only the lifecycle of the configured application process trees."""

    def __init__(
        self,
        specs: list[ProcessSpec],
        *,
        restart_policy: RestartPolicy | None = None,
        trading_configured: bool = True,
    ) -> None:
        if not specs or len({spec.name for spec in specs}) != len(specs):
            msg = "A process group requires unique, nonempty component selections"
            raise ValueError(msg)
        ports = [spec.port for spec in specs if spec.port]
        if len(set(ports)) != len(ports):
            msg = "Components cannot share a listener port"
            raise ValueError(msg)
        self._children = [_Child(spec) for spec in specs]
        self._policy = restart_policy or RestartPolicy(
            max_restarts=3, cooldown_seconds=1, backoff_base_seconds=2, backoff_cap_seconds=30
        )
        self._stopping = False
        self._configured = trading_configured

    @property
    def failed(self) -> bool:
        return any(child.exhausted for child in self._children)

    def _launch(self, child: _Child) -> None:
        metrics_dir = child.spec.environment.get("PROMETHEUS_MULTIPROC_DIR")
        environment = dict(child.spec.environment)
        if metrics_dir:
            Path(metrics_dir).mkdir(parents=True, exist_ok=True)
            child.metrics_directory = TemporaryDirectory(prefix="generation-", dir=metrics_dir)
            environment["PROMETHEUS_MULTIPROC_DIR"] = child.metrics_directory.name
        try:
            child.process = subprocess.Popen(
                child.spec.command,
                env=environment,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            child.last_exit_code = 127
            logger.exception(
                "Component launch failed",
                component=child.spec.name,
                error_type=type(error).__name__,
            )
            self._schedule_restart(child)
            return
        child.group_id = child.process.pid
        logger.info(
            "Component started",
            component=child.spec.name,
            pid=child.process.pid,
            restarts=child.restarts,
        )

    def _schedule_restart(self, child: _Child) -> None:
        if child.metrics_directory is not None:
            child.metrics_directory.cleanup()
            child.metrics_directory = None
        if not self._policy.can_restart(child.restarts, None):
            child.exhausted = True
            logger.error(
                "Component restart budget exhausted",
                component=child.spec.name,
                exit_code=child.last_exit_code,
            )
            return
        delay = max(self._policy.cooldown_seconds, self._policy.backoff_seconds(child.restarts + 1))
        child.next_restart = time.monotonic() + delay

    def start(self) -> None:
        for child in self._children:
            self._launch(child)

    def tick(self) -> None:
        """Reap exits and schedule retries without blocking other children."""
        if self._stopping:
            return
        for child in self._children:
            if child.exhausted:
                continue
            if child.process is not None:
                exit_code = child.process.poll()
                if exit_code is None:
                    continue
                child.last_exit_code = child.process.wait()
                _signal_group(child.group_id, signal.SIGKILL)
                child.process = None
                child.group_id = None
                logger.warning("Component exited", component=child.spec.name, exit_code=exit_code)
                self._schedule_restart(child)
            elif time.monotonic() >= child.next_restart:
                child.restarts += 1
                self._launch(child)

    def snapshot(self, *, probe: bool = True) -> dict[str, Any]:
        """Separate lifecycle liveness from execution/feed progress readiness."""
        components: dict[str, dict[str, Any]] = {}
        for child in self._children:
            alive = child.process is not None and child.process.poll() is None
            ready = False
            detail: dict[str, Any] = {}
            if probe and alive and child.spec.port:
                alive, detail = _probe(child.spec.port, "/live")
                if alive:
                    ready, detail = _probe(child.spec.port, child.spec.readiness_path)
            components[child.spec.name] = {
                "alive": alive,
                "ready": ready,
                "restarts": child.restarts,
                "last_exit_code": child.last_exit_code,
                "exhausted": child.exhausted,
                "progress": detail,
                "metrics_path": f"/metrics/{child.spec.name}" if child.spec.port else None,
            }
        alive = not self._stopping and all(item["alive"] for item in components.values())
        ready = alive and self._configured and all(item["ready"] for item in components.values())
        return {
            "alive": alive,
            "ready": ready,
            "trading_configured": self._configured,
            "status": "unhealthy"
            if not alive
            else "unconfigured"
            if not self._configured
            else "ready"
            if ready
            else "alive",
            "components": components,
        }

    def metrics(self) -> bytes:
        snapshot = self.snapshot(probe=False)
        lines = [
            "# TYPE vynmatrix_platform_component_alive gauge",
            "# TYPE vynmatrix_platform_component_restarts counter",
        ]
        for name, detail in snapshot["components"].items():
            label = json.dumps(name)
            lines.extend(
                (
                    f"vynmatrix_platform_component_alive{{component={label}}} "
                    f"{int(detail['alive'])}",
                    f"vynmatrix_platform_component_restarts{{component={label}}} "
                    f"{detail['restarts']}",
                )
            )
        return ("\n".join(lines) + "\n").encode()

    def component_metrics(self, name: str) -> tuple[int, bytes]:
        child = next(
            (child for child in self._children if child.spec.name == name and child.spec.port), None
        )
        if child is None:
            return 404, b"Unknown component\n"
        try:
            with build_opener(ProxyHandler({})).open(
                f"http://127.0.0.1:{child.spec.port}/metrics", timeout=1
            ) as response:
                payload = response.read(1024 * 1024 + 1)
                if len(payload) > 1024 * 1024:
                    return 502, b"Metrics response exceeds bound\n"
                return 200, payload
        except (URLError, OSError, TimeoutError):
            return 503, b"Component metrics unavailable\n"

    def stop(self, *, timeout: float = 55.0) -> None:
        """Stop producers before APIs/execution under one total grace deadline."""
        self._stopping = True
        deadline = time.monotonic() + min(max(timeout, 0), 55)
        orders = sorted({child.spec.stop_order for child in self._children})
        for index, order in enumerate(orders):
            stage = [child for child in self._children if child.spec.stop_order == order]
            stage_deadline = time.monotonic() + max(0, deadline - time.monotonic()) / (
                len(orders) - index
            )
            for child in stage:
                _signal_group(child.group_id, signal.SIGTERM)
            while time.monotonic() < stage_deadline:
                if all(
                    child.process is None or child.process.poll() is not None for child in stage
                ):
                    break
                time.sleep(min(0.02, max(0, stage_deadline - time.monotonic())))
            for child in stage:
                # A leader can exit before an ignoring grandchild. Always clean
                # its process group, including after the leader has been reaped.
                _signal_group(child.group_id, signal.SIGKILL)
                if child.process is not None:
                    try:
                        child.last_exit_code = child.process.wait(
                            timeout=max(0.01, deadline - time.monotonic())
                        )
                    except subprocess.TimeoutExpired:
                        logger.exception(
                            "Component did not reap before shutdown deadline",
                            component=child.spec.name,
                        )
                child.group_id = None
                if child.metrics_directory is not None:
                    child.metrics_directory.cleanup()
                    child.metrics_directory = None


@contextmanager
def _shutdown_signals(event: threading.Event) -> Iterator[None]:
    previous = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, request_stop)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def create_health_server(
    supervisor: PlatformSupervisor, *, host: str = "0.0.0.0", port: int = 8090
) -> ThreadingHTTPServer:
    """Expose private group liveness, progress and separately namespaced metrics."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            content_type = "application/json"
            if self.path == "/metrics":
                status, payload = 200, supervisor.metrics()
                content_type = "text/plain; version=0.0.4; charset=utf-8"
            elif self.path.startswith("/metrics/"):
                status, payload = supervisor.component_metrics(self.path.removeprefix("/metrics/"))
                content_type = "text/plain; version=0.0.4; charset=utf-8"
            elif self.path in {"/health", "/ready", "/status"}:
                snapshot = supervisor.snapshot()
                status = (
                    200
                    if self.path == "/status"
                    or snapshot["ready" if self.path == "/ready" else "alive"]
                    else 503
                )
                payload = json.dumps(snapshot).encode()
            else:
                status, payload = 404, b'{"error":"not_found"}'
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def run_group(group: str, environment: dict[str, str]) -> int:
    specs = build_processes(group, environment)
    supervisor = PlatformSupervisor(
        specs,
        trading_configured=group == "application"
        or any(spec.name == "indicator" for spec in specs),
    )
    try:
        port = int(environment.get("PLATFORM_HEALTH_PORT", "8090"))
    except ValueError:
        msg = "PLATFORM_HEALTH_PORT must be an integer"
        raise ValueError(msg) from None
    if not 1 <= port <= _MAX_PORT or port in {spec.port for spec in specs}:
        msg = "PLATFORM_HEALTH_PORT must be an unused listener port"
        raise ValueError(msg)
    server = create_health_server(supervisor, port=port)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    stop = threading.Event()
    with _shutdown_signals(stop):
        try:
            supervisor.start()
            thread.start()
            while not stop.wait(0.2):
                supervisor.tick()
                if supervisor.failed:
                    return 1
            return 0
        finally:
            supervisor.stop()
            if thread.is_alive():
                server.shutdown()
                thread.join(timeout=1)
            server.server_close()


def run_job(spec: ProcessSpec, *, timeout: float, lock_directory: Path) -> int:
    """Run one nonoverlapping job; credentials remain within its child process."""
    if not 0 < timeout <= _MAX_JOB_SECONDS:
        msg = "Job timeout must be between zero and 86400 seconds"
        raise ValueError(msg)
    lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (lock_directory / f"{spec.name}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.exception("The selected job is already running", component=spec.name)
            return 75
        stop = threading.Event()
        process: subprocess.Popen[bytes] | None = None
        with _shutdown_signals(stop):
            try:
                process = subprocess.Popen(
                    spec.command,
                    env=spec.environment,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if stop.wait(min(0.1, max(0, deadline - time.monotonic()))):
                        return 130
                    if time.monotonic() >= deadline:
                        logger.error("Job exceeded its runtime bound", component=spec.name)
                        return 124
                return process.wait()
            finally:
                if process is not None:
                    _signal_group(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        _signal_group(process.pid, signal.SIGKILL)
                        process.wait(timeout=1)
                    finally:
                        _signal_group(process.pid, signal.SIGKILL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="group", required=True)
    for group in ("application", "workers", "all"):
        modes.add_parser(group)
    jobs = modes.add_parser("job", help="Run a bounded existing job inside a runtime container")
    jobs.add_argument("name", choices=("backfill", "quality-compounder"))
    jobs.add_argument("--timeout-seconds", type=float, default=3600)
    args = parser.parse_args()
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        if args.group == "job":
            return run_job(
                build_job(args.name, os.environ),
                timeout=args.timeout_seconds,
                lock_directory=Path("/tmp/vynmatrix-jobs"),
            )
        return run_group(args.group, dict(os.environ))
    except (ValueError, OSError) as error:
        # Configuration errors contain field names only; never dump the parent
        # environment or child command/credential values at this boundary.
        logger.error(  # noqa: TRY400 -- dependency error text can contain credential material.
            "Platform launcher failed",
            error_type=type(error).__name__,
            reason=str(error) if isinstance(error, ValueError) else "runtime_resource_unavailable",
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
