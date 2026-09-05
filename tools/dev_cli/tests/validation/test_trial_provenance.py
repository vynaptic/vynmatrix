"""Immutable manifest and validation-trial provenance contracts."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dev_cli.validation.persistence.backtest_manifest_store import (
    BacktestManifestStore,
    canonical_manifest_bytes,
    manifest_sha256,
)
from dev_cli.validation.persistence.backtest_trial_store import (
    BacktestTrialRegistration,
    BacktestTrialStore,
    stable_trial_id,
)
from lib_application.db.models import (
    BacktestExperiment,
    BacktestResult,
    BacktestTrial,
    Base,
    Strategy,
    StrategyVersion,
)


def _engine():  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _seed_catalog(engine, *, semver: str = "1.2.3"):  # type: ignore[no-untyped-def]
    with Session(engine) as session:
        session.add(
            Strategy(
                strategy_id="registered_campaign",
                strategy_name="Registered Campaign",
                asset_class="crypto",
            )
        )
        session.flush()
        version = StrategyVersion(
            strategy_id="registered_campaign",
            semver=semver,
            git_commit="a" * 40,
            param_schema={},
            default_params={"indicator_period": 30},
        )
        experiment = BacktestExperiment(
            strategy_id="registered_campaign",
            experiment_type="parameter_sweep",
            metric="sharpe_ratio",
            config={"protocol": "registered-campaign-v1"},
        )
        session.add_all([version, experiment])
        session.commit()
        return experiment.experiment_id


def _registration(experiment_id: int, **overrides) -> BacktestTrialRegistration:  # type: ignore[no-untyped-def]
    values = {
        "experiment_id": experiment_id,
        "strategy_id": "registered_campaign",
        "strategy_semver": "1.2.3",
        "sequence": 0,
        "trial_family": "baseline",
        "fold_id": "full-panel",
        "cost_scenario": "expected",
        "parameters": {"risk_period": 7, "indicator_period": 30},
        "manifest_hash": "b" * 64,
    }
    values.update(overrides)
    return BacktestTrialRegistration(**values)


def test_canonical_manifest_is_order_independent_and_strict() -> None:
    first = {"z": [1, 2.5], "a": {"enabled": True}}
    second = {"a": {"enabled": True}, "z": [1, 2.5]}

    assert canonical_manifest_bytes(first) == canonical_manifest_bytes(second)
    assert manifest_sha256(first) == manifest_sha256(second)
    with pytest.raises(ValueError, match="non-finite"):
        canonical_manifest_bytes({"invalid": float("nan")})
    with pytest.raises(TypeError, match="non-string"):
        canonical_manifest_bytes({"invalid": {1: "value"}})


def test_manifest_store_is_content_addressed_idempotent_and_detects_corruption(
    tmp_path: Path,
) -> None:
    store = BacktestManifestStore(tmp_path / ".artifacts")
    manifest = {"protocol": "registered-campaign-v1", "bars": {"count": 2192}}

    first = store.store(manifest)
    second = store.store({"bars": {"count": 2192}, "protocol": "registered-campaign-v1"})

    assert first == second
    assert second.path.stat().st_mode & 0o777 == 0o444
    assert second.path.parts[-5:-1] == ("research", "manifests", "sha256", second.sha256[:2])
    assert store.read(first.sha256) == manifest

    changed = store.store({"protocol": "registered-campaign-v1", "bars": {"count": 2191}})
    assert changed.sha256 != first.sha256
    assert changed.path != first.path

    first.path.chmod(0o600)
    first.path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="corrupt"):
        store.store(manifest)


def test_trial_registration_requires_exact_catalog_version_and_is_idempotent() -> None:
    engine = _engine()
    experiment_id = _seed_catalog(engine)
    store = BacktestTrialStore(engine)
    registration = _registration(experiment_id)

    first = store.register(registration)
    second = store.register(registration)

    assert first == second == stable_trial_id(registration)
    with Session(engine) as session:
        row = session.get(BacktestTrial, first)
        assert row is not None
        assert row.status == "registered"
        assert row.parameters == {"risk_period": 7, "indicator_period": 30}
        assert session.query(BacktestTrial).count() == 1

    with pytest.raises(ValueError, match="exact strategy version"):
        store.register(_registration(experiment_id, strategy_semver="9.9.9", sequence=1))


def test_trial_lineage_constraints_reject_cross_strategy_rows() -> None:
    engine = _engine()
    primary_experiment_id = _seed_catalog(engine)
    with Session(engine) as session:
        session.add(
            Strategy(
                strategy_id="other_campaign",
                strategy_name="Other Campaign",
                asset_class="crypto",
            )
        )
        session.flush()
        other_version = StrategyVersion(
            strategy_id="other_campaign",
            semver="1.0.0",
            param_schema={},
            default_params={},
        )
        other_experiment = BacktestExperiment(
            strategy_id="other_campaign",
            experiment_type="parameter_sweep",
            metric="sharpe_ratio",
            config={},
        )
        session.add_all([other_version, other_experiment])
        session.flush()
        other_result = BacktestResult(
            strategy_id="other_campaign",
            strat_ver_id=other_version.strat_ver_id,
            experiment_id=other_experiment.experiment_id,
            backtest_id="other-result",
            symbol="BTC/USDC",
            asset_class="crypto",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 2),
            initial_capital=10_000,
            parameters={},
            engine="internal",
        )
        session.add(other_result)
        session.commit()
        other_version_id = other_version.strat_ver_id
        other_experiment_id = other_experiment.experiment_id
        other_result_id = other_result.result_id

    with Session(engine) as session:
        primary_version_id = session.execute(
            select(StrategyVersion.strat_ver_id).where(
                StrategyVersion.strategy_id == "registered_campaign"
            )
        ).scalar_one()

    base = {
        "experiment_id": primary_experiment_id,
        "strategy_id": "registered_campaign",
        "strat_ver_id": primary_version_id,
        "sequence": 0,
        "trial_family": "baseline",
        "fold_id": "full-panel",
        "cost_scenario": "expected",
        "parameters": {},
        "manifest_hash": "b" * 64,
    }
    mismatches = (
        {"experiment_id": other_experiment_id},
        {"strat_ver_id": other_version_id},
        {"result_id": other_result_id},
    )
    for sequence, mismatch in enumerate(mismatches):
        with Session(engine) as session:
            session.add(
                BacktestTrial(
                    trial_id=f"{sequence + 1:064x}",
                    **(base | {"sequence": sequence} | mismatch),
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()


def test_trial_identity_does_not_depend_on_database_surrogate_ids() -> None:
    first = _registration(11)
    recreated_database = _registration(97)

    assert stable_trial_id(first) == stable_trial_id(recreated_database)


def test_trial_sequence_and_identity_cannot_be_overwritten() -> None:
    engine = _engine()
    experiment_id = _seed_catalog(engine)
    store = BacktestTrialStore(engine)
    trial_id = store.register(_registration(experiment_id))

    with pytest.raises(ValueError, match="already belongs"):
        store.register(
            _registration(
                experiment_id,
                cost_scenario="stressed",
            )
        )

    with Session(engine) as session:
        trial = session.get(BacktestTrial, trial_id)
        assert trial is not None
        trial.cost_scenario = "stressed"
        with pytest.raises(ValueError, match="identity is immutable"):
            session.commit()

    with Session(engine) as session:
        trial = session.get(BacktestTrial, trial_id)
        assert trial is not None
        session.delete(trial)
        with pytest.raises(ValueError, match="append-only"):
            session.commit()


def test_trial_lifecycle_is_idempotent_and_retains_first_failure() -> None:
    engine = _engine()
    experiment_id = _seed_catalog(engine)
    store = BacktestTrialStore(engine)
    trial_id = store.register(_registration(experiment_id))

    store.mark_running(trial_id)
    store.mark_running(trial_id)
    store.mark_failed(
        trial_id,
        error_class="DataCoverageError",
        error_context="missing daily bar 2021-03-14",
    )
    store.mark_failed(
        trial_id,
        error_class="DifferentError",
        error_context="must not overwrite original evidence",
    )

    with Session(engine) as session:
        trial = session.get(BacktestTrial, trial_id)
        assert trial is not None
        assert trial.status == "failed"
        assert trial.started_at is not None
        assert trial.completed_at is not None
        assert trial.error_class == "DataCoverageError"
        assert trial.error_context == "missing daily bar 2021-03-14"
    with pytest.raises(ValueError, match="illegal backtest trial transition"):
        store.mark_running(trial_id)


def test_interrupted_trial_remains_terminal_evidence() -> None:
    engine = _engine()
    experiment_id = _seed_catalog(engine)
    store = BacktestTrialStore(engine)
    trial_id = store.register(_registration(experiment_id))

    store.mark_running(trial_id)
    store.mark_interrupted(trial_id, error_context="worker received SIGTERM")

    with Session(engine) as session:
        trial = session.get(BacktestTrial, trial_id)
        assert trial is not None
        assert trial.status == "interrupted"
        assert trial.error_context == "worker received SIGTERM"
    with pytest.raises(ValueError, match="illegal backtest trial transition"):
        store.mark_running(trial_id)


def test_postgres_trial_trigger_compares_json_parameters_as_jsonb() -> None:
    """PostgreSQL ``json`` has no equality operator; lifecycle updates must remain legal."""
    migration = (
        Path(__file__).resolve().parents[4]
        / "scripts"
        / "db"
        / "alembic"
        / "versions"
        / "0042_backtest_trial_provenance.py"
    ).read_text(encoding="utf-8")

    assert "OLD.parameters::jsonb IS DISTINCT FROM NEW.parameters::jsonb" in migration
    assert "OLD.parameters IS DISTINCT FROM NEW.parameters" not in migration


def test_trial_migration_enforces_lineage_and_preserves_nonempty_ledger_on_downgrade() -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "scripts"
        / "db"
        / "alembic"
        / "versions"
        / "0042_backtest_trial_provenance.py"
    ).read_text(encoding="utf-8")

    for constraint in (
        "fk_backtest_trial_experiment_lineage",
        "fk_backtest_trial_version_lineage",
        "fk_backtest_trial_result_lineage",
    ):
        assert constraint in migration
    assert "IF EXISTS (SELECT 1 FROM backtest_trials LIMIT 1)" in migration
    assert "Cannot downgrade 0042: immutable backtest trial evidence exists" in migration
