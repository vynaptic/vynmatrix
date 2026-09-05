"""Real subprocess and configuration-boundary tests for the platform launcher."""

from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest


def _module(name: str = "platform_processes") -> Any:
    assert (Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py").is_file()
    return importlib.import_module(f"scripts.{name}")


@pytest.fixture
def runtime_env() -> dict[str, str]:
    return {
        **{
            f"{role}_DATABASE_URL": f"postgresql://vm_{login}_login:secret@postgres/db"
            for role, login in (
                ("BACKEND", "backend"),
                ("SCORING", "scoring"),
                ("EXECUTION", "execution"),
                ("FEEDBACK", "feedback"),
                ("MARKET_DATA", "market_data"),
                ("INDICATOR", "indicator"),
            )
        },
        "BACKEND_ADMIN_API_KEY": "backend-secret",
        "SCORING_API_KEY": "scoring-secret",
        "EXECUTION_API_KEY": "execution-secret",
        "SCORING_ADMIN_API_KEY": "scoring-admin-secret",
        "EXECUTION_ADMIN_API_KEY": "execution-admin-secret",
        "FEEDBACK_API_KEY": "feedback-secret",
        "MARKET_DATA_API_KEY": "market-data-secret",
        "SECRETS_MASTER_KEYS": "encrypted-key-ring",
        "UNRELATED_SECRET": "must-not-be-inherited",
        "PROMETHEUS_MULTIPROC_DIR": "/tmp/wrong-shared-directory",
    }


def test_application_credentials_are_scoped_to_each_real_child(
    runtime_env: dict[str, str], tmp_path: Path
) -> None:
    module = _module()
    specs = module.build_processes("application", runtime_env)
    for spec in specs:
        output = tmp_path / f"{spec.name}.json"
        import subprocess

        subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,json,sys;open(sys.argv[1],'w').write(json.dumps(dict(os.environ)))",
                str(output),
            ],
            env=spec.environment,
            check=True,
        )
        child = json.loads(output.read_text())
        assert "UNRELATED_SECRET" not in child
        assert "SCORING_DATABASE_URL" not in child
        assert "PROMETHEUS_MULTIPROC_DIR" not in child
        assert child["DATABASE_URL"].startswith(f"postgresql://vm_{spec.name}_login:")
        assert child["EXECUTION_MODE"] == "paper"
        assert child["EXECUTION_ENGINE_ALLOW_LIVE"] == "false"
        if spec.name == "backend":
            assert child["BACKEND_ADMIN_API_KEY"] == "backend-secret"
            assert "API_KEY" not in child
            assert "ADMIN_API_KEY" not in child
        elif spec.name == "scoring":
            assert child["API_KEY"] == "scoring-secret"
            assert child["EXEC_ENGINE_API_KEY"] == "execution-secret"
            assert child["ADMIN_API_KEY"] == "scoring-admin-secret"
            assert "execution-admin-secret" not in child.values()
            assert "SECRETS_MASTER_KEYS" not in child
        else:
            assert child["API_KEY"] == "execution-secret"
            assert child["ADMIN_API_KEY"] == "execution-admin-secret"
            assert "scoring-admin-secret" not in child.values()
            assert child["SECRETS_MASTER_KEYS"] == "encrypted-key-ring"


@pytest.mark.parametrize("key", ["SCORING_ADMIN_API_KEY", "EXECUTION_ADMIN_API_KEY"])
def test_missing_scoped_admin_key_blocks_application_start(runtime_env, key) -> None:
    runtime_env.pop(key)
    with pytest.raises(ValueError, match=key):
        _module().build_processes("application", runtime_env)


@pytest.mark.parametrize("key", ["SCORING_ADMIN_API_KEY", "EXECUTION_ADMIN_API_KEY"])
@pytest.mark.parametrize(
    "other",
    [
        "BACKEND_ADMIN_API_KEY",
        "SCORING_API_KEY",
        "EXECUTION_API_KEY",
        "FEEDBACK_API_KEY",
        "MARKET_DATA_API_KEY",
    ],
)
def test_admin_key_cannot_reuse_any_service_or_backend_key(runtime_env, key, other) -> None:
    runtime_env[key] = runtime_env[other]
    with pytest.raises(ValueError, match="distinct"):
        _module().build_processes("application", runtime_env)


def test_admin_keys_are_distinct_and_never_reach_worker_children(runtime_env) -> None:
    runtime_env["EXECUTION_ADMIN_API_KEY"] = runtime_env["SCORING_ADMIN_API_KEY"]
    with pytest.raises(ValueError, match="distinct"):
        _module().build_processes("application", runtime_env)
    for child in _module().build_processes("workers", runtime_env):
        assert "ADMIN_API_KEY" not in child.environment
        assert "SCORING_ADMIN_API_KEY" not in child.environment
        assert "EXECUTION_ADMIN_API_KEY" not in child.environment


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DATABASE_URL", "postgresql://admin:private@postgres/db"),
        ("DB_PASSWORD", "private"),
        ("MIGRATION_DATABASE_URL", "private"),
        ("EXECUTION_MODE", "live"),
        ("EXECUTION_ENGINE_ALLOW_LIVE", "true"),
    ],
)
def test_unsafe_launcher_environment_is_rejected_without_secret_value(
    runtime_env: dict[str, str], key: str, value: str
) -> None:
    runtime_env[key] = value
    with pytest.raises(ValueError, match=r"credential|mode setting") as error:
        _module().build_processes("application", runtime_env)
    assert value not in str(error.value)


def test_runtime_role_cannot_be_replaced_with_migration_login(runtime_env: dict[str, str]) -> None:
    runtime_env["EXECUTION_DATABASE_URL"] = "postgresql://trader:private@postgres/db"
    with pytest.raises(ValueError, match="EXECUTION_DATABASE_URL"):
        _module().build_processes("application", runtime_env)


def test_unconfigured_installation_runs_feedback_without_empty_indicator(
    runtime_env: dict[str, str],
) -> None:
    specs = _module().build_processes("workers", runtime_env)
    assert [spec.name for spec in specs] == ["feedback"]
    assert specs[0].command[-1] == "daemon"


def test_selection_preserves_internal_routes_and_unique_listener_ports(
    runtime_env: dict[str, str],
) -> None:
    runtime_env.update(
        {
            "PLATFORM_WORKERS": "market-data,equity,fx,calendar-ibkr,calendar-saxo,calendar-zerodha",
            "STRATEGY_LIST": "SwingHighLowPMO",
            "INGESTOR_SYMBOLS": "BTC-USDC",
            "EQUITY_INGESTOR_SYMBOLS": "AAPL",
            "IBKR_MARKET_CALENDAR_SYMBOLS": "AAPL",
            "SAXO_MARKET_CALENDAR_SYMBOLS": "EURUSD",
            "ZERODHA_MARKET_CALENDAR_SYMBOLS": "NIFTY",
            "EODHD_API_TOKEN": "equity-secret",
            "IBKR_GATEWAY_URL": "https://gateway:5000",
        }
    )
    module = _module()
    specs = module.build_processes("all", runtime_env)
    by_name = {spec.name: spec for spec in specs}
    assert len({spec.port for spec in specs}) == len(specs)
    assert by_name["indicator"].environment["SIGNAL_API_URL"] == "http://127.0.0.1:8001"
    assert (
        by_name["calendar-ibkr"].environment["MARKET_CALENDAR_API_URL"] == "http://127.0.0.1:8081"
    )
    assert "EODHD_API_TOKEN" not in by_name["indicator"].environment
    assert "BACKEND_ADMIN_API_KEY" not in by_name["calendar-ibkr"].environment
    assert by_name["calendar-ibkr"].environment["MARKET_CALENDAR_ADMIN_API_KEY"] == "backend-secret"
    assert by_name["equity"].environment["INGESTOR_SYMBOLS"] == "AAPL"
    assert by_name["equity"].environment["INGESTOR_SOURCE"] == "eodhd"
    assert (
        by_name["indicator"].environment["PROMETHEUS_MULTIPROC_DIR"]
        != "/tmp/wrong-shared-directory"
    )
    split = {spec.name: spec for spec in module.build_processes("workers", runtime_env)}
    assert split["indicator"].environment["SIGNAL_API_URL"] == "http://application:8001"


@pytest.mark.parametrize("selection", ["unknown", "fx,fx", "fx,,equity"])
def test_invalid_worker_selection_does_not_fall_back(
    runtime_env: dict[str, str], selection: str
) -> None:
    runtime_env["PLATFORM_WORKERS"] = selection
    with pytest.raises(ValueError, match="PLATFORM_WORKERS"):
        _module().build_processes("workers", runtime_env)


def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "child did not reach expected state"
        time.sleep(0.01)


def test_repeated_child_failure_is_bounded_and_reaped(tmp_path: Path) -> None:
    module = _module("run_platform")
    specs = _module()
    attempts = tmp_path / "attempts"
    spec = specs.ProcessSpec(
        name="failing",
        command=(
            sys.executable,
            "-c",
            "import sys;open(sys.argv[1],'a').write('attempt\\n');sys.exit(3)",
            str(attempts),
        ),
        environment={},
        port=0,
    )
    from lib_common.runner_utils import RestartPolicy

    supervisor = module.PlatformSupervisor(
        [spec],
        restart_policy=RestartPolicy(max_restarts=2, cooldown_seconds=0, backoff_base_seconds=0),
    )
    try:
        supervisor.start()

        def exhausted() -> bool:
            supervisor.tick()
            return supervisor.failed

        _until(exhausted)
        assert attempts.read_text().splitlines() == ["attempt"] * 3
        snapshot = supervisor.snapshot(probe=False)
        assert snapshot["components"]["failing"]["exhausted"] is True
        assert snapshot["alive"] is False
    finally:
        supervisor.stop(timeout=0.2)


def test_shutdown_kills_descendants_with_one_total_budget(tmp_path: Path) -> None:
    module = _module("run_platform")
    specs = _module()
    marker = tmp_path / "grandchild"
    child_script = "import os,signal,time,sys;signal.signal(signal.SIGTERM,signal.SIG_IGN);open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(60)"
    parent_script = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);time.sleep(60)"
    spec = specs.ProcessSpec(
        name="tree",
        command=(sys.executable, "-c", parent_script, child_script, str(marker)),
        environment={},
        port=0,
    )
    supervisor = module.PlatformSupervisor([spec])
    supervisor.start()
    try:
        _until(marker.exists)
        pid = int(marker.read_text())
        started = time.monotonic()
        supervisor.stop(timeout=0.25)
        assert time.monotonic() - started < 1.0
        import psutil

        _until(
            lambda: not psutil.pid_exists(pid)
            or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        )
        assert supervisor.snapshot(probe=False)["alive"] is False
    finally:
        supervisor.stop(timeout=0.1)


def test_explicit_jobs_require_provider_selection_and_panel_gate(
    runtime_env: dict[str, str],
) -> None:
    module = _module()
    with pytest.raises(ValueError, match="INGESTOR_SYMBOLS"):
        module.build_job("backfill", runtime_env)
    with pytest.raises(ValueError, match="QUALITY_COMPOUNDER_PANELS_ENABLED"):
        module.build_job("quality-compounder", runtime_env)


def test_worker_service_auth_is_configured_without_broker_key_leaks(
    runtime_env: dict[str, str],
) -> None:
    runtime_env["PLATFORM_WORKERS"] = "fx"
    runtime_env["COINBASE_API_SECRET"] = "trade-secret"
    specs = {spec.name: spec for spec in _module().build_processes("workers", runtime_env)}
    assert specs["feedback"].environment["API_KEY"] == "feedback-secret"
    assert specs["fx"].environment["API_KEY"] == "market-data-secret"
    for spec in specs.values():
        assert "SECRETS_MASTER_KEYS" not in spec.environment
        assert "COINBASE_API_SECRET" not in spec.environment


def test_feed_outage_is_visible_without_breaking_management_liveness() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import urlopen

    class FeedHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = (
                b"feed_fresh 0\n"
                if self.path == "/metrics"
                else b'{"ready":false,"checks":{"market_data_fresh":false}}'
            )
            self.send_response(200 if self.path in {"/metrics", "/live"} else 503)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: Any) -> None:
            pass

    feed = ThreadingHTTPServer(("127.0.0.1", 0), FeedHandler)
    feed_thread = threading.Thread(target=feed.serve_forever, daemon=True)
    feed_thread.start()
    module = _module("run_platform")
    spec = _module().ProcessSpec(
        name="feed",
        command=(sys.executable, "-c", "import time;time.sleep(30)"),
        environment={},
        port=feed.server_port,
    )
    supervisor = module.PlatformSupervisor([spec])
    supervisor.start()
    server = module.create_health_server(supervisor, port=0)
    assert server.server_address[0] == "127.0.0.1"
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/health") as response:
            assert json.load(response)["alive"] is True
        with pytest.raises(HTTPError, match="503") as error:
            urlopen(f"{base}/ready")
        readiness = json.load(error.value)
        assert readiness["components"]["feed"]["progress"]["checks"]["market_data_fresh"] is False
        with urlopen(f"{base}/metrics/feed") as response:
            assert response.read() == b"feed_fresh 0\n"
        with urlopen(f"{base}/metrics") as response:
            assert b'component="feed"} 1' in response.read()
    finally:
        supervisor.stop(timeout=0.2)
        for endpoint in (server, feed):
            endpoint.shutdown()
            endpoint.server_close()
        server_thread.join(timeout=1)
        feed_thread.join(timeout=1)


def test_job_timeout_reaps_child_and_lock_prevents_overlap(tmp_path: Path) -> None:
    import fcntl

    module = _module("run_platform")
    spec = _module().ProcessSpec(
        name="backfill",
        command=(sys.executable, "-c", "import time;time.sleep(30)"),
        environment={},
        port=0,
    )
    started = time.monotonic()
    assert module.run_job(spec, timeout=0.1, lock_directory=tmp_path) == 124
    assert time.monotonic() - started < 1
    with (tmp_path / "backfill.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert module.run_job(spec, timeout=0.1, lock_directory=tmp_path) == 75


def test_running_pid_with_missing_http_listener_is_not_management_healthy() -> None:
    import socket

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        module = _module("run_platform")
        spec = _module().ProcessSpec(
            name="api",
            command=(sys.executable, "-c", "import time;time.sleep(30)"),
            environment={},
            port=port,
        )
        supervisor = module.PlatformSupervisor([spec])
        supervisor.start()
        try:
            assert supervisor.snapshot()["alive"] is False
        finally:
            supervisor.stop(timeout=0.1)


def test_group_sigterm_stops_and_reaps_its_application_child(tmp_path: Path) -> None:
    import os
    import signal
    import socket
    import subprocess

    marker = tmp_path / "child"
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    child_code = "import os,sys,time;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(30)"
    harness = "\n".join(
        [
            "import sys",
            "from scripts import run_platform as runtime",
            "from scripts.platform_processes import ProcessSpec",
            "spec=ProcessSpec('test-child',(sys.executable,'-c',sys.argv[1],sys.argv[2]),{},0)",
            "runtime.build_processes=lambda group,environment:[spec]",
            "raise SystemExit(runtime.run_group('application',{'PLATFORM_HEALTH_PORT':sys.argv[3]}))",
        ]
    )
    process = subprocess.Popen([sys.executable, "-c", harness, child_code, str(marker), str(port)])
    try:
        _until(marker.exists)
        pid = int(marker.read_text())
        os.kill(process.pid, signal.SIGTERM)
        assert process.wait(timeout=5) == 0
        import psutil

        assert not psutil.pid_exists(pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)


@pytest.mark.parametrize("source", ["deribit_testnet", "delta_india", "saxo_simulation"])
def test_existing_market_sources_remain_selectable(
    runtime_env: dict[str, str], source: str
) -> None:
    runtime_env.update(
        {"PLATFORM_WORKERS": "market-data", "INGESTOR_SOURCE": source, "INGESTOR_SYMBOLS": "BTC"}
    )
    specs = _module().build_processes("workers", runtime_env)
    assert specs[-1].environment["INGESTOR_SOURCE"] == source


def test_indicator_metrics_generations_do_not_mix_after_a_restart(tmp_path: Path) -> None:
    from lib_common.runner_utils import RestartPolicy

    output = tmp_path / "generations"
    module = _module("run_platform")
    spec = _module().ProcessSpec(
        name="indicator",
        command=(
            sys.executable,
            "-c",
            "import os,sys;open(sys.argv[1],'a').write(os.environ['PROMETHEUS_MULTIPROC_DIR']+'\\n')",
            str(output),
        ),
        environment={"PROMETHEUS_MULTIPROC_DIR": str(tmp_path / "metrics")},
        port=0,
    )
    supervisor = module.PlatformSupervisor(
        [spec],
        restart_policy=RestartPolicy(max_restarts=1, cooldown_seconds=0, backoff_base_seconds=0),
    )
    supervisor.start()
    try:

        def exhausted() -> bool:
            supervisor.tick()
            return supervisor.failed

        _until(exhausted)
    finally:
        supervisor.stop(timeout=0.2)
    paths = output.read_text().splitlines()
    assert len(paths) == 2
    assert paths[0] != paths[1]
    assert all(not Path(path).exists() for path in paths)


def test_component_metrics_proxy_forwards_child_service_key() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.request import urlopen

    class KeyedHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/metrics" and self.headers.get("X-API-Key") != "feed-secret":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"Invalid or missing API key")
                return
            payload = (
                b"feed_fresh 1\n" if self.path == "/metrics" else b'{"ready":true,"checks":{}}'
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: Any) -> None:
            pass

    feed = ThreadingHTTPServer(("127.0.0.1", 0), KeyedHandler)
    feed_thread = threading.Thread(target=feed.serve_forever, daemon=True)
    feed_thread.start()
    module = _module("run_platform")
    spec = _module().ProcessSpec(
        name="feed",
        command=(sys.executable, "-c", "import time;time.sleep(30)"),
        environment={"API_KEY": "feed-secret"},
        port=feed.server_port,
    )
    supervisor = module.PlatformSupervisor([spec])
    supervisor.start()
    server = module.create_health_server(supervisor, host="127.0.0.1", port=0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/metrics/feed") as response:
            assert response.read() == b"feed_fresh 1\n"
    finally:
        supervisor.stop(timeout=0.2)
        for endpoint in (server, feed):
            endpoint.shutdown()
            endpoint.server_close()
        server_thread.join(timeout=1)
        feed_thread.join(timeout=1)


def test_indicator_child_uses_bounded_strategy_pool_and_forwards_idle_interval(
    runtime_env: dict[str, str],
) -> None:
    runtime_env["STRATEGY_LIST"] = "SwingHighLowPMO"
    runtime_env["DB_POOL_SIZE"] = "3"
    runtime_env["DB_MAX_OVERFLOW"] = "2"
    runtime_env["DB_POOL_CONNECTION_BUDGET"] = "5"
    runtime_env["SIGNAL_RELAY_IDLE_INTERVAL_SEC"] = "7"
    specs = {spec.name: spec for spec in _module().build_processes("workers", runtime_env)}

    indicator = specs["indicator"].environment
    # Strategy grandchildren inherit this environment, so the fixed 2/0 pool
    # bounds every worker to two pooled connections plus its LISTEN connection.
    assert indicator["DB_POOL_SIZE"] == "2"
    assert indicator["DB_MAX_OVERFLOW"] == "0"
    assert indicator["DB_POOL_CONNECTION_BUDGET"] == "2"
    assert indicator["SIGNAL_RELAY_IDLE_INTERVAL_SEC"] == "7"

    feedback = specs["feedback"].environment
    assert feedback["DB_POOL_SIZE"] == "3"
    assert feedback["DB_MAX_OVERFLOW"] == "2"
    assert "SIGNAL_RELAY_IDLE_INTERVAL_SEC" not in feedback


def test_scoring_child_forwards_outbox_notify_flag(runtime_env: dict[str, str]) -> None:
    runtime_env["SCORING_OUTBOX_NOTIFY_ENABLED"] = "false"
    specs = {spec.name: spec for spec in _module().build_processes("application", runtime_env)}
    assert specs["scoring"].environment["SCORING_OUTBOX_NOTIFY_ENABLED"] == "false"
    assert "SCORING_OUTBOX_NOTIFY_ENABLED" not in specs["execution"].environment
