"""The scoring read model preserves canonical binding mode policy."""

from __future__ import annotations

from sqlalchemy.orm import Session

from lib_application.db.models import Broker, LinkedBrokerAccount, User, UserStrategyBinding
from scoring_engine.binding_utils import resolve_execution_mode
from scoring_engine.models import ScoringUserBinding
from scoring_engine.storage import AppScoreStore


def _store() -> AppScoreStore:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    with Session(store._engine) as session:
        broker = Broker(code="paper", name="Paper Broker", capabilities={})
        session.add_all([User(user_id="u1", email="u1@example.com", base_ccy="USD"), broker])
        session.flush()
        session.add(
            LinkedBrokerAccount(
                account_id=1,
                user_id="u1",
                broker_id=broker.broker_id,
                environment="paper",
                display_name="U1 paper",
                base_ccy="USD",
                paper_initial_equity=100_000,
                paper_initial_cash=100_000,
                status="connected",
            )
        )
        session.commit()
    return store


def _only(store: AppScoreStore) -> ScoringUserBinding:
    bindings = store.list_bindings()
    assert len(bindings) == 1
    return bindings[0]


def _insert_binding(
    store: AppScoreStore,
    *,
    execution_modes_allowed: list[str] | None = None,
    preferred_mode: str | None = None,
    mode_selection_policy: str = "fixed",
) -> None:
    """Seed the canonical ORM shape; production writes use the backend API."""
    with Session(store._engine) as session:
        session.add(
            UserStrategyBinding(
                user_id="u1",
                strategy_id="mode-policy-strategy",
                broker_account_id=1,
                asset_score_threshold=0.6,
                execution_modes_allowed=execution_modes_allowed or ["spot"],
                preferred_mode=preferred_mode,
                mode_selection_policy=mode_selection_policy,
                is_active=True,
            )
        )
        session.commit()
    store.invalidate_bindings_cache()


def test_resolve_execution_mode_policy_beats_first_allowed() -> None:
    # A ranking policy yields "best" even with a non-empty allow-list, so
    # evaluate_bindings can rank the permitted modes (BD-3).
    assert resolve_execution_mode(None, ["spot", "futures"], "highest_sharpe") == "best"
    # An explicit preferred mode still wins (fixed).
    assert resolve_execution_mode("spot", ["spot", "futures"], "highest_sharpe") == "spot"
    # No policy → first allowed mode.
    assert resolve_execution_mode(None, ["futures", "spot"], "fixed") == "futures"
    assert resolve_execution_mode(None, [], None) is None


def test_fixed_single_mode_round_trip() -> None:
    store = _store()
    _insert_binding(
        store,
        execution_modes_allowed=["futures"],
        preferred_mode="futures",
    )
    b = _only(store)
    assert b.execution_modes_allowed == ["futures"]
    assert b.mode_selection_policy == "fixed"
    # resolve_execution_mode → preferred wins → the fixed single mode.
    assert b.execution_mode == "futures"


def test_default_binding_stays_fixed_spot() -> None:
    # A binding created with no mode fields must remain a fixed spot binding —
    # the DTO default policy must NOT silently turn it into a ranked binding.
    store = _store()
    _insert_binding(store)
    b = _only(store)
    assert b.mode_selection_policy == "fixed"
    assert b.execution_mode == "spot"


def test_policy_binding_resolves_to_best_for_ranking() -> None:
    store = _store()
    _insert_binding(
        store,
        execution_modes_allowed=["spot", "futures"],
        mode_selection_policy="highest_sharpe",
    )
    b = _only(store)
    assert b.execution_modes_allowed == ["spot", "futures"]
    assert b.mode_selection_policy == "highest_sharpe"
    assert b.preferred_mode is None
    # The read path resolves to "best", which is what triggers policy ranking
    # in evaluate_bindings (previously unreachable from the creation path).
    assert b.execution_mode == "best"
