"""Maintenance fences durable admission before exact sandbox cleanup and snapshots."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from dev_cli.core.research.sandbox import LABEL_GENERATION, LABEL_JOB

JOB = str(uuid4())
GENERATION = str(uuid4())
IMAGE = "sha256:" + "a" * 64
BUNDLE = "b" * 64
CONTAINER = "c" * 64


class Store:
    def __init__(self, *, maintenance: bool = False) -> None:
        self.events: list[str] = []
        self.state: dict[str, Any] | None = {"generation": GENERATION, "maintenance": maintenance}
        self.interrupted = False
        self.held = False

    def begin(self) -> dict[str, Any] | None:
        self.events.append("lock_and_fence")
        self.held = True
        old = dict(self.state) if self.state else None
        if self.state:
            self.state["maintenance"] = True
        return old

    def lookup(self, job_id: str, generation: str, image: str) -> dict[str, Any] | None:
        if (job_id, generation, image) != (JOB, GENERATION, IMAGE):
            return None
        return {"job_id": JOB, "generation": GENERATION, "image_hash": IMAGE, "bundle_hash": BUNDLE}

    def interrupt_running(self) -> None:
        self.events.append("interrupt")
        self.interrupted = True

    def refence(self) -> None:
        self.events.append("refence")
        if self.state:
            self.state["maintenance"] = True

    def complete(self) -> None:
        self.events.append("rotate_resume")
        if self.state:
            self.state.update(generation=str(uuid4()), maintenance=False)

    def close(self) -> None:
        self.events.append("unlock")
        self.held = False


class Docker:
    def __init__(self, *, unknown: bool = False, fail: bool = False) -> None:
        self.rows = [
            {
                "Id": CONTAINER,
                "Name": "/vynmatrix-research-" + UUID(JOB).hex,
                "Image": IMAGE,
                "Config": {
                    "Labels": {
                        LABEL_JOB: str(uuid4()) if unknown else JOB,
                        LABEL_GENERATION: GENERATION,
                        "com.docker.compose.project": "vynmatrix",
                        "com.docker.compose.service": "research",
                    }
                },
            }
        ]
        self.fail = fail
        self.removed: list[str] = []

    def running(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def remove(self, identity: str) -> None:
        if self.fail:
            raise ValueError("exact removal could not be proved")
        self.removed.append(identity)
        self.rows = []


def guard(store: Store, docker: Docker) -> Any:
    from dev_cli.core.research.maintenance import ResearchMaintenance

    return ResearchMaintenance(store=store, docker=docker, drain_seconds=0)


def test_fence_and_exact_drain_precede_retained_interruption_then_resume() -> None:
    store, docker = Store(), Docker()
    operation = guard(store, docker)
    operation.begin()
    assert store.events == ["lock_and_fence", "interrupt"]
    assert store.held
    assert docker.removed == [CONTAINER]
    assert store.interrupted
    old_generation = store.state["generation"] if store.state else None
    operation.complete()
    assert store.state
    assert store.state["generation"] != old_generation
    assert store.events[-2:] == ["rotate_resume", "unlock"]


@pytest.mark.parametrize(("unknown", "fail"), [(True, False), (False, True)])
def test_unprovable_drain_retains_fence_and_releases_session_lock(
    unknown: bool, fail: bool
) -> None:
    store, docker = Store(), Docker(unknown=unknown, fail=fail)
    with pytest.raises(ValueError, match=r"Unrecognized|removal"):
        guard(store, docker).begin()
    assert store.state
    assert store.state["maintenance"] is True
    assert store.held is False
    assert not store.interrupted


def test_restore_refences_rewound_control_before_unlock_and_never_resumes_on_abort() -> None:
    store, docker = Store(), Docker()
    operation = guard(store, docker)
    operation.begin()
    store.state = {"generation": str(uuid4()), "maintenance": False}
    operation.after_restore()
    assert store.held
    assert store.state["maintenance"] is True
    operation.abort()
    assert store.state["maintenance"] is True
    assert not store.held
    assert "rotate_resume" not in store.events


def test_missing_migration_authority_is_not_a_legacy_bypass() -> None:
    from dev_cli.core.research.maintenance import MaintenanceStore

    with pytest.raises(ValueError, match="migration database URL"):
        MaintenanceStore({})


def test_legacy_control_absence_releases_lock_without_fabricating_generation() -> None:
    store, docker = Store(), Docker()
    store.state = None
    operation = guard(store, docker)
    operation.begin()
    operation.complete()
    assert store.state is None
    assert not store.held
    assert not store.interrupted
    assert docker.removed == []


def test_session_barrier_orders_launch_before_control_and_releases_reverse(monkeypatch) -> None:
    from dev_cli.core.research.maintenance import MaintenanceStore
    from lib_application.db import session as sessions

    events: list[tuple[str, str]] = []

    class Connection:
        def execute(self, statement, parameters=None):
            sql = str(statement)
            if parameters:
                events.append(("release" if "unlock" in sql else "acquire", parameters["key"]))

        def commit(self):
            pass

        def in_transaction(self):
            return False

        def close(self):
            pass

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(sessions, "create_engine_for_env", lambda **_kwargs: Engine())
    monkeypatch.setattr(sessions, "dispose_engine", lambda _engine: None)
    store = MaintenanceStore(
        {"MIGRATION_DATABASE_URL": "postgresql://owner:unit@127.0.0.1/isolated"}
    )
    assert store._open()
    store.close()
    assert events == [
        ("acquire", "vynmatrix.research.launch"),
        ("acquire", "vynmatrix.research.control"),
        ("release", "vynmatrix.research.control"),
        ("release", "vynmatrix.research.launch"),
    ]


def test_maintenance_drains_only_its_project_despite_external_research_labels(monkeypatch) -> None:
    from dev_cli.core.research.process import DockerBackend

    own_sandbox = Docker().rows[0]
    base = [
        {
            "Id": str(index) * 64,
            "Image": IMAGE,
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "vynmatrix",
                    "com.docker.compose.service": service,
                }
            },
        }
        for index, service in enumerate(("postgres", "application"))
    ]
    foreign = [
        {
            "Id": str(index) * 64,
            "Image": IMAGE,
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "another-project",
                    "com.docker.compose.service": "research",
                    LABEL_JOB: JOB,
                    LABEL_GENERATION: GENERATION,
                }
            },
        }
        for index in range(2, 6)
    ]
    rows, removed = [*base, own_sandbox, *foreign], []
    docker = DockerBackend()

    def run(arguments):
        assert arguments[0] == "ps", "Only mocked read-only inventory is authorized"
        selected = rows
        if "label=com.docker.compose.project=vynmatrix" in arguments:
            selected = [
                row
                for row in rows
                if row["Config"]["Labels"]["com.docker.compose.project"] == "vynmatrix"
            ]
        return SimpleNamespace(
            returncode=0, stdout="\n".join(row["Id"] for row in selected).encode(), stderr=b""
        )

    def remove(identity):
        removed.append(identity)
        rows[:] = [row for row in rows if row["Id"] != identity]

    monkeypatch.setattr(docker, "_run", run)
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda identity: next((row for row in rows if row["Id"] == identity), None),
    )
    monkeypatch.setattr(docker, "remove", remove)
    store = Store()
    guard(store, docker).begin()
    assert removed == [CONTAINER]
    assert all(row in rows for row in foreign)
    assert store.interrupted
