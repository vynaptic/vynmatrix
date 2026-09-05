"""The explicit development canary gate is not general release promotion."""

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dev_cli.core.canary_activation import load_canary
from lib_application.db.models import ApiAuditLog, Base, Strategy, StrategyVersion, User
from lib_application.services import canary_activation as service

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def session(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(service, "require_maintenance_database_role", lambda session: None)
    with Session(engine) as session:
        session.add(
            User(
                user_id="owner",
                email="owner@example.invalid",
                base_ccy="USD",
                is_deployment_owner=True,
            )
        )
        yield session
    engine.dispose()


@pytest.fixture
def canary():
    return load_canary(ROOT, strategy_id="swing_high_low_pmo_v1", version="1.1.0")


def install(session, canary, *, status="registered", active=False):
    release = canary.release
    session.add(
        Strategy(
            strategy_id=release.strategy_id,
            strategy_name=release.strategy_name,
            asset_class=release.asset_class,
            is_active=active,
        )
    )
    session.add(
        StrategyVersion(
            strat_ver_id=1,
            strategy_id=release.strategy_id,
            semver=release.semver,
            status=status,
            default_params=release.default_params,
            param_schema=release.param_schema,
        )
    )
    session.commit()


def activate(session, canary):
    return service.activate_canary(
        session, canary, environment="dev", execution_mode="paper", allow_live="false"
    )


def test_activate_repeat_and_rollback(session, canary):
    install(session, canary)
    assert activate(session, canary)["changed"] is True
    session.rollback()
    assert session.get(StrategyVersion, 1).status == "registered"
    assert session.scalar(select(ApiAuditLog)) is None
    assert activate(session, canary)["changed"] is True
    session.commit()
    assert activate(session, canary)["changed"] is False
    session.commit()
    assert len(session.scalars(select(ApiAuditLog)).all()) == 1


@pytest.mark.parametrize(
    ("status", "active"), [("pulled", False), ("deprecated", False), ("active", False)]
)
def test_retirement_cannot_be_reactivated(session, canary, status, active):
    install(session, canary, status=status, active=active)
    with pytest.raises(ValueError, match=r"retired|disabled"):
        activate(session, canary)


def test_immutable_release_drift_refused(session, canary):
    install(session, canary)
    session.get(StrategyVersion, 1).default_params = {}
    session.commit()
    with pytest.raises(ValueError, match="source"):
        activate(session, canary)


@pytest.mark.parametrize(
    "kwargs", [{"environment": "prod"}, {"execution_mode": "live"}, {"allow_live": "true"}]
)
def test_runtime_gates(session, canary, kwargs):
    install(session, canary)
    values = {"environment": "dev", "execution_mode": "paper", "allow_live": "false"} | kwargs
    with pytest.raises(ValueError, match=r"dev.*paper"):
        service.activate_canary(session, canary, **values)


def test_foreign_identity_and_missing_owner_refused(session, canary, monkeypatch):
    install(session, canary)

    def deny(_session):
        raise ValueError("maintenance authority required")

    monkeypatch.setattr(service, "require_maintenance_database_role", deny)
    with pytest.raises(ValueError, match="maintenance"):
        activate(session, canary)
    monkeypatch.setattr(service, "require_maintenance_database_role", lambda session: None)
    session.get(User, "owner").is_deployment_owner = False
    session.commit()
    with pytest.raises(RuntimeError, match="owner"):
        activate(session, canary)


def test_wrong_source_version_and_noncanary_refused():
    with pytest.raises(ValueError, match="version"):
        load_canary(ROOT, strategy_id="swing_high_low_pmo_v1", version="0.0.0")
    with pytest.raises(ValueError, match="canary"):
        load_canary(ROOT, strategy_id="us_quality_compounder_v1", version="0.2.0")


@pytest.mark.parametrize(
    "changes", [{"decision": "PROMOTED"}, {"enabled": False}, {"environments": ("dev", "prod")}]
)
def test_source_governance_refused(session, canary, changes):
    install(session, canary)
    with pytest.raises(ValueError, match="canary"):
        activate(session, replace(canary, **changes))
    assert session.get(StrategyVersion, 1).status == "registered"
    assert session.scalar(select(ApiAuditLog)) is None


def test_cli_uses_maintenance_session_and_commits(session, canary, monkeypatch):
    from contextlib import contextmanager
    from importlib import import_module

    from click.testing import CliRunner

    user = import_module("dev_cli.commands.user")
    from dev_cli.commands.db_canary import activate_canary as command

    install(session, canary)
    stages = []

    @contextmanager
    def database_session(stage):
        stages.append(stage)
        yield session

    monkeypatch.setattr(user, "_database_session", database_session)
    result = CliRunner().invoke(
        command,
        ["--strategy-id", canary.release.strategy_id, "--version", canary.release.semver],
        env={
            "ENVIRONMENT": "dev",
            "ENV": "dev",
            "EXECUTION_MODE": "paper",
            "EXECUTION_ENGINE_ALLOW_LIVE": "false",
        },
    )
    assert result.exit_code == 0, result.output
    assert stages == ["maintenance"]
    session.rollback()
    assert session.get(StrategyVersion, 1).status == "active"


def test_cli_rejects_conflicting_environment(monkeypatch):
    from click.testing import CliRunner

    from dev_cli.commands.db_canary import activate_canary as command

    result = CliRunner().invoke(
        command,
        ["--strategy-id", "swing_high_low_pmo_v1", "--version", "1.1.0"],
        env={"ENVIRONMENT": "dev", "ENV": "prod"},
    )
    assert result.exit_code != 0
    assert "must agree" in result.output
