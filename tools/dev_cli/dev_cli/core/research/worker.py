"""Lease-fenced host supervisor, executed exclusively from a verified bundle."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from dev_cli.core.deployment.artefact import PLATFORM_REPOSITORY, image_identity
from dev_cli.core.deployment.environment import load_env_file, resolve_host_url
from dev_cli.core.runtime_roles import _catalog, plan_runtime_roles
from lib_application.db.models.research_jobs import ResearchJobAttempt
from lib_application.db.session import dispose_engine, get_session_factory
from lib_application.services import research_jobs
from lib_application.services.account_onboarding import owner_scope
from lib_application.services.research_admission import CONTROL_BUSY_DETAIL, RESEARCH_LAUNCH_LOCK
from lib_application.services.research_authoring import (
    ResearchWorkflowError,
    content_hash,
    get_revision,
)
from lib_application.services.research_data import get_dataset
from lib_application.services.research_registry import get_model

from .backtest import MAX_WARMUP, compatible_model, model_rows, run_backtest, strategy_source
from .bundle import verify_bundle
from .process import DockerBackend, SandboxProcess, encode_message, exact_identity
from .sandbox import LABEL_GENERATION, LABEL_JOB, WALL_SECONDS, SandboxSpec

RESULT_BYTES = 16 * 1024 * 1024
CHUNK_ROWS = 100
BASE_CONTAINER_COUNT = 2
CONTROL_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0, 1.0)
CONTROL_PROBE_MILLISECONDS = 250
CONTROL_BUSY_STATUS = 503
TransactionResult = TypeVar("TransactionResult")


def read_result(wire: Any) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    consumed = 0
    while True:
        message = wire.receive()
        consumed += len(json.dumps(message, allow_nan=False, separators=(",", ":")).encode())
        if consumed > RESULT_BYTES:
            msg = "Research result exceeds 16 MiB"
            raise ValueError(msg)
        if message.get("kind") == "part":
            if (
                set(message) != {"kind", "field", "rows"}
                or message["field"] != "predictions"
                or not isinstance(message["rows"], list)
                or len(message["rows"]) > CHUNK_ROWS
                or not all(isinstance(row, dict) for row in message["rows"])
            ):
                msg = "Research result part schema invalid"
                raise ValueError(msg)
            rows.extend(message["rows"])
        elif message.get("kind") == "result":
            if set(message) != {"kind", "value"} or not isinstance(message["value"], dict):
                msg = "Research result schema invalid"
                raise ValueError(msg)
            value = message["value"]
            if rows:
                if value.get("predictions") != []:
                    msg = "Research prediction result schema invalid"
                    raise ValueError(msg)
                value["predictions"] = rows
            if (
                len(json.dumps(value, allow_nan=False, separators=(",", ":")).encode())
                > RESULT_BYTES
            ):
                msg = "Research result exceeds 16 MiB"
                raise ValueError(msg)
            return dict(value)
        else:
            msg = "Research result schema invalid"
            raise ValueError(msg)


def _row_chunks(wire: Any, rows: list[dict[str, Any]]) -> None:
    chunk: list[dict[str, Any]] = []
    for row in rows:
        candidate = [*chunk, row]
        try:
            encode_message({"kind": "rows", "bars": candidate})
        except ValueError:
            if not chunk:
                raise
            wire.send({"kind": "rows", "bars": chunk})
            chunk = [row]
        else:
            chunk = candidate
        if len(chunk) == CHUNK_ROWS:
            wire.send({"kind": "rows", "bars": chunk})
            chunk = []
    if chunk:
        wire.send({"kind": "rows", "bars": chunk})


def execute_job(wire: Any, job: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    from lib_application.services.research_holdout import (  # noqa: PLC0415
        evaluation_holdout_evidence,
    )

    kind, settings = job["kind"], job["inputs"]
    if kind == "backtest":
        result = run_backtest(
            wire,
            inputs["dataset"],
            inputs["revision"],
            settings["assumptions"],
            model=inputs.get("model"),
            evaluation_start=settings.get("evaluation_start"),
        )
        if inputs.get("model") is not None:
            model = inputs["model"]
            result.setdefault("validation", {})["holdout_reuse"] = evaluation_holdout_evidence(
                model,
                inputs["dataset"]["metadata"],
                [row["bar_close"] for row in inputs["dataset"]["bars"]],
                evaluation_start=settings.get("evaluation_start") or model["training_cutoff"],
            )
        return result
    if kind == "validate_strategy":
        strategy_source(wire, inputs["revision"], mode=kind)
    elif kind in {"train", "predict"}:
        dataset = inputs["dataset"]
        rows = model_rows(dataset)
        metadata = dataset["metadata"]
        initial: dict[str, Any] = {"kind": "init", "mode": kind}
        if kind == "train":
            initial.update(
                horizon=settings["horizon"],
                identity={
                    **{key: metadata[key] for key in ("instrument_id", "source", "timeframe")},
                    "dataset_hash": dataset["content_hash"],
                    "image_hash": job["image_hash"],
                },
            )
        else:
            model, start = compatible_model(
                inputs["model"], dataset, settings.get("evaluation_start")
            )
            initial.update(model=model, evaluation_start=start.isoformat())
        wire.send(initial)
        _row_chunks(wire, rows)
        wire.send({"kind": "compute"})
    else:
        msg = "Unsupported research job kind"
        raise ValueError(msg)
    result = read_result(wire)
    wire.finish()
    if kind == "validate_strategy" and (
        result.get("valid") is not True
        or type(result.get("warmup_bars")) is not int
        or not 0 <= result["warmup_bars"] <= MAX_WARMUP
    ):
        msg = "Isolated strategy validation result invalid"
        raise ValueError(msg)
    if kind == "predict":
        validation = result.setdefault("validation", {})
        if not isinstance(validation, dict):
            msg = "Isolated prediction validation result invalid"
            raise ValueError(msg)
        validation["holdout_reuse"] = evaluation_holdout_evidence(
            inputs["model"],
            inputs["dataset"]["metadata"],
            [row["ts"] for row in result["predictions"]],
        )
    return result


class ResearchStore:
    """Every operation opens and closes its own transaction before compute."""

    def __init__(self, factory: Any, *, worker_id: str, image_hash: str, bundle_hash: str) -> None:
        self.factory, self.worker_id = factory, worker_id
        self.image_hash, self.bundle_hash = image_hash, bundle_hash
        self.generation = ""
        self._identity_claimed = False

    @staticmethod
    def _deadline(deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            msg = "Research attempt deadline expired during control admission"
            raise TimeoutError(msg)

    def _transaction_once(
        self, operation: Callable[[Any, str], TransactionResult], *, deadline: float | None = None
    ) -> TransactionResult:
        with self.factory() as session, session.begin(), owner_scope(session) as owner_id:
            self._deadline(deadline)
            result = operation(session, owner_id)
            self._deadline(deadline)
            return result

    def _control_retry_allowed(self, deadline: float | None) -> bool:
        if not self.generation:
            return False

        def probe(session: Any, _owner: str) -> bool:
            if session.get_bind().dialect.name == "postgresql":
                remaining = (
                    CONTROL_PROBE_MILLISECONDS
                    if deadline is None
                    else min(
                        CONTROL_PROBE_MILLISECONDS,
                        max(1, int((deadline - time.monotonic()) * 1000)),
                    )
                )
                # A SELECT can itself wait behind maintenance DDL. Bound that
                # status lookup without taking the contested advisory lock.
                session.execute(
                    text(
                        "SELECT set_config('statement_timeout', :timeout, true), "
                        "set_config('lock_timeout', :timeout, true)"
                    ),
                    {"timeout": f"{remaining}ms"},
                )
            control = research_jobs.get_research_control(session, lock=False)
            identity = (control.worker_id, control.image_hash, control.bundle_hash)
            expected = (self.worker_id, self.image_hash, self.bundle_hash)
            return (
                not control.maintenance
                and control.generation == self.generation
                and (
                    identity == expected
                    or (not self._identity_claimed and identity == (None, None, None))
                )
            )

        try:
            return self._transaction_once(probe, deadline=deadline)
        except SQLAlchemyError:
            # A timed-out/unavailable status probe cannot authorize another try.
            return False

    def transaction(
        self, operation: Callable[[Any, str], TransactionResult], *, deadline: float | None = None
    ) -> TransactionResult:
        for attempt in range(len(CONTROL_RETRY_DELAYS) + 1):
            try:
                return self._transaction_once(operation, deadline=deadline)
            except ResearchWorkflowError as exc:
                if (
                    exc.status_code != CONTROL_BUSY_STATUS
                    or exc.detail != CONTROL_BUSY_DETAIL
                    or attempt == len(CONTROL_RETRY_DELAYS)
                    or not self._control_retry_allowed(deadline)
                ):
                    raise
                pause = CONTROL_RETRY_DELAYS[attempt]
                if deadline is not None and time.monotonic() + pause >= deadline:
                    msg = "Research attempt deadline prevents another control retry"
                    raise TimeoutError(msg) from exc
                time.sleep(pause)
        msg = "Research control retry budget exhausted"
        raise RuntimeError(msg)

    def start(self) -> None:
        def start(session: Any, _owner: str) -> None:
            status = research_jobs.supervisor_status(session)
            self.generation = status["generation"]
            self._pulse(session)

        self.transaction(start)
        self._identity_claimed = True

    def _pulse(self, session: Any) -> None:
        research_jobs.supervisor_heartbeat(
            session,
            worker_id=self.worker_id,
            generation=self.generation,
            image_hash=self.image_hash,
            bundle_hash=self.bundle_hash,
        )

    def pulse(self) -> None:
        self.transaction(lambda session, _owner: self._pulse(session))

    def claim(self) -> dict[str, Any] | None:
        return self.transaction(
            lambda session, _owner: research_jobs.claim_job(
                session,
                worker_id=self.worker_id,
                generation=self.generation,
                image_hash=self.image_hash,
                bundle_hash=self.bundle_hash,
            )
        )

    @staticmethod
    def fence(job: dict[str, Any]) -> dict[str, Any]:
        return {
            "attempt_id": job["attempt_id"],
            "token": job["claim_token"],
            "generation": job["generation"],
        }

    def heartbeat(self, job: dict[str, Any], *, deadline: float | None = None) -> None:
        def operation(session: Any, owner_id: str) -> None:
            if owner_id != job["owner_id"]:
                msg = "Research job owner no longer matches the designated owner"
                raise ValueError(msg)
            self._pulse(session)
            research_jobs.heartbeat_job(session, owner_id, job["job_id"], **self.fence(job))

        self.transaction(operation, deadline=deadline)

    def finish(self, job: dict[str, Any], *, deadline: float | None = None, **payload: Any) -> None:
        self.transaction(
            lambda session, owner: research_jobs.finish_job(
                session, owner, job["job_id"], **self.fence(job), **payload
            ),
            deadline=deadline,
        )

    @contextlib.contextmanager
    def launch_barrier(self, _job: dict[str, Any], *, deadline: float) -> Iterator[None]:
        """Serialize creation with maintenance without holding a DB transaction.

        Maintenance takes this session lock before its control lock. Heartbeats
        continue on their own connections and acquire only the control lock.
        """
        engine = self.factory.kw["bind"]
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            acquired = False
            try:
                while not acquired:
                    self._deadline(deadline)
                    acquired = (
                        connection.scalar(
                            text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                            {"key": RESEARCH_LAUNCH_LOCK},
                        )
                        is True
                    )
                    if not acquired:
                        if not self._control_retry_allowed(deadline):
                            msg = "Research launch was fenced by maintenance or changed authority"
                            raise ResearchWorkflowError(msg, status_code=409)
                        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                yield
            finally:
                if acquired:
                    released = False
                    try:
                        released = (
                            connection.scalar(
                                text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                                {"key": RESEARCH_LAUNCH_LOCK},
                            )
                            is True
                        )
                    finally:
                        if not released:
                            # Never return an uncertain session lock to the pool.
                            connection.invalidate()
                    if not released:
                        msg = "Research launch barrier release could not be verified"
                        raise ValueError(msg)

    def interrupt(self, job: dict[str, Any]) -> None:
        self.transaction(
            lambda session, owner: research_jobs.interrupt_job(
                session,
                owner,
                job["job_id"],
                **self.fence(job),
                detail="Host research supervisor stopped",
            )
        )

    def retire(self) -> None:
        self.transaction(
            lambda session, _owner: research_jobs.retire_supervisor(
                session, worker_id=self.worker_id, generation=self.generation
            )
        )

    def load(self, job: dict[str, Any]) -> dict[str, Any]:
        def operation(session: Any, owner_id: str) -> dict[str, Any]:
            if owner_id != job["owner_id"] or content_hash(job["inputs"]) != job["inputs_hash"]:
                msg = "Frozen job owner/input identity changed"
                raise ValueError(msg)
            settings, lineage = job["inputs"], job["lineage"]
            result: dict[str, Any] = {}
            if settings.get("dataset_id"):
                dataset = get_dataset(session, owner_id, settings["dataset_id"], include_bars=True)
                expected = lineage["dataset"]
                if (
                    dataset["dataset_id"] != expected["dataset_id"]
                    or dataset["content_hash"] != expected["content_hash"]
                    or dataset["original_hash"] != expected["original_hash"]
                    or content_hash(dataset["manifest"]) != expected["manifest_hash"]
                ):
                    msg = "Frozen dataset lineage changed"
                    raise ValueError(msg)
                result["dataset"] = dataset
            if settings.get("revision_id"):
                row = get_revision(session, owner_id, settings["revision_id"])
                expected = lineage["revision"]
                if (
                    hashlib.sha256(row.source.encode()).hexdigest() != expected["source_hash"]
                    or content_hash({"source": row.source, "config": row.config_json})
                    != expected["content_hash"]
                ):
                    msg = "Frozen strategy revision identity changed"
                    raise ValueError(msg)
                result["revision"] = {
                    "revision_id": row.revision_id,
                    "source": row.source,
                    "config": json.loads(json.dumps(row.config_json, allow_nan=False)),
                }
            if settings.get("model_id"):
                model = get_model(session, owner_id, settings["model_id"])
                if model["artifact_hash"] != lineage["model"]["artifact_hash"]:
                    msg = "Frozen model lineage changed"
                    raise ValueError(msg)
                result["model"] = model["artifact"]
            return result

        return dict(self.transaction(operation))

    def reconcile(self, docker: DockerBackend) -> None:
        self.transaction(lambda session, _owner: research_jobs.reconcile_expired_attempts(session))
        for actual in docker.sandboxes():
            labels = actual.get("Config", {}).get("Labels") or {}
            job_id, generation = labels.get(LABEL_JOB), labels.get(LABEL_GENERATION)
            if job_id is None:
                continue
            if not isinstance(job_id, str):
                msg = "Research sandbox job label has invalid identity"
                raise TypeError(msg)
            container_job_id = job_id

            def lookup(
                session: Any,
                _owner: str,
                *,
                job_id: str = container_job_id,
                generation: str | None = generation,
                image: str = actual["Image"],
            ) -> dict[str, Any]:
                row = session.scalar(
                    select(ResearchJobAttempt)
                    .where(
                        ResearchJobAttempt.job_id == job_id,
                        ResearchJobAttempt.generation == generation,
                        ResearchJobAttempt.image_hash == image,
                    )
                    .order_by(ResearchJobAttempt.attempt.desc())
                    .limit(1)
                )
                if row is None:
                    msg = "Unrecognized labelled research sandbox blocks new claims"
                    raise ValueError(msg)
                if row.status == "running":
                    msg = "Another running research attempt blocks new claims"
                    raise ValueError(msg)
                return {"bundle_hash": row.bundle_hash, "generation": row.generation}

            attempt = self.transaction(lookup)
            spec = SandboxSpec(
                job_id, attempt["generation"], actual["Image"], attempt["bundle_hash"]
            )
            docker.remove(exact_identity(actual, spec))


class AttemptGuard:
    """Heartbeat in fresh transactions even while trusted replay is computing."""

    def __init__(self, store: ResearchStore, job: dict[str, Any]) -> None:
        self.store, self.job = store, job
        self.stopped = threading.Event()
        self.error: Exception | None = None
        self.deadline = time.monotonic() + WALL_SECONDS
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stopped.wait(1):
            try:
                self.store.heartbeat(self.job, deadline=self.deadline)
            except (ResearchWorkflowError, SQLAlchemyError, ValueError, OSError) as exc:
                self.error = exc
                return

    def check(self) -> None:
        if self.error is not None:
            msg = "Research attempt was cancelled or fenced"
            raise ValueError(msg) from self.error
        if time.monotonic() >= self.deadline:
            msg = "Research attempt exceeded its five-minute wall limit"
            raise TimeoutError(msg)

    def __enter__(self) -> AttemptGuard:
        self.store.heartbeat(self.job, deadline=self.deadline)
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.stopped.set()
        self.thread.join(timeout=12)
        if self.thread.is_alive():
            msg = "Research heartbeat did not drain"
            raise ValueError(msg)


@contextlib.contextmanager
def verified_sandbox(
    store: ResearchStore, job: dict[str, Any], wire: Any, guard: AttemptGuard
) -> Iterator[Any]:
    """A claimed attempt cannot launch across a completed maintenance scan."""
    with contextlib.ExitStack() as running:
        with store.launch_barrier(job, deadline=guard.deadline):
            store.heartbeat(job, deadline=guard.deadline)
            guard.check()
            with contextlib.ExitStack() as starting:
                active = starting.enter_context(wire)
                # Owner cancellation may happen while Docker creates/inspects.
                # A refused start is removed before releasing the launch lock.
                store.heartbeat(job, deadline=guard.deadline)
                guard.check()
                running.enter_context(starting.pop_all())
        yield active


def verify_capacity(
    docker: DockerBackend, image_hash: str, *, sandbox_id: str | None = None
) -> None:
    rows = docker.running()
    if sandbox_id is not None:
        if sum(row.get("Id") == sandbox_id for row in rows) != 1:
            msg = "Verified research sandbox is absent from actual running capacity"
            raise ValueError(msg)
        rows = [row for row in rows if row.get("Id") != sandbox_id]
    if len(rows) != BASE_CONTAINER_COUNT:
        msg = "Research requires PostgreSQL and one combined application container"
        raise ValueError(msg)
    services = {}
    for actual in rows:
        config = actual.get("Config", {})
        labels = config.get("Labels") or {}
        if labels.get("com.docker.compose.project") != "vynmatrix":
            msg = "An unrelated running container occupies research capacity"
            raise ValueError(msg)
        services[labels.get("com.docker.compose.service")] = actual
    if (
        set(services) != {"postgres", "application"}
        or services["application"].get("Image") != image_hash
    ):
        msg = "Research requires the same stamped image and combined application layout"
        raise ValueError(msg)
    config = services["application"].get("Config", {})
    env = dict(item.split("=", 1) for item in config.get("Env", []) if "=" in item)
    command = config.get("Cmd") or []
    if env.get("PLATFORM_APPLICATION_GROUP") != "all" or not command or command[-1] != "all":
        msg = "Research requires the supported combined application/worker mode"
        raise ValueError(msg)


def save_diagnostics(root: Path, job: dict[str, Any], value: bytes) -> None:
    """Retain bounded stderr privately; never include it in credential-bearing logs."""
    attempt_id = job["attempt_id"]
    if str(UUID(attempt_id)) != attempt_id or len(value) > 1024 * 1024:
        msg = "Diagnostic artifact identity/size invalid"
        raise ValueError(msg)
    directory = root / ".artifacts/research/attempts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        directory / (attempt_id + ".stderr"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)


def run_worker(
    store: ResearchStore,
    root: Path,
    bundle: Path,
    bundle_hash: str,
    *,
    once: bool = False,
    docker: DockerBackend | None = None,
) -> int:
    docker = docker if docker is not None else DockerBackend()
    store.start()
    try:
        while True:
            verify_bundle(bundle, bundle_hash)
            store.pulse()
            store.reconcile(docker)
            verify_capacity(docker, store.image_hash)
            job = store.claim()
            if job is not None:
                try:
                    with AttemptGuard(store, job) as guard:
                        inputs = store.load(job)
                        spec = SandboxSpec(
                            job["job_id"], job["generation"], job["image_hash"], job["bundle_hash"]
                        )
                        wire = SandboxProcess(
                            root,
                            spec,
                            docker=docker,
                            poll=guard.check,
                            admit=lambda container_id: verify_capacity(
                                docker, store.image_hash, sandbox_id=container_id
                            ),
                        )
                        try:
                            with verified_sandbox(store, job, wire, guard) as active:
                                result = execute_job(active, job, inputs)
                        finally:
                            save_diagnostics(root, job, bytes(wire.diagnostics))
                        guard.check()
                        result["lineage"] = {
                            **job["lineage"],
                            "image_hash": job["image_hash"],
                            "bundle_hash": job["bundle_hash"],
                            "generation": job["generation"],
                            "attempt_id": job["attempt_id"],
                        }
                        store.finish(job, result=result, deadline=guard.deadline)
                except KeyboardInterrupt:
                    store.interrupt(job)
                    raise
                except (ValueError, TypeError, KeyError, TimeoutError, OSError) as exc:
                    # Cancellation/maintenance already owns its terminal state.
                    with contextlib.suppress(ResearchWorkflowError):
                        store.finish(
                            job, error={"code": "research_refused", "detail": str(exc)[:500]}
                        )
            if once:
                return 0
            time.sleep(1)
    finally:
        store.retire()


def _configuration(root: Path) -> tuple[str, str]:
    env = {**load_env_file(root / ".env"), **os.environ}
    url = (env.get("RESEARCH_DATABASE_URL") or "").strip()
    if not url:
        msg = "RESEARCH_DATABASE_URL is required; run vmdev init --update"
        raise ValueError(msg)
    tag = env.get("VM_DEPLOY_IMAGE_TAG", "")
    match = re.fullmatch(r"sha-([0-9a-f]{12})(-dirty)?", tag)
    if match is None:
        msg = "Research requires the declared commit-stamped platform image"
        raise ValueError(msg)
    image = image_identity(f"{PLATFORM_REPOSITORY}:{tag}")
    if (
        image is None
        or not image["source_commit"].startswith(match.group(1))
        or image["source_dirty"] != ("true" if match.group(2) else "false")
    ):
        msg = "Declared research image identity/stamp verification failed"
        raise ValueError(msg)
    return resolve_host_url(url, env), image["image_id"]


def verify_database_login(session: Any) -> None:
    """Use the existing installed-authority contract, including membership drift."""
    current = session.scalar(text("SELECT current_user"))
    plan = plan_runtime_roles(_catalog(session), rotate=False)
    if current != "vm_research_login" or current not in plan.authenticate:
        msg = "Research supervisor requires its dedicated least-privilege database login"
        raise ValueError(msg)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--bundle-hash", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    verify_bundle(args.bundle, args.bundle_hash)
    if not Path(__file__).resolve().is_relative_to(args.bundle.resolve()):
        msg = "Research worker must execute from its verified frozen bundle"
        raise ValueError(msg)
    url, image = _configuration(args.root)
    factory = get_session_factory(db_url=url)
    try:
        with factory() as session:
            verify_database_login(session)

        def interrupted(_signal: int, _frame: Any) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
        store = ResearchStore(
            factory, worker_id=str(uuid4()), image_hash=image, bundle_hash=args.bundle_hash
        )
        return run_worker(store, args.root, args.bundle, args.bundle_hash, once=args.once)
    finally:
        dispose_engine(factory.kw["bind"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (ValueError, OSError, SQLAlchemyError):
        # Database exceptions can contain connection URLs and parameters.
        sys.stderr.write(
            "Research supervisor refused to run; "
            "check its isolated role, generation, image and capacity.\n"
        )
        sys.exit(1)
