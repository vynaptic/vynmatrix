"""Host orchestration never imports editor code or trusts editor performance."""

from __future__ import annotations

import importlib
import io
import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev_cli.core.research.sandbox import SANDBOX_PROGRAM, SandboxSpec

JOB = "a85a9daa-990d-4d4c-91c7-b650b8580117"
GENERATION = "7df58e14-23fa-426d-b3b5-30f270d569d3"
IMAGE = "sha256:" + "a" * 64
SPEC = SandboxSpec(JOB, GENERATION, IMAGE, "b" * 64)


def module(name):
    return importlib.import_module("dev_cli.core.research." + name)


def dataset():
    at = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "dataset_id": JOB,
        "content_hash": "c" * 64,
        "eligibility": {"training": True, "backtest": True},
        "metadata": {
            "instrument_id": 1,
            "source": "recorded",
            "timeframe": "1h",
            "currency": "USD",
            "timestamp_semantics": "bar_open",
            "instrument_terms": {
                "canonical": "BTCUSD",
                "asset_class": "crypto",
                "settlement_currency": "USD",
                "is_tradable": True,
                "market_session_policy": "continuous",
                "lot_size": "0.000001",
                "tick_size": "0.01",
            },
            "timing_evidence": {"policy": "continuous"},
        },
        "bars": [
            {
                "ts": (at + timedelta(hours=i)).isoformat(),
                "bar_open": (at + timedelta(hours=i)).isoformat(),
                "bar_close": (at + timedelta(hours=i + 1)).isoformat(),
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100",
                "volume": "10",
            }
            for i in range(8)
        ],
    }


ASSUMPTIONS = {
    "initial_capital": "1000",
    "currency": "USD",
    "size_pct": "0.5",
    "fee_bps": "10",
    "slippage_bps": "2",
    "annualization_factor": "365",
}
REVISION = {
    "revision_id": JOB,
    "source": "raise RuntimeError('must never execute on host')",
    "config": {"class_name": "UserStrategy", "strategy_id": "Draft", "parameters": {}},
}


class Conversation:
    def __init__(self, *, warmup=2, short=False):
        self.messages = []
        self.warmup = warmup
        self.short = short
        self.closed = False

    def send(self, message):
        self.messages.append(message)

    def receive(self):
        message = self.messages[-1]
        if message["kind"] == "start":
            return {"kind": "ready", "warmup_bars": self.warmup}
        assert message["kind"] == "bar"
        # Any future-data delivery would be visible in this message.
        assert "bars" not in message
        assert "source" not in message["bar"]
        return {
            "sequence": message["sequence"],
            "signals": [
                {
                    "action": "SHORT" if self.short else "HOLD",
                    "symbol": "BTCUSD",
                    "timestamp": message["bar"]["ts"],
                    "confidence": 0.5,
                }
            ],
        }

    def finish(self):
        self.closed = True


def test_strategy_only_receives_current_bars_and_warmup_is_discarded():
    wire = Conversation()
    result = module("backtest").run_backtest(wire, dataset(), REVISION, ASSUMPTIONS)
    messages = [item for item in wire.messages if item["kind"] == "bar"]
    assert [item["sequence"] for item in messages] == list(range(8))
    assert [item["warmup"] for item in messages] == [True, True] + [False] * 6
    assert messages[0]["bar"]["ts"] == dataset()["bars"][0]["bar_close"]
    assert result["bootstrap_bars"] == 2
    assert result["signals_emitted"] == 6
    assert result["currency"] == "USD"
    assert result["total_trades"] == 0
    assert result["sharpe_ratio"] is None
    assert result["profit_factor"] is None
    assert result["raw_signal_ledger"]
    assert result["equity_curve"]
    assert result["trades"] == []
    assert wire.closed


@pytest.mark.parametrize("mutation", ["ineligible", "currency", "product", "session", "timing"])
def test_invalid_dataset_refused_before_source_delivery(mutation):
    data, wire = dataset(), Conversation()
    if mutation == "ineligible":
        data["eligibility"]["backtest"] = False
    elif mutation == "currency":
        data["metadata"]["currency"] = "EUR"
    elif mutation == "product":
        data["metadata"]["instrument_terms"]["asset_class"] = "futures"
    elif mutation == "session":
        data["metadata"]["instrument_terms"]["asset_class"] = "equity"
    else:
        del data["bars"][0]["bar_close"]
    with pytest.raises(ValueError, match=r"eligible|currency|products|session|timing|close"):
        module("backtest").run_backtest(wire, data, REVISION, ASSUMPTIONS)
    assert not wire.messages


def test_short_signal_refused_by_fully_funded_execution():
    with pytest.raises(ValueError, match="long-only"):
        module("backtest").run_backtest(Conversation(short=True), dataset(), REVISION, ASSUMPTIONS)


def test_model_rows_use_authoritative_close_and_eligibility():
    value = module("backtest").model_rows(dataset())
    assert value[0]["ts"] == dataset()["bars"][0]["bar_close"]
    data = dataset()
    data["eligibility"]["training"] = False
    with pytest.raises(ValueError, match="eligible"):
        module("backtest").model_rows(data)


def inspection():
    return {
        "Id": "d" * 64,
        "Name": "/" + SPEC.container_name,
        "Image": IMAGE,
        "Config": {
            "User": "10001",
            "Cmd": SANDBOX_PROGRAM,
            "Env": [
                "EXECUTION_MODE=paper",
                "EXECUTION_ENGINE_ALLOW_LIVE=false",
                "OPENBLAS_NUM_THREADS=1",
                "OMP_NUM_THREADS=1",
                "MKL_NUM_THREADS=1",
                "NUMEXPR_NUM_THREADS=1",
            ],
            "Labels": {
                "com.vynmatrix.research.job": JOB,
                "com.vynmatrix.research.generation": GENERATION,
            },
        },
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": 32,
            "Memory": 1073741824,
            "NanoCpus": 1000000000,
            "Tmpfs": {"/tmp": "size=64m,mode=1777"},
        },
        "Mounts": [],
    }


class Child:
    def __init__(self, output=b'{"kind":"result","value":{}}\n'):
        self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(output), io.BytesIO()
        self.pid = 0

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class Docker:
    def __init__(self, *, actual=None, output=b'{"kind":"result","value":{}}\n'):
        self.actual = actual if actual is not None else inspection()
        self.child = Child(output)
        self.removed = []
        self.events = []

    def start(self, command):
        self.events.append("start")
        return self.child

    def inspect(self, target):
        self.events.append("inspect")
        return self.actual

    def remove(self, container_id):
        self.removed.append(container_id)

    def running(self):
        return [self.actual]

    def sandboxes(self):
        return [self.actual]


def test_source_never_delivered_until_actual_containment_verified():
    docker = Docker()
    docker.actual["HostConfig"]["NetworkMode"] = "bridge"
    with (
        pytest.raises(ValueError, match="isolation"),
        module("process").SandboxProcess(Path("/repo"), SPEC, docker=docker) as wire,
    ):
        wire.send({"kind": "init", "source": "secret editor source"})
    assert docker.child.stdin.getvalue() == b""
    assert docker.removed == ["d" * 64]


def test_protocol_overflow_kills_exact_verified_container():
    docker = Docker(output=b"x" * 65537 + b"\n")
    with (
        pytest.raises(ValueError, match="message"),
        module("process").SandboxProcess(Path("/repo"), SPEC, docker=docker) as wire,
    ):
        wire.receive()
    assert docker.removed == ["d" * 64]


def test_cancellation_kills_exact_container_and_fences_result():
    docker = Docker()

    def cancelled():
        raise RuntimeError("cancelled")

    with (
        pytest.raises(RuntimeError, match="cancelled"),
        module("process").SandboxProcess(
            Path("/repo"), SPEC, docker=docker, poll=cancelled
        ) as wire,
    ):
        wire.receive()
    assert docker.removed == ["d" * 64]


def test_frozen_bundle_detects_code_change_and_does_not_read_checkout(tmp_path, monkeypatch):
    bundle = module("bundle")
    source = tmp_path / "source"
    package = source / "tools/dev_cli/dev_cli/core/research"
    package.mkdir(parents=True)
    (package / "worker.py").write_text("value = 1\n")
    monkeypatch.setattr(bundle, "environment_fingerprint", lambda: {"python": "test"})
    path, digest = bundle.freeze_bundle(source, tmp_path / "bundles")
    (package / "worker.py").write_text("value = 2\n")
    assert bundle.verify_bundle(path, digest)["files"]
    frozen = path / "tools/dev_cli/dev_cli/core/research/worker.py"
    frozen.chmod(0o600)
    frozen.write_text("value = 3\n")
    with pytest.raises(ValueError, match="identity"):
        bundle.verify_bundle(path, digest)


def test_result_parts_are_bounded_and_schema_checked():
    messages = iter(
        [
            {"kind": "part", "field": "predictions", "rows": [{"ts": "t"}]},
            {"kind": "result", "value": {"predictions": [], "metrics": {}}},
        ]
    )
    wire = type("Wire", (), {"receive": lambda self: next(messages)})()
    assert module("worker").read_result(wire)["predictions"] == [{"ts": "t"}]
    messages = iter([{"kind": "part", "field": "command", "rows": []}])
    with pytest.raises(ValueError, match="schema"):
        module("worker").read_result(wire)


def test_capacity_requires_combined_same_image_and_reserves_third_slot():
    worker = module("worker")
    postgres = {
        "Image": "postgres",
        "Config": {
            "Labels": {
                "com.docker.compose.project": "vynmatrix",
                "com.docker.compose.service": "postgres",
            }
        },
    }
    application = {
        "Image": IMAGE,
        "Config": {
            "Labels": {
                "com.docker.compose.project": "vynmatrix",
                "com.docker.compose.service": "application",
            },
            "Env": ["PLATFORM_APPLICATION_GROUP=all"],
            "Cmd": ["python", "-m", "scripts.run_platform", "all"],
        },
    }
    rows = [postgres, application]
    docker = SimpleNamespace(running=lambda: rows)
    worker.verify_capacity(docker, IMAGE)
    rows.append(inspection())
    with pytest.raises(ValueError, match="combined"):
        worker.verify_capacity(docker, IMAGE)
    worker.verify_capacity(docker, IMAGE, sandbox_id="d" * 64)
    application["Config"]["Cmd"][-1] = "application"
    with pytest.raises(ValueError, match="combined"):
        worker.verify_capacity(docker, IMAGE, sandbox_id="d" * 64)


def test_protocol_nonfinite_and_stderr_overflow_are_refused():
    process = module("process")
    docker = Docker(output=b'{"value":NaN}\n')
    with (
        pytest.raises(ValueError, match="finite"),
        process.SandboxProcess(Path("/repo"), SPEC, docker=docker) as wire,
    ):
        wire.receive()
    docker = Docker()
    docker.child.stderr = io.BytesIO(b"x" * (1048576 + 1))
    with process.SandboxProcess(Path("/repo"), SPEC, docker=docker) as wire:
        # The bounded reader runs independently of stdout and trips admission.
        for thread in wire.threads[:2]:
            thread.join(timeout=1)
        with pytest.raises(ValueError, match="diagnostic"):
            wire.receive()
    assert docker.removed == ["d" * 64]


def test_diagnostics_are_saved_by_exact_attempt_without_overwrite(tmp_path):
    worker = module("worker")
    job = {"attempt_id": JOB}
    worker.save_diagnostics(tmp_path, job, b"bounded editor error\n")
    artifact = tmp_path / ".artifacts/research/attempts" / (JOB + ".stderr")
    assert artifact.read_bytes() == b"bounded editor error\n"
    assert artifact.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        worker.save_diagnostics(tmp_path, job, b"do not overwrite retained diagnostics")


def test_model_backtest_cutoff_warmup_and_fixed_artifact():
    from lib_strategy.research import train_return_model

    backtest = module("backtest")
    data = dataset()
    template = data["bars"][0]
    at = datetime(2026, 1, 1, tzinfo=UTC)
    data["bars"] = [
        {
            **template,
            "ts": (at + timedelta(hours=i)).isoformat(),
            "bar_open": (at + timedelta(hours=i)).isoformat(),
            "bar_close": (at + timedelta(hours=i + 1)).isoformat(),
        }
        for i in range(1300)
    ]
    identity = {key: data["metadata"][key] for key in ("instrument_id", "source", "timeframe")}
    identity.update(dataset_hash=data["content_hash"], image_hash=IMAGE)
    model = train_return_model(backtest.model_rows(data), identity=identity)["model"]
    wire = Conversation()
    with pytest.raises(ValueError, match="cutoff"):
        backtest.run_backtest(
            wire,
            data,
            REVISION,
            ASSUMPTIONS,
            model=model,
            evaluation_start=data["bars"][0]["bar_close"],
        )
    assert wire.messages == []
    result = backtest.run_backtest(wire, data, REVISION, ASSUMPTIONS, model=model)
    callbacks = [message for message in wire.messages if message["kind"] == "bar"]
    first = next(message for message in callbacks if not message["warmup"])
    assert first["bar"]["ts"] == model["training_cutoff"]
    assert result["bootstrap_bars"] == first["sequence"]
    assert wire.messages[0]["config"]["parameters"]["research_model"] == model
    changed = dataset()
    changed["metadata"]["source"] = "other"
    with pytest.raises(ValueError, match="identity"):
        backtest.compatible_model(model, changed, None)


def test_trusted_engine_produces_next_open_fills_and_charged_costs():
    class Decisions(Conversation):
        def receive(self):
            response = super().receive()
            if "sequence" in response:
                response["signals"][0]["action"] = {2: "LONG", 4: "CLOSE"}.get(
                    response["sequence"], "HOLD"
                )
            return response

    result = module("backtest").run_backtest(Decisions(), dataset(), REVISION, ASSUMPTIONS)
    assert result["total_trades"] == 1
    trade = result["trades"][0]
    assert trade["entry_ts"] == dataset()["bars"][3]["bar_open"]
    assert trade["exit_ts"] == dataset()["bars"][5]["bar_open"]
    assert trade["side"] == "long"
    assert trade["fees"] > 0
    assert trade["pnl"] < 0
    assert result["cost_breakdown"]["commission"] > 0


def test_session_execution_checks_exact_intervals_and_ambiguous_authority():
    backtest, data = module("backtest"), dataset()
    data["metadata"]["instrument_terms"]["asset_class"] = "equity"
    session = {"opens_at": data["bars"][0]["bar_open"], "closes_at": data["bars"][-1]["bar_close"]}
    data["metadata"]["timing_evidence"] = {
        "kind": "authoritative_calendar",
        "calendar": {"calendar_id": "recorded"},
        "sessions": [session],
    }
    assert len(backtest.execution_bars(data)) == 8
    data["metadata"]["timing_evidence"]["sessions"].append(dict(session))
    with pytest.raises(ValueError, match="ambiguous"):
        backtest.execution_bars(data)


def test_store_releases_transaction_before_returning_inputs(monkeypatch):
    worker = module("worker")
    active = []

    @contextmanager
    def transaction():
        active.append(True)
        try:
            yield
        finally:
            active.pop()

    @contextmanager
    def owner(_session):
        assert active
        yield "owner"

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            assert not active

        def begin(self):
            return transaction()

    monkeypatch.setattr(worker, "owner_scope", owner)
    store = worker.ResearchStore(Session, worker_id=JOB, image_hash=IMAGE, bundle_hash="b" * 64)
    assert store.transaction(lambda _session, owner_id: owner_id) == "owner"
    assert active == []


@pytest.mark.parametrize("status", ["interrupted", "cancelled", "running", "unknown"])
def test_orphan_reconciliation_only_removes_known_terminal_attempt(monkeypatch, status):
    worker = module("worker")
    monkeypatch.setattr(worker.research_jobs, "reconcile_expired_attempts", lambda _session: [])
    row = (
        None
        if status == "unknown"
        else SimpleNamespace(status=status, bundle_hash="b" * 64, generation=GENERATION)
    )
    session = SimpleNamespace(scalar=lambda _statement: row)
    store = worker.ResearchStore(None, worker_id=JOB, image_hash=IMAGE, bundle_hash="b" * 64)
    monkeypatch.setattr(store, "transaction", lambda operation: operation(session, "owner"))
    docker = Docker()
    if status in {"running", "unknown"}:
        with pytest.raises(ValueError, match="blocks"):
            store.reconcile(docker)
        assert docker.removed == []
    else:
        store.reconcile(docker)
        assert docker.removed == ["d" * 64]


def test_worker_reuses_runtime_role_authority_gate(monkeypatch):
    from dev_cli.core.runtime_roles import RUNTIME_ROLES

    worker = module("worker")
    catalog = {}
    for login, group in RUNTIME_ROLES:
        flags = dict.fromkeys(
            (
                "rolsuper",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
                "owns_objects",
            ),
            False,
        )
        catalog[group] = {**flags, "rolcanlogin": False, "memberships": []}
        catalog[login] = {
            **flags,
            "rolcanlogin": True,
            "rolinherit": True,
            "memberships": [{"role": group, "inherit": True, "admin": False}],
        }
    monkeypatch.setattr(worker, "_catalog", lambda _session: catalog)
    session = SimpleNamespace(scalar=lambda _statement: "vm_research_login")
    worker.verify_database_login(session)
    catalog["vm_research_login"]["memberships"].append(
        {"role": "vm_backend", "inherit": True, "admin": False}
    )
    with pytest.raises(ValueError, match="authority"):
        worker.verify_database_login(session)


def test_exited_orphan_is_reconciled_before_retry_container_name_is_reused(monkeypatch):
    worker = module("worker")
    monkeypatch.setattr(worker.research_jobs, "reconcile_expired_attempts", lambda _session: [])
    row = SimpleNamespace(status="interrupted", bundle_hash="b" * 64, generation=GENERATION)
    session = SimpleNamespace(scalar=lambda _statement: row)
    store = worker.ResearchStore(None, worker_id=JOB, image_hash=IMAGE, bundle_hash="b" * 64)
    monkeypatch.setattr(store, "transaction", lambda operation: operation(session, "owner"))
    docker = Docker()
    monkeypatch.setattr(docker, "running", list)
    store.reconcile(docker)
    assert docker.removed == ["d" * 64]


def test_copied_labels_on_another_container_do_not_authorize_cleanup():
    actual = inspection()
    actual["Name"] = "/unrelated-container"
    with pytest.raises(ValueError, match="identity"):
        module("process").exact_identity(actual, SPEC)


def test_model_parameter_requires_selected_registered_artifact():
    revision = {
        **REVISION,
        "config": {
            **REVISION["config"],
            "parameters": {"research_model": {"training_cutoff": "2026-01-01T00:00:00+00:00"}},
        },
    }
    wire = Conversation()
    with pytest.raises(ValueError, match="registered"):
        module("backtest").run_backtest(wire, dataset(), revision, ASSUMPTIONS)
    assert wire.messages == []


def test_deep_untrusted_json_fails_attempt_without_crashing_supervisor():
    docker = Docker(output=b'{"x":' * 1500 + b"0" + b"}" * 1500 + b"\n")
    with (
        pytest.raises(ValueError, match="nesting"),
        module("process").SandboxProcess(Path("/repo"), SPEC, docker=docker) as wire,
    ):
        wire.receive()
    assert docker.removed == ["d" * 64]


def test_public_equity_curve_contract_matches_chart_points():
    result = module("backtest").run_backtest(Conversation(), dataset(), REVISION, ASSUMPTIONS)
    point = result["equity_curve"][0]
    assert set(point) == {"at", "value"}
    assert point["at"] == dataset()["bars"][2]["bar_close"]
    assert point["value"] == 1000.0


def test_one_negative_daily_return_has_undefined_sortino():
    report = SimpleNamespace(
        to_dict=lambda: {"sharpe_ratio": 0.0, "sortino_ratio": 0.0},
        trades=[],
        equity_curve=[
            (datetime(2026, 1, 1, tzinfo=UTC), 1000.0),
            (datetime(2026, 1, 2, tzinfo=UTC), 900.0),
        ],
        metrics=SimpleNamespace(max_drawdown_pct=10),
        turnover_notional=0,
    )
    result = module("backtest")._result(report, dataset(), ASSUMPTIONS)
    assert result["sharpe_ratio"] is None
    assert result["sortino_ratio"] is None


def busy_store(monkeypatch, **changes):
    worker = module("worker")
    active, opened, slept = [], [], []

    @contextmanager
    def owner(_session):
        yield "owner"

    class Session:
        def __init__(self):
            self.number = len(opened)
            opened.append(self)

        def __enter__(self):
            assert not active
            active.append(self.number)
            return self

        def __exit__(self, *_args):
            active.pop()

        def begin(self):
            return owner(self)

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    status = SimpleNamespace(
        maintenance=False,
        generation=GENERATION,
        worker_id=JOB,
        image_hash=IMAGE,
        bundle_hash="b" * 64,
    )
    for key, value in changes.items():
        setattr(status, key, value)
    monkeypatch.setattr(worker, "owner_scope", owner)
    monkeypatch.setattr(
        worker.research_jobs, "get_research_control", lambda _session, *, lock: status
    )

    def pause(seconds):
        assert not active
        slept.append(seconds)

    monkeypatch.setattr(worker.time, "sleep", pause)
    store = worker.ResearchStore(Session, worker_id=JOB, image_hash=IMAGE, bundle_hash="b" * 64)
    store.generation = GENERATION
    store._identity_claimed = True
    return store, opened, slept


def busy_error():
    return module("worker").ResearchWorkflowError(
        "research is fenced for maintenance or another control transaction", status_code=503
    )


def test_control_busy_retries_only_after_fresh_matching_status_transaction(monkeypatch):
    store, opened, slept = busy_store(monkeypatch)
    attempts = []

    def operation(session, _owner):
        attempts.append(session.number)
        if len(attempts) < 3:
            raise busy_error()
        return "saved once"

    assert store.transaction(operation) == "saved once"
    assert attempts == [0, 2, 4]
    assert len(opened) == 5
    assert len(slept) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"maintenance": True},
        {"generation": JOB},
        {"worker_id": GENERATION},
        {"image_hash": "sha256:" + "c" * 64},
        {"bundle_hash": "c" * 64},
    ],
)
def test_control_busy_never_retries_changed_or_maintenance_fence(monkeypatch, changes):
    store, _opened, slept = busy_store(monkeypatch, **changes)
    attempts = []

    def operation(_session, _owner):
        attempts.append(True)
        raise busy_error()

    with pytest.raises(module("worker").ResearchWorkflowError, match="fenced"):
        store.transaction(operation)
    assert attempts == [True]
    assert slept == []


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        (409, "research attempt was fenced; result refused"),
        (503, "research is fenced for maintenance"),
        (503, "research control is not initialized"),
    ],
)
def test_control_refusal_is_not_treated_as_transient_busy(monkeypatch, code, detail):
    store, opened, slept = busy_store(monkeypatch)

    def operation(_session, _owner):
        raise module("worker").ResearchWorkflowError(detail, status_code=code)

    with pytest.raises(module("worker").ResearchWorkflowError, match=detail):
        store.transaction(operation)
    assert len(opened) == 1
    assert slept == []


def test_control_busy_retry_is_bounded_and_cannot_extend_attempt_deadline(monkeypatch):
    worker = module("worker")
    store, _opened, slept = busy_store(monkeypatch)
    attempts = []

    def operation(_session, _owner):
        attempts.append(True)
        raise busy_error()

    with pytest.raises(worker.ResearchWorkflowError, match="fenced"):
        store.transaction(operation)
    assert len(attempts) == len(worker.CONTROL_RETRY_DELAYS) + 1
    assert len(slept) == len(worker.CONTROL_RETRY_DELAYS)
    attempts.clear()
    slept.clear()
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.0)
    with pytest.raises(TimeoutError, match="deadline"):
        store.transaction(operation, deadline=100.001)
    assert len(attempts) == 1
    assert slept == []


def test_claim_paused_before_launch_cannot_cross_completed_maintenance_scan():
    worker = module("worker")
    claimed, proceed = threading.Event(), threading.Event()
    launch_lock = threading.Lock()
    state = {"maintenance": False, "starts": 0}
    errors = []

    class Store:
        @contextmanager
        def launch_barrier(self, _job, *, deadline):
            with launch_lock:
                yield

        def heartbeat(self, _job, *, deadline):
            if state["maintenance"]:
                raise worker.ResearchWorkflowError("research attempt was fenced", status_code=409)

    class Wire:
        def __enter__(self):
            state["starts"] += 1
            return self

        def __exit__(self, *_args):
            state["starts"] -= 1

    guard = SimpleNamespace(deadline=99999999999.0, check=lambda: None)

    def claimant():
        claimed.set()
        assert proceed.wait(2)
        try:
            with worker.verified_sandbox(Store(), {}, Wire(), guard):
                errors.append("unexpected execution")
        except worker.ResearchWorkflowError as exc:
            errors.append(exc.detail)

    thread = threading.Thread(target=claimant)
    thread.start()
    assert claimed.wait(2)
    # Maintenance acquired the shared launch barrier, durably fenced, and
    # proved an empty scan while the already-claimed attempt was paused.
    with launch_lock:
        state["maintenance"] = True
        assert state["starts"] == 0
    proceed.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors == ["research attempt was fenced"]
    assert state["starts"] == 0


def test_cancelled_start_is_cleaned_before_launch_barrier_releases():
    worker, events = module("worker"), []

    class Store:
        @contextmanager
        def launch_barrier(self, _job, *, deadline):
            events.append("locked")
            try:
                yield
            finally:
                events.append("unlocked")

        def heartbeat(self, _job, *, deadline):
            if "verified" in events:
                raise worker.ResearchWorkflowError("cancelled during creation", status_code=409)
            events.append("validated")

    class Wire:
        def __enter__(self):
            events.append("verified")
            return self

        def __exit__(self, *_args):
            events.append("removed")

    guard = SimpleNamespace(deadline=99999999999.0, check=lambda: None)
    with (
        pytest.raises(worker.ResearchWorkflowError, match="cancelled"),
        worker.verified_sandbox(Store(), {}, Wire(), guard),
    ):
        pytest.fail("cancelled start must not execute")
    assert events == ["locked", "validated", "verified", "removed", "unlocked"]


@pytest.mark.parametrize("release_ok", [True, False])
def test_launch_session_lock_is_autocommit_and_uncertain_release_discards_connection(release_ok):
    worker, events = module("worker"), []

    class Connection:
        def execution_options(self, **options):
            events.append(options)
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            events.append("closed")

        def scalar(self, statement, arguments):
            assert arguments == {"key": worker.RESEARCH_LAUNCH_LOCK}
            if "pg_try_advisory_lock" in str(statement):
                events.append("acquired")
                return True
            events.append("release")
            return release_ok

        def invalidate(self):
            events.append("invalidated")

    factory = SimpleNamespace(kw={"bind": SimpleNamespace(connect=Connection)})
    store = worker.ResearchStore(factory, worker_id=JOB, image_hash=IMAGE, bundle_hash="b" * 64)
    if release_ok:
        with store.launch_barrier({}, deadline=99999999999.0):
            assert events[-1] == "acquired"
        assert events == [{"isolation_level": "AUTOCOMMIT"}, "acquired", "release", "closed"]
    else:
        with (
            pytest.raises(ValueError, match="release"),
            store.launch_barrier({}, deadline=99999999999.0),
        ):
            pass
        assert events[-2:] == ["invalidated", "closed"]


def test_successful_launch_unlocks_before_compute_and_keeps_cleanup_owner():
    worker, events = module("worker"), []

    class Store:
        @contextmanager
        def launch_barrier(self, _job, *, deadline):
            events.append("locked")
            try:
                yield
            finally:
                events.append("unlocked")

        def heartbeat(self, _job, *, deadline):
            events.append("validated")

    class Wire:
        def __enter__(self):
            events.append("created")
            return self

        def __exit__(self, *_args):
            events.append("removed")

    guard = SimpleNamespace(deadline=99999999999.0, check=lambda: None)
    with worker.verified_sandbox(Store(), {}, Wire(), guard):
        assert events[-1] == "unlocked"
        events.append("compute")
    assert events == [
        "locked",
        "validated",
        "created",
        "validated",
        "unlocked",
        "compute",
        "removed",
    ]


@pytest.mark.parametrize(
    ("options", "exit_code"),
    [
        (["name=seccomp,profile=default"], 0),
        (["name=apparmor"], 0),
        (["name=seccomp,profile=builtin"], 1),
        (["name=seccomp,profile=builtin", "name=seccomp,profile=custom"], 0),
    ],
)
def test_daemon_default_seccomp_must_be_builtin_before_any_launch(monkeypatch, options, exit_code):
    process = module("process")
    docker = process.DockerBackend()
    response = SimpleNamespace(
        returncode=exit_code, stdout=json.dumps(options).encode(), stderr=b""
    )
    monkeypatch.setattr(docker, "_run", lambda _arguments: response)

    def forbidden_start(*_args, **_kwargs):
        pytest.fail("An unverified daemon profile must not create a sandbox")

    monkeypatch.setattr(process.subprocess, "Popen", forbidden_start)
    with pytest.raises(ValueError, match="seccomp"):
        docker.start(["docker", "compose", "run", "research"])


def test_verified_builtin_daemon_seccomp_allows_declared_launch(monkeypatch):
    process = module("process")
    docker, calls = process.DockerBackend(), []

    def inspect(arguments):
        calls.append(arguments)
        return SimpleNamespace(
            returncode=0, stdout=b'["name=seccomp,profile=builtin","name=cgroupns"]', stderr=b""
        )

    monkeypatch.setattr(docker, "_run", inspect)
    child = object()
    monkeypatch.setattr(process.subprocess, "Popen", lambda *_args, **_kwargs: child)
    assert docker.start(["docker", "compose", "run", "research"]) is child
    assert calls == [["info", "--format", "{{json .SecurityOptions}}"]]
