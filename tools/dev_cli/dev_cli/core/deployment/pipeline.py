"""One idempotent command for a fresh install and for an upgrade.

The stages that already work -- provisioning, migration, roles, catalogue and
owner adoption -- are reused exactly as ``vmdev db bootstrap`` runs them. What
is added around them is the part that was missing: a snapshot taken before
anything destructive, a record of what is being installed, an immutable tag for
Compose to point at, and a rollback that puts the previous generation back when
a stage after the runtime stops fails.
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dev_cli.core.database_lifecycle import PlatformLifecycle
from dev_cli.core.deployment import artefact, checks, configuration, environment, record

SNAPSHOT_DIRECTORY = Path(".artifacts") / "deployments"
SNAPSHOT_RETENTION = 5
_SNAPSHOT_MAGIC = b"PGDMP"
_HEALTH_TIMEOUT_SECONDS = 15
_HTTP_OK = 200

#: Ordered stages. Everything from ``stop`` onwards has already touched the
#: installation, so a failure there is what rollback exists for.
STAGES: tuple[str, ...] = (
    "preflight",
    "build",
    "snapshot",
    "record",
    "stop",
    "provision",
    "account",
    "start",
    "verify",
)
_FIRST_DESTRUCTIVE_STAGE = "stop"

NO_SNAPSHOT_FRESH = "fresh install: there was no database to snapshot"
NO_SNAPSHOT_REQUESTED = "explicitly skipped with --skip-snapshot"


class DeploymentError(RuntimeError):
    """A deployment stage failed; the message names the stage and what was done."""


def rollback_decision(stage: str, *, mode: str, snapshot: Path | None) -> str:
    """Decide what a failure in ``stage`` justifies, before touching anything.

    ``none``   -- nothing has changed yet, so there is nothing to undo.
    ``restore``-- put the database and the image generation back.
    ``refuse`` -- the installation was changed but no verified snapshot exists;
                  stopping with the database untouched beats a bad restore.
    """
    if stage not in STAGES:
        msg = f"Unknown deployment stage: {stage}"
        raise ValueError(msg)
    if mode != record.UPGRADE:
        return "none"
    if STAGES.index(stage) < STAGES.index(_FIRST_DESTRUCTIVE_STAGE):
        return "none"
    return "restore" if snapshot_is_restorable(snapshot) else "refuse"


def snapshot_is_restorable(snapshot: Path | None) -> bool:
    """A snapshot counts only when it exists and is a PostgreSQL custom archive."""
    if snapshot is None:
        return False
    try:
        with snapshot.open("rb") as stream:
            return stream.read(len(_SNAPSHOT_MAGIC)) == _SNAPSHOT_MAGIC
    except OSError:
        return False


def prune_snapshots(directory: Path, *, retain: int = SNAPSHOT_RETENTION) -> list[Path]:
    """Keep the newest ``retain`` snapshots; deploy frequency must not fill the disk."""
    if not directory.is_dir():
        return []
    archives = sorted(
        (path for path in directory.glob("*.dump") if path.is_file()),
        key=lambda path: path.name,
        reverse=True,
    )
    removed = []
    for path in archives[retain:]:
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


@dataclass
class DeploymentPlan:
    """What ``deploy`` would do, printed by ``--plan`` and asserted by the tests."""

    mode: str
    image_tag: str
    source_commit: str
    source_dirty: bool
    image_present: bool
    rebuild: bool
    target_head: str
    current_revision: str | None
    pending_revisions: list[str]
    deployed: dict[str, Any] | None
    configuration_keys: dict[str, list[str]]
    snapshot_path: str | None
    snapshot_reason: str | None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "image_tag": self.image_tag,
            "source_commit": self.source_commit,
            "source_dirty": self.source_dirty,
            "image_present": self.image_present,
            "rebuild": self.rebuild,
            "target_head": self.target_head,
            "current_revision": self.current_revision,
            "pending_revisions": self.pending_revisions,
            "deployed": self.deployed,
            "configuration_keys": self.configuration_keys,
            "snapshot_path": self.snapshot_path,
            "snapshot_reason": self.snapshot_reason,
            "warnings": self.warnings,
        }


class Deployer:
    """Install or upgrade one local stack, from one already-loaded configuration."""

    def __init__(
        self,
        root: Path,
        env: Mapping[str, str],
        *,
        echo: Callable[[str], None],
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.root = root
        self.run = run
        self.echo = echo
        self.source = artefact.source_state(root, run=run)
        self.env = {**env, artefact.IMAGE_TAG_KEY: self.source.tag}
        self.host_env = environment.resolve_host_env(self.env)
        self.previous_tag = (env.get(artefact.IMAGE_TAG_KEY) or "").strip()
        self.lifecycle = PlatformLifecycle(root, self.env, run=run)
        self.snapshot_directory = root / SNAPSHOT_DIRECTORY

    # -- reading the installation -------------------------------------------------

    def _settings(self) -> Any:
        from dev_cli.core.bootstrap import BootstrapSettings  # noqa: PLC0415

        return BootstrapSettings.parse(self.host_env)

    def target_head(self) -> str:
        """The Alembic head this checkout would migrate to."""
        from alembic.config import Config  # noqa: PLC0415
        from alembic.script import ScriptDirectory  # noqa: PLC0415

        config = Config(str(self.root / "scripts/db/alembic.ini"))
        config.set_main_option("script_location", str(self.root / "scripts/db/alembic"))
        head = ScriptDirectory.from_config(config).get_current_head()
        if not isinstance(head, str):
            msg = "The migration history has no single head"
            raise DeploymentError(msg)
        return head

    def pending_revisions(self, current: str | None) -> list[str]:
        """Revisions between the installed one and this checkout's head, oldest first."""
        from alembic.config import Config  # noqa: PLC0415
        from alembic.script import ScriptDirectory  # noqa: PLC0415

        config = Config(str(self.root / "scripts/db/alembic.ini"))
        config.set_main_option("script_location", str(self.root / "scripts/db/alembic"))
        scripts = ScriptDirectory.from_config(config)
        revisions = [item.revision for item in scripts.iterate_revisions("head", current or "base")]
        return list(reversed(revisions))

    def database_present(self) -> bool:
        """Whether the explicit target database exists on the configured server."""
        from sqlalchemy import text  # noqa: PLC0415

        from lib_application.db.session import (  # noqa: PLC0415
            create_engine_for_env,
            dispose_engine,
        )

        settings = self._settings()
        engine = create_engine_for_env(
            env="dev", db_url=settings.admin.render_as_string(hide_password=False)
        )
        try:
            with engine.connect() as connection:
                return bool(
                    connection.scalar(
                        text("SELECT 1 FROM pg_catalog.pg_database WHERE datname = :name"),
                        {"name": str(settings.migration.database)},
                    )
                )
        finally:
            dispose_engine(engine)

    def installed_revision(self) -> str | None:
        """The Alembic revision the target database is on, or ``None``."""
        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415

        from lib_application.db.session import (  # noqa: PLC0415
            create_engine_for_env,
            dispose_engine,
        )

        settings = self._settings()
        engine = create_engine_for_env(
            env="dev", db_url=settings.migration.render_as_string(hide_password=False)
        )
        try:
            with engine.connect() as connection:
                revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            return None if revision is None else str(revision)
        except SQLAlchemyError:
            return None
        finally:
            dispose_engine(engine)

    def deployed_record(self) -> dict[str, Any] | None:
        """The newest successful record, used to decide rebuilds and rollback targets."""
        from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415

        settings = self._settings()
        try:
            with record.maintenance_session(
                settings.migration.render_as_string(hide_password=False)
            ) as session:
                if not record.table_exists(session):
                    return None
                return record.latest(session, outcome=record.SUCCEEDED)
        except (SQLAlchemyError, ValueError, RuntimeError):
            return None

    # -- planning -----------------------------------------------------------------

    def plan(self, *, skip_snapshot: bool = False) -> DeploymentPlan:
        """Everything the run would do, without changing anything."""
        present = self.database_present()
        mode = record.UPGRADE if present else record.INSTALL
        current = self.installed_revision() if present else None
        deployed = self.deployed_record() if present else None
        image_reference = f"{artefact.PLATFORM_REPOSITORY}:{self.source.tag}"
        identity = artefact.image_identity(image_reference, run=self.run)
        example = environment.declared_keys(self.root / configuration.ENV_EXAMPLE)
        configured = environment.declared_keys(self.root / configuration.ENV_FILE)
        warnings = [
            f"{key} is exported in this shell and overrides .env"
            for key in environment.shell_overrides(environment.load_env_file(self.root / ".env"))
        ]
        if self.source.dirty:
            warnings.append("the working tree has uncommitted changes; the image is tagged -dirty")
        snapshot_reason: str | None = None
        snapshot_path: str | None = None
        if mode == record.INSTALL:
            snapshot_reason = NO_SNAPSHOT_FRESH
        elif skip_snapshot:
            snapshot_reason = NO_SNAPSHOT_REQUESTED
        else:
            snapshot_path = str(self._snapshot_target())
        return DeploymentPlan(
            mode=mode,
            image_tag=self.source.tag,
            source_commit=self.source.commit,
            source_dirty=self.source.dirty,
            image_present=identity is not None,
            # A dirty tree reuses one tag across edits, so its image is always rebuilt.
            rebuild=identity is None or self.source.dirty,
            target_head=self.target_head(),
            current_revision=current,
            pending_revisions=[
                revision for revision in self.pending_revisions(current) if revision != current
            ],
            deployed=deployed,
            configuration_keys=environment.key_diff(example, configured),
            snapshot_path=snapshot_path,
            snapshot_reason=snapshot_reason,
            warnings=warnings,
        )

    def _snapshot_target(self) -> Path:
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.snapshot_directory / f"snapshot-{stamp}-{self.source.tag}.dump"

    # -- running ------------------------------------------------------------------

    def start_only(self) -> None:
        """Bring a stopped stack back up on the image it already recorded."""
        self.lifecycle.command("up", "-d", "--wait", "postgres")
        self.lifecycle.start_runtime()
        self._verify_health()

    def deploy(self, plan: DeploymentPlan, owner: Mapping[str, Any]) -> dict[str, Any]:
        """Run every stage, rolling back what a late failure justifies."""
        stage = "preflight"
        snapshot: Path | None = None
        deployment_id: int | None = None
        result: dict[str, Any] = {}
        try:
            stage = "build"
            if plan.rebuild:
                self._build(plan)
            stage = "snapshot"
            self.lifecycle.command("up", "-d", "--wait", "postgres")
            if plan.snapshot_reason is None:
                snapshot = self._snapshot()
            stage = "record"
            deployment_id = self._open_record(plan, snapshot)
            stage = "stop"
            self.lifecycle.stop_runtime()
            stage = "provision"
            result = self._provision(owner)
            stage = "account"
            if plan.mode == record.INSTALL:
                self._create_paper_account(owner)
            stage = "start"
            self._point_compose_at(plan.image_tag)
            self.lifecycle.start_runtime()
            stage = "verify"
            self._verify_health()
            deployment_id = self._close(plan, snapshot, deployment_id, record.SUCCEEDED)
        except (ValueError, RuntimeError, OSError) as exc:
            self._recover(plan, stage, snapshot, deployment_id, exc)
            raise
        artefact.prune_generations(
            artefact.PLATFORM_REPOSITORY,
            keep={plan.image_tag, self.previous_tag},
            run=self.run,
        )
        prune_snapshots(self.snapshot_directory)
        return {
            "mode": plan.mode,
            "image_tag": plan.image_tag,
            "revision": result.get("revision"),
            "owner_id": result.get("owner_id"),
            "deployment_id": deployment_id,
            "snapshot": None if snapshot is None else str(snapshot),
        }

    # -- stages -------------------------------------------------------------------

    def _build(self, plan: DeploymentPlan) -> None:
        from dev_cli.core.builder import Builder  # noqa: PLC0415
        from dev_cli.core.config import load_config  # noqa: PLC0415

        self.echo(f"Building wheels and image {artefact.PLATFORM_REPOSITORY}:{plan.image_tag}")
        builder = Builder(load_config())
        builder.build_all_libs()
        builder.build_all_strategies()
        builder.docker_builder.build_from_containers_config(
            plan.image_tag,
            Path("config/containers.yaml"),
            stamp={
                "VM_SOURCE_COMMIT": plan.source_commit,
                "VM_SOURCE_DIRTY": "true" if plan.source_dirty else "false",
                "VM_BUILD_TIME": artefact.build_time(),
            },
        )

    def _snapshot(self) -> Path:
        from dev_cli.core.database_backup import DatabaseBackup  # noqa: PLC0415

        target = self._snapshot_target()
        target.parent.mkdir(parents=True, exist_ok=True)
        self.echo(f"Snapshotting the database to {target}")
        # The dump runs inside the existing PostgreSQL container, so it uses the
        # container-addressed configuration, not the host-resolved one.
        DatabaseBackup(self.lifecycle).backup(target)
        if not snapshot_is_restorable(target):
            msg = "The snapshot is not a PostgreSQL custom archive; refusing to continue"
            raise DeploymentError(msg)
        return target

    def _open_record(self, plan: DeploymentPlan, snapshot: Path | None) -> int | None:
        """Open the in-flight record, unless this database predates ``0108``."""
        from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415

        settings = self._settings()
        try:
            with record.maintenance_session(
                settings.migration.render_as_string(hide_password=False)
            ) as session:
                if not record.table_exists(session):
                    return None
                return record.open_record(
                    session,
                    mode=plan.mode,
                    image_tag=plan.image_tag,
                    image_digest=self._image_digest(plan.image_tag),
                    source_commit=plan.source_commit,
                    source_dirty=plan.source_dirty,
                    alembic_head=plan.target_head,
                    snapshot_path=snapshot,
                    snapshot_reason=plan.snapshot_reason,
                )
        except SQLAlchemyError:
            return None

    def _image_digest(self, tag: str) -> str:
        identity = artefact.image_identity(f"{artefact.PLATFORM_REPOSITORY}:{tag}", run=self.run)
        return "" if identity is None else identity["image_digest"]

    def _provision(self, owner: Mapping[str, Any]) -> dict[str, Any]:
        """Create or adopt the database, migrate it, and reconcile roles and references."""
        from dev_cli.core.bootstrap import bootstrap_database  # noqa: PLC0415

        return bootstrap_database(self.root, self._settings(), dict(owner))

    def _create_paper_account(self, owner: Mapping[str, Any]) -> None:
        """Give a fresh installation the local paper account it needs to show anything."""
        from sqlalchemy import select  # noqa: PLC0415

        from lib_application.db.models import LinkedBrokerAccount  # noqa: PLC0415
        from lib_application.db.session import (  # noqa: PLC0415
            create_engine_for_env,
            dispose_engine,
            get_session_factory,
        )
        from lib_application.services.account_onboarding import (  # noqa: PLC0415
            BrokerAccountIn,
            onboard_account,
            owner_scope,
        )
        from lib_infrastructure.brokers.secrets import create_secrets_provider  # noqa: PLC0415

        profile = dict(owner["profile"])
        equity = configuration.parse_equity(
            self.host_env.get(configuration.PAPER_EQUITY_KEY) or "0"
        )
        url = self.host_env["BACKEND_DATABASE_URL"]
        engine = create_engine_for_env(env="dev", db_url=url)
        try:
            factory = get_session_factory(engine=engine)
            with factory() as session, session.begin(), owner_scope(session) as owner_id:
                existing = session.scalar(
                    select(LinkedBrokerAccount.account_id).where(
                        LinkedBrokerAccount.user_id == owner_id,
                        LinkedBrokerAccount.config_key == configuration.PAPER_ACCOUNT_CONFIG_KEY,
                    )
                )
                if existing is not None:
                    return
                onboard_account(
                    session,
                    BrokerAccountIn(
                        config_key=configuration.PAPER_ACCOUNT_CONFIG_KEY,
                        broker_code=configuration.PAPER_BROKER_CODE,
                        environment="paper",
                        base_ccy=str(profile["base_ccy"]),
                        display_name="Local paper account",
                        paper_initial_equity=equity,
                        paper_initial_cash=equity,
                    ),
                    create_secrets_provider(
                        backend="db",
                        session_factory=factory,
                        master_keys=self.host_env.get("SECRETS_MASTER_KEYS") or None,
                    ),
                )
        finally:
            dispose_engine(engine)

    def _point_compose_at(self, tag: str) -> None:
        """Write the exact tag into ``.env`` so Compose names one immutable image."""
        path = self.root / configuration.ENV_FILE
        if not path.is_file():
            return
        path.write_text(
            environment.render_values(
                path.read_text(encoding="utf-8"), {artefact.IMAGE_TAG_KEY: tag}
            ),
            encoding="utf-8",
        )
        self.env[artefact.IMAGE_TAG_KEY] = tag
        self.lifecycle.env[artefact.IMAGE_TAG_KEY] = tag

    def _verify_health(self) -> None:
        """Every declared service is running and healthy, and the UI answers."""
        services = artefact.compose_services(self.lifecycle.command("ps", "--format", "json"))
        unhealthy = artefact.unhealthy_services(services)
        if unhealthy:
            msg = f"Services started but are not healthy: {', '.join(unhealthy)}"
            raise DeploymentError(msg)
        port = (self.env.get("BACKEND_PORT") or "8081").strip()
        url = f"http://127.0.0.1:{port}/ui/"
        try:
            with urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT_SECONDS) as response:
                status = int(response.status)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            msg = f"The owner UI did not answer on {url}"
            raise DeploymentError(msg) from exc
        if status != _HTTP_OK:
            msg = f"The owner UI answered {status} on {url}"
            raise DeploymentError(msg)

    # -- failure ------------------------------------------------------------------

    def _close(
        self,
        plan: DeploymentPlan,
        snapshot: Path | None,
        deployment_id: int | None,
        outcome: str,
        *,
        failure_stage: str | None = None,
    ) -> int | None:
        """Close the record, opening one first when the migration has just created it."""
        from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415

        settings = self._settings()
        try:
            with record.maintenance_session(
                settings.migration.render_as_string(hide_password=False)
            ) as session:
                if not record.table_exists(session):
                    return deployment_id
                if deployment_id is None:
                    deployment_id = record.open_record(
                        session,
                        mode=plan.mode,
                        image_tag=plan.image_tag,
                        image_digest=self._image_digest(plan.image_tag),
                        source_commit=plan.source_commit,
                        source_dirty=plan.source_dirty,
                        alembic_head=plan.target_head,
                        snapshot_path=snapshot,
                        snapshot_reason=plan.snapshot_reason,
                    )
                record.close_record(
                    session, deployment_id, outcome=outcome, failure_stage=failure_stage
                )
        except (SQLAlchemyError, ValueError, RuntimeError) as exc:
            self.echo(f"Could not write the deployment record: {exc}")
        return deployment_id

    def _recover(
        self,
        plan: DeploymentPlan,
        stage: str,
        snapshot: Path | None,
        deployment_id: int | None,
        error: Exception,
    ) -> None:
        """Undo what a failure at this stage justifies, and say what was done."""
        decision = rollback_decision(stage, mode=plan.mode, snapshot=snapshot)
        self.echo(f"Deployment failed during {stage}: {error}")
        if decision == "none":
            self._close(plan, snapshot, deployment_id, record.FAILED, failure_stage=stage)
            return
        if decision == "refuse":
            self.echo(
                "No verified snapshot exists, so the database was left exactly as it is. "
                "Inspect it before retrying; a bad restore is worse than a stopped stack."
            )
            self._close(plan, snapshot, deployment_id, record.FAILED, failure_stage=stage)
            return
        assert snapshot is not None
        try:
            self._restore(snapshot)
            target = self._previous_tag(plan)
            self._point_compose_at(target)
            self.lifecycle.start_runtime()
            self.echo(f"Rolled back to {artefact.PLATFORM_REPOSITORY}:{target}")
            self._close(plan, snapshot, deployment_id, record.ROLLED_BACK, failure_stage=stage)
        except (ValueError, RuntimeError, OSError) as rollback_error:
            self.echo(f"Rollback did not complete: {rollback_error}")
            self._close(plan, snapshot, deployment_id, record.FAILED, failure_stage=stage)

    def _previous_tag(self, plan: DeploymentPlan) -> str:
        """The generation to return to: the last recorded success, else what ``.env`` had."""
        deployed = plan.deployed or {}
        candidate = str(deployed.get("image_tag") or "") or self.previous_tag
        if not candidate or candidate == plan.image_tag:
            msg = "No previous image generation is recorded to roll back to"
            raise DeploymentError(msg)
        return candidate

    def _restore(self, snapshot: Path) -> None:
        """Provision roles first: a dump's grants cannot resolve without them."""
        from dev_cli.core.database_backup import DatabaseBackup  # noqa: PLC0415
        from dev_cli.core.runtime_roles import (  # noqa: PLC0415
            PASSWORD_ENV,
            provision_runtime_roles,
        )

        settings = self._settings()
        provision_runtime_roles(
            settings.admin.set(database=settings.migration.database).render_as_string(
                hide_password=False
            ),
            settings.migration.render_as_string(hide_password=False),
            {login: self.host_env.get(key, "") for login, key in PASSWORD_ENV.items()},
        )
        DatabaseBackup(self.lifecycle).restore(snapshot)


def describe_state(root: Path, env: Mapping[str, str]) -> list[checks.Check]:
    """Doctor's view of the installed state: schema, record and running image.

    Every fact here needs a reachable Docker or PostgreSQL, so an unreachable
    installation is reported as one warning instead of an exception.
    """
    from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415

    tag = (env.get(artefact.IMAGE_TAG_KEY) or "").strip()
    try:
        deployer = Deployer(root, env, echo=lambda _message: None)
    except (ValueError, RuntimeError, OSError) as exc:
        return [checks.Check(name="deployment:state", status=checks.WARN, detail=str(exc))]
    collected = list(checks.check_image(tag))
    try:
        present = deployer.database_present()
        running = artefact.running_images(
            artefact.compose_services(deployer.lifecycle.command("ps", "--format", "json"))
        )
    except (SQLAlchemyError, ValueError, RuntimeError, OSError) as exc:
        collected.append(
            checks.Check(
                name="deployment:state",
                status=checks.WARN,
                detail=f"the installation could not be inspected: {exc}",
            )
        )
        return collected
    collected.extend(
        checks.check_state(
            database_present=present,
            installed_revision=deployer.installed_revision() if present else None,
            target_head=deployer.target_head(),
            deployed=deployer.deployed_record() if present else None,
            running=running,
            image_tag=tag,
        )
    )
    return collected


__all__ = [
    "NO_SNAPSHOT_FRESH",
    "NO_SNAPSHOT_REQUESTED",
    "SNAPSHOT_DIRECTORY",
    "SNAPSHOT_RETENTION",
    "STAGES",
    "Deployer",
    "DeploymentError",
    "DeploymentPlan",
    "describe_state",
    "prune_snapshots",
    "rollback_decision",
    "snapshot_is_restorable",
]
