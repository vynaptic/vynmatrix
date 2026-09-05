"""Fail-closed parameter-feedback lifecycle tests for strategy retirement."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from feedback_loop_engine.models import (
    EvaluationHorizon,
    OptimizationMethod,
    OptimizationSuggestion,
    TriggerReason,
)
from feedback_loop_engine.optimizer import ParameterOptimizer, SuggestionGenerationError
from lib_application.db.models import (
    Base,
    Strategy,
    StrategyParameterFeedback,
    StrategyVersion,
)
from lib_infrastructure.persistence.sqlalchemy.repositories.signal_performance_repo import (
    SQLAlchemySignalPerformanceRepository,
)


def _database() -> tuple[object, sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


def _add_strategy_version(
    session_factory: sessionmaker[Session],
    *,
    strategy_id: str = "active_strategy_v1",
    strat_ver_id: int = 101,
    strategy_active: bool = True,
    version_status: str = "active",
    default_params: object | None = None,
) -> None:
    with session_factory() as session:
        session.add(
            Strategy(
                strategy_id=strategy_id,
                strategy_name=strategy_id,
                asset_class="crypto",
                is_active=strategy_active,
            )
        )
        session.add(
            StrategyVersion(
                strat_ver_id=strat_ver_id,
                strategy_id=strategy_id,
                semver="1.0.0",
                param_schema={},
                default_params=({"ema_period": 10} if default_params is None else default_params),
                status=version_status,
            )
        )
        session.commit()


def _suggestion(
    *,
    strategy_id: str = "active_strategy_v1",
    strat_ver_id: int | None = 101,
) -> OptimizationSuggestion:
    return OptimizationSuggestion(
        strategy_id=strategy_id,
        strategy_code=strategy_id,
        strat_ver_id=strat_ver_id,
        instr_id=None,
        symbol=None,
        horizon=EvaluationHorizon.D1,
        trigger_reason=TriggerReason.CONSECUTIVE_WRONG,
        consecutive_wrong_signals=3,
        accuracy_window_days=30,
        accuracy_pct=0.4,
        current_params={"ema_period": 10},
        suggested_params={"ema_period": 15},
        optimization_method=OptimizationMethod.BOUNDED_HEURISTIC,
        explanation="bounded test suggestion",
    )


def test_approval_is_suggestion_only_and_never_writes_strategy_config(tmp_path: Path) -> None:
    engine, session_factory = _database()
    _add_strategy_version(session_factory)
    config_path = tmp_path / "indicator" / "active_strategy_v1" / "config.json"
    config_path.parent.mkdir(parents=True)
    original_config = '{"parameters": {"period": "10"}}'
    config_path.write_text(original_config, encoding="utf-8")

    optimizer = ParameterOptimizer(engine)
    feedback_id = optimizer.persist_suggestion(_suggestion())
    assert feedback_id is not None

    repository = SQLAlchemySignalPerformanceRepository(session_factory=session_factory)
    pending = repository.get_pending_suggestion(feedback_id)
    assert pending is not None
    assert pending.current_params == {"ema_period": 10}
    assert pending.suggested_params == {"ema_period": 15}
    assert repository.approve_suggestion(
        feedback_id=feedback_id,
        reviewer_user_id="reviewer",
    )
    assert repository.get_pending_suggestion(feedback_id) is None
    assert config_path.read_text(encoding="utf-8") == original_config

    with session_factory() as session:
        record = session.get(StrategyParameterFeedback, feedback_id)
        assert record is not None
        assert record.status == "approved"


def test_generation_reads_exact_version_params_and_ignores_filesystem_config(
    tmp_path: Path,
) -> None:
    engine, session_factory = _database()
    _add_strategy_version(
        session_factory,
        default_params={"ema_period": "10", "runner_kind": "signal_worker"},
    )
    config_path = tmp_path / "indicator" / "active_strategy_v1" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"parameters":{"ema_period":"999"}}', encoding="utf-8")

    suggestion = ParameterOptimizer(engine).generate_suggestion(
        strategy_id="active_strategy_v1",
        strategy_code="active_strategy_v1",
        strat_ver_id=101,
        instr_id=10,
        symbol="BTCUSDC",
        trigger_reason=TriggerReason.CONSECUTIVE_WRONG,
        consecutive_wrong=3,
    )

    assert suggestion.current_params == {
        "ema_period": 10,
        "runner_kind": "signal_worker",
    }
    assert suggestion.suggested_params == {
        "ema_period": 15,
        "runner_kind": "signal_worker",
    }
    assert suggestion.supporting_data["changed_parameters"] == ["ema_period"]
    assert config_path.read_text(encoding="utf-8") == ('{"parameters":{"ema_period":"999"}}')


@pytest.mark.parametrize(
    ("strategy_id", "strat_ver_id", "version_status"),
    [
        ("missing_strategy", 101, "active"),
        ("active_strategy_v1", 999, "active"),
        ("active_strategy_v1", 101, "deprecated"),
    ],
)
def test_generation_fails_closed_without_exact_active_version(
    strategy_id: str,
    strat_ver_id: int,
    version_status: str,
) -> None:
    engine, session_factory = _database()
    _add_strategy_version(session_factory, version_status=version_status)

    with pytest.raises(SuggestionGenerationError):
        ParameterOptimizer(engine).generate_suggestion(
            strategy_id=strategy_id,
            strategy_code=strategy_id,
            strat_ver_id=strat_ver_id,
            instr_id=None,
            symbol=None,
            trigger_reason=TriggerReason.CONSECUTIVE_WRONG,
        )


@pytest.mark.parametrize(
    "default_params",
    [
        {},
        [],
        {"ema_period": "invalid"},
        {"ema_period": "nan"},
        {"ema_period": True},
    ],
)
def test_generation_fails_closed_on_invalid_default_params(
    default_params: object,
) -> None:
    engine, session_factory = _database()
    _add_strategy_version(session_factory, default_params=default_params)

    with pytest.raises(SuggestionGenerationError):
        ParameterOptimizer(engine).generate_suggestion(
            strategy_id="active_strategy_v1",
            strategy_code="active_strategy_v1",
            strat_ver_id=101,
            instr_id=None,
            symbol=None,
            trigger_reason=TriggerReason.CONSECUTIVE_WRONG,
        )


def test_generation_fails_closed_when_no_parameter_can_change() -> None:
    engine, session_factory = _database()
    _add_strategy_version(
        session_factory,
        default_params={"runner_kind": "signal_worker"},
    )

    with pytest.raises(SuggestionGenerationError, match="No supported parameter"):
        ParameterOptimizer(engine).generate_suggestion(
            strategy_id="active_strategy_v1",
            strategy_code="active_strategy_v1",
            strat_ver_id=101,
            instr_id=None,
            symbol=None,
            trigger_reason=TriggerReason.CONSECUTIVE_WRONG,
        )


def test_high_drawdown_uses_bounded_risk_adjustment() -> None:
    engine, session_factory = _database()
    _add_strategy_version(
        session_factory,
        default_params={
            "stop_loss_pct": 0.006,
            "take_profit_pct": 0.005,
            "position_size_pct": 0.10,
        },
    )

    suggestion = ParameterOptimizer(engine).generate_suggestion(
        strategy_id="active_strategy_v1",
        strategy_code="active_strategy_v1",
        strat_ver_id=101,
        instr_id=None,
        symbol=None,
        trigger_reason=TriggerReason.HIGH_DRAWDOWN,
    )

    assert suggestion.suggested_params == {
        "stop_loss_pct": 0.0048,
        "take_profit_pct": 0.005,
        "position_size_pct": 0.08,
    }
    assert suggestion.optimization_method is OptimizationMethod.BOUNDED_HEURISTIC


def test_non_actionable_suggestion_is_not_persisted() -> None:
    engine, session_factory = _database()
    _add_strategy_version(session_factory)
    suggestion = _suggestion()
    suggestion.suggested_params = dict(suggestion.current_params)

    assert ParameterOptimizer(engine).persist_suggestion(suggestion) is None
    with session_factory() as session:
        assert session.scalar(select(StrategyParameterFeedback.feedback_id)) is None


def test_stale_or_invalid_suggestion_snapshot_is_not_persisted() -> None:
    engine, session_factory = _database()
    _add_strategy_version(session_factory)
    stale = _suggestion()
    stale.current_params = {"ema_period": 9}
    invalid = _suggestion()
    invalid.suggested_params = {"ema_period": float("nan")}
    optimizer = ParameterOptimizer(engine)

    assert optimizer.persist_suggestion(stale) is None
    assert optimizer.persist_suggestion(invalid) is None
    with session_factory() as session:
        assert session.scalar(select(StrategyParameterFeedback.feedback_id)) is None


@pytest.mark.parametrize(
    ("strategy_active", "version_status", "suggestion_version"),
    [
        (False, "active", 101),
        (True, "deprecated", 101),
        (True, "active", None),
    ],
)
def test_ineligible_strategy_versions_cannot_create_feedback(
    strategy_active: bool,
    version_status: str,
    suggestion_version: int | None,
) -> None:
    engine, session_factory = _database()
    _add_strategy_version(
        session_factory,
        strategy_active=strategy_active,
        version_status=version_status,
    )
    optimizer = ParameterOptimizer(engine)

    assert optimizer.persist_suggestion(_suggestion(strat_ver_id=suggestion_version)) is None
    with session_factory() as session:
        assert session.scalar(select(StrategyParameterFeedback.feedback_id)) is None


def test_version_must_belong_to_the_exact_strategy() -> None:
    engine, session_factory = _database()
    _add_strategy_version(session_factory)
    with session_factory() as session:
        session.add(
            Strategy(
                strategy_id="other_strategy_v1",
                strategy_name="other",
                asset_class="crypto",
                is_active=True,
            )
        )
        session.commit()

    optimizer = ParameterOptimizer(engine)
    assert (
        optimizer.persist_suggestion(_suggestion(strategy_id="other_strategy_v1", strat_ver_id=101))
        is None
    )


def test_expired_feedback_cannot_be_reopened() -> None:
    _engine, session_factory = _database()
    _add_strategy_version(session_factory)
    with session_factory() as session:
        record = StrategyParameterFeedback(
            strategy_id="active_strategy_v1",
            strat_ver_id=101,
            horizon="1d",
            trigger_reason="consecutive_wrong",
            current_params={},
            suggested_params={},
            status="expired",
        )
        session.add(record)
        session.commit()
        feedback_id = int(record.feedback_id)

    repository = SQLAlchemySignalPerformanceRepository(session_factory=session_factory)
    assert not repository.approve_suggestion(
        feedback_id=feedback_id,
        reviewer_user_id="reviewer",
    )
    with session_factory() as session:
        assert session.get(StrategyParameterFeedback, feedback_id).status == "expired"


@pytest.mark.parametrize("status", ["approved", "rejected", "expired"])
def test_only_pending_feedback_can_be_rejected(status: str) -> None:
    _engine, session_factory = _database()
    _add_strategy_version(session_factory)
    with session_factory() as session:
        record = StrategyParameterFeedback(
            strategy_id="active_strategy_v1",
            strat_ver_id=101,
            horizon="1d",
            trigger_reason="consecutive_wrong",
            current_params={},
            suggested_params={},
            status=status,
        )
        session.add(record)
        session.commit()
        feedback_id = int(record.feedback_id)

    repository = SQLAlchemySignalPerformanceRepository(session_factory=session_factory)
    assert not repository.reject_suggestion(
        feedback_id=feedback_id,
        reviewer_user_id="reviewer",
        reason="invalid transition attempt",
    )
    with session_factory() as session:
        record = session.get(StrategyParameterFeedback, feedback_id)
        assert record is not None
        assert record.status == status
        assert record.review_notes is None


@pytest.mark.parametrize(
    ("strategy_active", "version_status", "feedback_version"),
    [
        (False, "active", 101),
        (True, "deprecated", 101),
        (True, "active", None),
    ],
)
def test_ineligible_feedback_cannot_be_approved(
    strategy_active: bool,
    version_status: str,
    feedback_version: int | None,
) -> None:
    _engine, session_factory = _database()
    _add_strategy_version(
        session_factory,
        strategy_active=strategy_active,
        version_status=version_status,
    )
    with session_factory() as session:
        record = StrategyParameterFeedback(
            strategy_id="active_strategy_v1",
            strat_ver_id=feedback_version,
            horizon="1d",
            trigger_reason="consecutive_wrong",
            current_params={},
            suggested_params={},
            status="pending",
        )
        session.add(record)
        session.commit()
        feedback_id = int(record.feedback_id)

    repository = SQLAlchemySignalPerformanceRepository(session_factory=session_factory)
    assert not repository.approve_suggestion(
        feedback_id=feedback_id,
        reviewer_user_id="reviewer",
    )
    with session_factory() as session:
        assert session.get(StrategyParameterFeedback, feedback_id).status == "pending"
