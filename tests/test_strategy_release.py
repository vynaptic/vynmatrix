"""Paper release of one exact strategy version: evidence gates, audit and undo."""

from __future__ import annotations

import json
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dev_cli.core import strategy_release as loader
from dev_cli.core.catalogue import load_strategy_releases
from lib_application.db.models import (
    AccountRebalancePlan,
    ApiAuditLog,
    Base,
    Deployment,
    Execution,
    ExecutionDecisionLog,
    ExecutionLog,
    Order,
    OrderIntent,
    OutboxEvent,
    PendingOrder,
    Strategy,
    StrategyVersion,
    User,
    UserStrategyBinding,
)
from lib_application.services import strategy_release as service
from lib_application.services.binding_control import (
    MODES,
    BindingControlError,
    require_release,
    trading_state,
)
from lib_application.services.strategy_authority import require_active_strategy_version

ROOT = Path(__file__).resolve().parents[1]
STRATEGY_ID = "atr_breakout_v1"
PAPER = {"environment": "dev", "execution_mode": "paper", "allow_live": "false"}
COMMIT = "a" * 40
TREE = "c" * 40
IMAGE_TAG = "sha-aaaaaaaaaaaa"


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
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def release():
    (packaged,) = load_strategy_releases(ROOT, strategy_id=STRATEGY_ID)
    return packaged


def install(session, release, *, status="registered", active=False, semver=None):
    if session.get(Strategy, release.strategy_id) is None:
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
            strategy_id=release.strategy_id,
            semver=semver or release.semver,
            status=status,
            default_params=release.default_params,
            param_schema=release.param_schema,
        )
    )
    session.commit()


def deploy(session, *, outcome="succeeded", dirty=False):
    now = datetime.now(tz=UTC)
    row = Deployment(
        mode="install",
        image_tag=IMAGE_TAG,
        image_digest="sha256:" + "d" * 64,
        source_commit=COMMIT,
        source_dirty=dirty,
        alembic_head="0109",
        started_at=now,
        finished_at=now,
        outcome=outcome,
        snapshot_reason="fresh install",
    )
    session.add(row)
    session.commit()
    return row.deployment_id


def evidence(release, **changes):
    values = {
        "release": release,
        "readiness": "READY_FOR_PAPER_TRADING",
        "decision": "KEEP_HIGH_PRIORITY",
        "source_commit": COMMIT,
        "config_sha256": "b" * 64,
        "strategy_tree": TREE,
        "deployment_id": 1,
    }
    return service.ReleaseSource(**(values | changes))


def release_it(session, source, *, apply=True, reason="paper trial", **runtime):
    return service.release_strategy_version(
        session, source, **(PAPER | runtime), reason=reason, apply=apply
    )


def withdraw_it(session, *, version="1.0.0", apply=True, reason="trial over", **runtime):
    return service.withdraw_strategy_version(
        session,
        strategy_id=STRATEGY_ID,
        version=version,
        **(PAPER | runtime),
        reason=reason,
        apply=apply,
    )


def state(session, semver="1.0.0"):
    session.expire_all()
    version = session.scalar(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == STRATEGY_ID, StrategyVersion.semver == semver
        )
    )
    return version.status, session.get(Strategy, STRATEGY_ID).is_active


def audits(session):
    return [row.action for row in session.scalars(select(ApiAuditLog))]


@pytest.fixture
def ready(session, release):
    install(session, release)
    deploy(session)
    return evidence(release)


def test_check_writes_nothing_then_apply_releases_audits_and_repeats_as_noop(session, ready):
    checked = release_it(session, ready, apply=False, reason=None)
    assert checked == {
        "strategy_id": STRATEGY_ID,
        "version": "1.0.0",
        "mode": "check",
        "changed": False,
        "would_change": True,
        "status": "registered",
        "strategy_is_active": False,
    }
    session.commit()
    assert state(session) == ("registered", False)
    assert audits(session) == []

    assert release_it(session, ready)["changed"] is True
    session.commit()
    assert state(session) == ("active", True)
    (row,) = session.scalars(select(ApiAuditLog))
    assert row.action == "strategy.release"
    assert row.user_id == "owner"
    assert row.req == {
        "strategy_id": STRATEGY_ID,
        "version": "1.0.0",
        "reason": "paper trial",
        "source_commit": COMMIT,
        "config_sha256": "b" * 64,
        "strategy_tree": TREE,
        "readiness": "READY_FOR_PAPER_TRADING",
        "decision": "KEEP_HIGH_PRIORITY",
        "deployment_id": 1,
    }
    assert row.resp["allow_live"] == "false"
    assert row.resp["previous_status"] == "registered"

    assert release_it(session, ready)["changed"] is False
    session.commit()
    assert audits(session) == ["strategy.release"]


def test_release_satisfies_the_bind_and_new_work_gates_and_withdraw_reverses_them(session, ready):
    with pytest.raises(BindingControlError):
        require_release(session, STRATEGY_ID)
    assert trading_state(session, "owner").released == 0
    release_it(session, ready)
    session.commit()
    require_release(session, STRATEGY_ID)
    assert trading_state(session, "owner").released == 1
    assert require_active_strategy_version(
        session, strategy_id=STRATEGY_ID, strategy_version="1.0.0"
    )
    withdraw_it(session)
    session.commit()
    assert state(session) == ("registered", False)
    with pytest.raises(BindingControlError, match="not released"):
        require_release(session, STRATEGY_ID)
    assert trading_state(session, "owner").released == 0
    assert release_it(session, ready)["changed"] is True
    assert audits(session) == ["strategy.release", "strategy.withdraw", "strategy.release"]


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"readiness": "STATIC_REVIEW_ONLY"}, "not paper-ready"),
        ({"readiness": "READY_FOR_BACKTEST"}, "not paper-ready"),
        ({"decision": "INSUFFICIENT_EVIDENCE"}, "does not keep"),
        ({"decision": "E2E_PIPELINE_CANARY_ONLY"}, "does not keep"),
        ({"decision": "PROMOTED"}, "does not keep"),
        ({"source_commit": "HEAD"}, "source_commit"),
        ({"strategy_tree": "tree"}, "strategy_tree"),
        ({"config_sha256": "B" * 64}, "SHA-256"),
        ({"deployment_id": 0}, "deployment_id"),
    ],
)
def test_unproven_evidence_is_refused_before_any_write(session, ready, changes, match):
    with pytest.raises(ValueError, match=match):
        release_it(session, replace(ready, **changes))
    assert state(session) == ("registered", False)


@pytest.mark.parametrize(
    "runtime",
    [
        {"environment": "local"},
        {"execution_mode": "live"},
        {"allow_live": "true"},
        {"allow_live": "False"},
    ],
)
def test_live_or_unknown_runtime_is_refused(session, ready, runtime):
    with pytest.raises(ValueError, match="EXECUTION_MODE=paper"):
        release_it(session, ready, **runtime)
    with pytest.raises(ValueError, match="EXECUTION_MODE=paper"):
        withdraw_it(session, **runtime)


@pytest.mark.parametrize("reason", [None, "   ", "x" * 501])
def test_apply_requires_a_bounded_reason(session, ready, reason):
    with pytest.raises(ValueError, match="reason"):
        release_it(session, ready, reason=reason)
    with pytest.raises(ValueError, match="reason"):
        withdraw_it(session, reason=reason)


@pytest.mark.parametrize(
    ("status", "active", "match"),
    [
        ("deprecated", False, "new semver"),
        ("pulled", False, "new semver"),
        ("active", False, "withdraw it first"),
    ],
)
def test_retired_or_switched_off_versions_are_refused(session, release, status, active, match):
    install(session, release, status=status, active=active)
    deploy(session)
    with pytest.raises(ValueError, match=match):
        release_it(session, evidence(release))


def test_registered_row_that_differs_from_the_package_is_refused(session, ready):
    session.scalar(select(StrategyVersion)).default_params = {"lookback": "1"}
    session.commit()
    with pytest.raises(ValueError, match="packaged source"):
        release_it(session, ready)


def test_a_second_active_version_is_refused(session, release):
    install(session, release, status="active", active=True, semver="0.9.0")
    install(session, release)
    deploy(session)
    with pytest.raises(ValueError, match=r"0\.9\.0 is released; withdraw it first"):
        release_it(session, evidence(release))


def test_release_requires_a_recorded_clean_finished_deployment(session, release):
    install(session, release)
    with pytest.raises(ValueError, match="No deployment is recorded"):
        release_it(session, evidence(release))
    deploy(session, dirty=True)
    with pytest.raises(ValueError, match="clean checkout"):
        release_it(session, evidence(release))
    deploy(session)
    deploy(session, outcome="failed")
    with pytest.raises(ValueError, match="did not finish"):
        release_it(session, evidence(release, deployment_id=2))
    deploy(session, outcome="rolled_back")
    with pytest.raises(ValueError, match="changed while the release was checked"):
        release_it(session, evidence(release, deployment_id=1))
    assert release_it(session, evidence(release, deployment_id=2))["changed"] is True


def test_withdraw_check_writes_nothing_and_repeat_is_noop(session, ready):
    release_it(session, ready)
    session.commit()
    checked = withdraw_it(session, apply=False, reason=None)
    assert checked["mode"] == "check"
    assert checked["changed"] is False
    assert checked["would_change"] is True
    assert checked["status"] == "active"
    assert checked["strategy_is_active"] is True
    session.commit()
    assert state(session) == ("active", True)
    withdraw_it(session)
    session.commit()
    row = session.scalars(
        select(ApiAuditLog).where(ApiAuditLog.action == "strategy.withdraw")
    ).one()
    assert row.req == {"strategy_id": STRATEGY_ID, "version": "1.0.0", "reason": "trial over"}
    assert row.resp["dead_letter_count"] == 0
    assert withdraw_it(session)["changed"] is False
    assert audits(session) == ["strategy.release", "strategy.withdraw"]


def test_withdraw_needs_no_packaged_source_so_an_old_version_can_leave(session, release):
    install(session, release, status="active", active=True, semver="0.9.0")
    session.scalar(select(StrategyVersion)).default_params = {"anything": "older"}
    session.commit()
    assert withdraw_it(session, version="0.9.0")["changed"] is True
    session.commit()
    assert state(session, "0.9.0") == ("registered", False)


@pytest.mark.parametrize(
    ("status", "active", "match"),
    [
        ("pulled", False, "retired"),
        ("registered", True, "not the released version"),
    ],
)
def test_withdraw_refuses_what_is_not_this_release(session, release, status, active, match):
    install(session, release, status=status, active=active)
    with pytest.raises(ValueError, match=match):
        withdraw_it(session)
    with pytest.raises(ValueError, match="No registered version"):
        withdraw_it(session, version="9.9.9")


def _binding(session, mode, account=1):
    row = UserStrategyBinding(
        user_id="owner",
        broker_account_id=account,
        strategy_id=STRATEGY_ID,
        **MODES[mode].as_fields(),
    )
    session.add(row)
    session.flush()
    return row


def _order(session, status, purpose="stop_loss"):
    session.add(
        PendingOrder(
            order_id=f"order-{status}",
            user_id="owner",
            signal_id="signal-1",
            broker_account_id=1,
            symbol="BTC-USDC",
            settlement_currency="USD",
            side="SELL",
            order_type="stop",
            quantity=Decimal("1"),
            purpose=purpose,
            reduce_only=True,
            execution_mode="paper",
            broker="paper",
            status=status,
            strategy_id=STRATEGY_ID,
        )
    )


def _execution(session, status):
    session.add(
        ExecutionLog(
            log_id=f"log-{status}",
            user_id="owner",
            account_id=1,
            strategy_id=STRATEGY_ID,
            signal_type="long",
            execution_mode="paper",
            status=status,
        )
    )


def _decision(session, status):
    binding = _binding(session, "off", account=2)
    session.add(
        ExecutionDecisionLog(
            user_id="owner",
            binding_id=binding.binding_id,
            status=status,
            idempotency_key=f"decision-{status}",
        )
    )


def _event(session, status, strategy_id=STRATEGY_ID, nested=False, top=None):
    payload = {"signal": {"strategy_id": strategy_id}} if nested else {"strategy_id": strategy_id}
    if top is not None:
        payload["strategy_id"] = top
    session.add(
        OutboxEvent(
            topic="signals.scored",
            event_type="scored",
            payload=payload,
            status=status,
            event_key=f"event-{status}-{strategy_id}-{nested}",
        )
    )


_FILLS = iter(range(1, 1000))


def _fill(session, side, qty="0.5", account=1):
    number = next(_FILLS)
    session.add(
        OrderIntent(
            intent_id=number,
            user_id="owner",
            account_id=account,
            strategy_id=STRATEGY_ID,
            canonical_signal_id=1,
            side=side,
            execution_mode="spot",
            broker_environment="paper",
            method="SPOT",
            payload={},
            status="routed",
        )
    )
    session.add(
        Order(
            order_id=number,
            intent_id=number,
            broker_id=1,
            account_id=account,
            settlement_currency="USD",
            client_order_id=f"client-{number}",
            broker_order_ref=f"broker-{number}",
            state="filled",
        )
    )
    session.add(
        Execution(
            exec_id=number,
            order_id=number,
            instr_id=1,
            fill_ts=datetime(2026, 10, 1, 12, 0) + timedelta(minutes=number),
            qty=Decimal(qty),
            price=Decimal("60000"),
            fee_ccy="USD",
            fee_amount=Decimal("1"),
            venue="paper",
            trade_id=f"trade-{number}",
        )
    )


def _plan(session, status):
    now = datetime.now(tz=UTC)
    session.add(
        AccountRebalancePlan(
            account_plan_id="1" * 64,
            model_rebalance_id="2" * 64,
            user_id="owner",
            binding_id=1,
            broker_account_id=1,
            strategy_id=STRATEGY_ID,
            data_use_scope="paper_forward",
            provider_authority_sha256="3" * 64,
            decision_cutoff=now,
            execute_not_before=now + timedelta(minutes=1),
            execution_session_sha256="4" * 64,
            expires_at=now + timedelta(hours=1),
            execution_policy={},
            broker_route={},
            execution_policy_sha256="5" * 64,
            broker_route_sha256="6" * 64,
            content_sha256="7" * 64,
            model_leg_count=1,
            execution_leg_count=1,
            intentional_cash_slots=0,
            status=status,
            progress_deadline_at=now + timedelta(minutes=30),
        )
    )


@pytest.mark.parametrize(
    ("make", "match"),
    [
        (lambda s: _binding(s, "close_only"), "1 binding"),
        (lambda s: _binding(s, "trading"), "1 binding"),
        (lambda s: _order(s, "working"), "1 open or resting order"),
        (lambda s: _order(s, "submission_unknown", purpose=None), "1 open or resting order"),
        (lambda s: _execution(s, "submitted"), "1 unfinished execution"),
        (lambda s: _decision(s, "pending"), "1 pending scoring decision"),
        (lambda s: _event(s, "failed"), "1 undelivered outbox event"),
        (lambda s: _event(s, "in_progress", nested=True), "1 undelivered outbox event"),
        (lambda s: _event(s, "pending", nested=True, top="another_v1"), "1 undelivered outbox"),
        (lambda s: _fill(s, "BUY"), "1 open position"),
        (lambda s: (_fill(s, "BUY", "1"), _fill(s, "SELL", "0.4")), "1 open position"),
        (lambda s: _fill(s, "SELL", account=2), "1 open position"),
        (lambda s: _plan(s, "running"), "1 unfinished rebalance plan"),
        (lambda s: _plan(s, "blocked"), "1 unfinished rebalance plan"),
    ],
)
def test_withdraw_is_refused_while_anything_could_still_trade(session, ready, make, match):
    release_it(session, ready)
    session.commit()
    make(session)
    session.commit()
    with pytest.raises(ValueError, match=f"Cannot withdraw {STRATEGY_ID}: {match}"):
        withdraw_it(session)
    assert state(session) == ("active", True)


def test_finished_work_dead_letters_and_other_strategies_do_not_block(session, ready):
    release_it(session, ready)
    session.commit()
    _binding(session, "off")
    _order(session, "filled")
    _execution(session, "filled")
    _decision(session, "executed")
    _event(session, "published")
    _event(session, "dead_letter")
    _event(session, "pending", strategy_id="another_v1")
    _fill(session, "BUY", "1")
    _fill(session, "SELL", "0.25")
    _fill(session, "SELL", "0.75")
    _plan(session, "completed")
    session.commit()
    result = withdraw_it(session)
    assert result["changed"] is True
    assert result["dead_letter_count"] == 1


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    """A minimal committed source tree holding one strategy marked paper-ready."""
    schema = "config/schemas/indicator_strategy_config.schema.json"
    (tmp_path / schema).parent.mkdir(parents=True)
    shutil.copy(ROOT / schema, tmp_path / schema)
    target = tmp_path / "strategies/indicator/ATRBreakout"
    target.mkdir(parents=True)
    shutil.copy(ROOT / "strategies/indicator/ATRBreakout/core.py", target / "core.py")
    config = json.loads((ROOT / "strategies/indicator/ATRBreakout/config.json").read_text())
    config["metadata"] |= {"readiness": "READY_FOR_PAPER_TRADING", "decision": "KEEP_HIGH_PRIORITY"}
    (target / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.email=owner@example.invalid",
        "-c",
        "user.name=owner",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "mark ready",
    )
    return tmp_path


def test_loader_reads_the_committed_label_hash_and_tree(checkout):
    packaged = loader.load_packaged_release(checkout, strategy_id=STRATEGY_ID, version="1.0.0")
    commit = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert packaged.readiness == "READY_FOR_PAPER_TRADING"
    assert packaged.decision == "KEEP_HIGH_PRIORITY"
    assert packaged.source_commit == commit
    assert packaged.strategy_tree == loader.strategy_tree_at(checkout, commit, "ATRBreakout")
    assert loader.strategy_tree_at(checkout, "0" * 40, "ATRBreakout") is None
    service.require_release_labels(
        STRATEGY_ID, readiness=packaged.readiness, decision=packaged.decision
    )


def test_loader_refuses_an_uncommitted_label_or_an_unknown_version(checkout):
    with pytest.raises(ValueError, match="exact unambiguous"):
        loader.load_packaged_release(checkout, strategy_id=STRATEGY_ID, version="9.9.9")
    config = checkout / "strategies/indicator/ATRBreakout/config.json"
    config.write_text(config.read_text().replace("KEEP_HIGH_PRIORITY", "IMPROVE_THEN_VALIDATE"))
    with pytest.raises(ValueError, match="differs from HEAD"):
        loader.load_packaged_release(checkout, strategy_id=STRATEGY_ID, version="1.0.0")
    _git(checkout, "rm", "-q", "--cached", "strategies/indicator/ATRBreakout/config.json")
    with pytest.raises(ValueError, match="not tracked"):
        loader.load_packaged_release(checkout, strategy_id=STRATEGY_ID, version="1.0.0")


def test_the_swing_canary_can_never_be_released():
    config = json.loads((ROOT / "strategies/indicator/SwingHighLowPMO/config.json").read_text())
    with pytest.raises(ValueError, match=r"not paper-ready|does not keep"):
        service.require_release_labels(
            config["strategy_id"],
            readiness="READY_FOR_PAPER_TRADING",
            decision=config["metadata"]["decision"],
        )


@pytest.fixture
def cli(session, release, monkeypatch):
    """Drive the commands against the SQLite session with a stubbed checkout."""
    user = import_module("dev_cli.commands.user")
    commands = import_module("dev_cli.commands.db_release")
    stages: list[str] = []

    @contextmanager
    def database_session(stage):
        stages.append(stage)
        yield session

    packaged = loader.PackagedRelease(
        release=release,
        readiness="READY_FOR_PAPER_TRADING",
        decision="KEEP_HIGH_PRIORITY",
        source_commit=COMMIT,
        config_sha256="b" * 64,
        strategy_tree=TREE,
    )
    trees = {COMMIT: TREE}
    monkeypatch.setattr(user, "_database_session", database_session)
    monkeypatch.setattr(commands.deployment_environment, "load_env_file", lambda path: {})
    monkeypatch.setattr(loader, "load_packaged_release", lambda root, **kwargs: packaged)
    monkeypatch.setattr(loader, "strategy_tree_at", lambda root, commit, name: trees.get(commit))
    install(session, release)
    deploy(session)

    def run(name, *args, **env):
        command = getattr(commands, name)
        environment = {
            "ENV": "dev",
            "ENVIRONMENT": "dev",
            "EXECUTION_MODE": "paper",
            "EXECUTION_ENGINE_ALLOW_LIVE": "false",
            "VM_DEPLOY_IMAGE_TAG": IMAGE_TAG,
        } | env
        return CliRunner().invoke(
            command,
            ["--strategy-id", STRATEGY_ID, "--version", "1.0.0", *args],
            env=environment,
        )

    run.stages = stages
    run.packaged = packaged
    run.trees = trees
    return run


def test_cli_checks_then_applies_under_maintenance_and_withdraws(cli, session):
    checked = cli("release_strategy", "--check")
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.output)["mode"] == "check"
    assert state(session) == ("registered", False)
    applied = cli("release_strategy", "--apply", "--reason", "paper trial")
    assert applied.exit_code == 0, applied.output
    assert state(session) == ("active", True)
    withdrawn = cli("withdraw_strategy", "--apply", "--reason", "trial over")
    assert withdrawn.exit_code == 0, withdrawn.output
    assert json.loads(withdrawn.output)["dead_letter_count"] == 0
    assert state(session) == ("registered", False)
    assert cli.stages == ["maintenance"] * 3


@pytest.mark.parametrize("args", [(), ("--check", "--apply"), ("--check", "--reason", "why")])
def test_cli_requires_exactly_one_mode(cli, args):
    for name in ("release_strategy", "withdraw_strategy"):
        result = cli(name, *args)
        assert result.exit_code == 2, result.output
    assert cli.stages == []


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"EXECUTION_MODE": None}, "EXECUTION_MODE must be set"),
        ({"EXECUTION_ENGINE_ALLOW_LIVE": None}, "EXECUTION_ENGINE_ALLOW_LIVE must be set"),
        ({"ENV": "staging"}, "must agree"),
    ],
)
def test_cli_refuses_unresolved_runtime_before_opening_the_database(cli, env, match):
    for name in ("release_strategy", "withdraw_strategy"):
        result = cli(name, "--check", **env)
        assert result.exit_code == 1
        assert match in result.output
    assert cli.stages == []


def test_cli_refuses_an_unready_label_before_opening_the_database(cli, monkeypatch):
    unready = replace(cli.packaged, readiness="STATIC_REVIEW_ONLY")
    monkeypatch.setattr(loader, "load_packaged_release", lambda root, **kwargs: unready)
    result = cli("release_strategy", "--check")
    assert result.exit_code == 1
    assert "not paper-ready" in result.output
    assert cli.stages == []


def test_cli_refuses_source_the_running_image_does_not_ship(cli, session):
    cli.trees[COMMIT] = "e" * 40
    result = cli("release_strategy", "--apply", "--reason", "paper trial")
    assert result.exit_code == 1
    assert "deploy this commit first" in result.output
    assert state(session) == ("registered", False)


@pytest.mark.parametrize("tag", [None, "sha-bbbbbbbbbbbb"])
def test_cli_refuses_an_image_tag_the_deployment_did_not_record(cli, session, tag):
    result = cli("release_strategy", "--check", VM_DEPLOY_IMAGE_TAG=tag)
    assert result.exit_code == 1
    assert "deploy with vmdev deploy" in result.output
    assert state(session) == ("registered", False)


def test_cli_refuses_live_runtime(cli, session):
    result = cli("release_strategy", "--apply", "--reason", "x", EXECUTION_ENGINE_ALLOW_LIVE="true")
    assert result.exit_code == 1
    assert "EXECUTION_MODE=paper" in result.output
    assert state(session) == ("registered", False)
