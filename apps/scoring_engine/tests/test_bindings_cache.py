"""Tests for the short-TTL ``list_bindings`` cache on ``AppScoreStore``."""

import pytest
from sqlalchemy.orm import Session

from lib_application.db import models as app_models
from lib_application.db.models import Broker, LinkedBrokerAccount
from lib_application.services.deployment_owner import DeploymentOwnerError
from scoring_engine.storage import AppScoreStore


def _store(monkeypatch, *, ttl_seconds: float = 60.0) -> AppScoreStore:
    del monkeypatch
    store = AppScoreStore(
        "sqlite+pysqlite:///:memory:",
        bindings_cache_ttl_seconds=ttl_seconds,
    )
    with Session(store._engine) as session:
        broker = Broker(code="paper", name="Paper Broker", capabilities={})
        session.add_all(
            [
                app_models.User(
                    user_id="user-1",
                    email="u1@example.com",
                    base_ccy="USD",
                    is_deployment_owner=True,
                ),
                app_models.User(user_id="user-2", email="u2@example.com", base_ccy="EUR"),
                broker,
            ]
        )
        session.flush()
        session.add_all(
            [
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="user-1",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="User 1 paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=2,
                    user_id="user-2",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="User 2 paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
            ]
        )
        session.commit()
    return store


def _deactivate_all_bindings_directly(store: AppScoreStore) -> None:
    """Flip is_active off out-of-process so the cache is intentionally stale."""
    with Session(store._engine) as s:
        for row in s.query(app_models.UserStrategyBinding).all():
            row.is_active = False
        s.commit()


def _insert_binding(store: AppScoreStore, *, user_id: str, account_id: int) -> None:
    """Represent the backend process writing the canonical binding table."""
    with Session(store._engine) as session:
        session.add(
            app_models.UserStrategyBinding(
                user_id=user_id,
                strategy_id="cache-contract-strategy",
                broker_account_id=account_id,
                asset_score_threshold=0.6,
                is_active=True,
            )
        )
        session.commit()


def test_list_bindings_rechecks_revocation_within_ttl(monkeypatch) -> None:
    store = _store(monkeypatch)
    _insert_binding(store, user_id="user-1", account_id=1)

    first = store.list_bindings()
    assert [b.user_id for b in first] == ["user-1"]

    # Mutate the DB behind the cache's back.
    _deactivate_all_bindings_directly(store)

    # Revocation is authoritative even while the projection cache is warm.
    assert store.list_bindings() == []

    # Explicit invalidation forces a re-read that reflects the DB change.
    store.invalidate_bindings_cache()
    assert store.list_bindings() == []


def test_explicit_invalidation_observes_backend_write(monkeypatch) -> None:
    store = _store(monkeypatch)
    _insert_binding(store, user_id="user-1", account_id=1)
    assert [b.user_id for b in store.list_bindings()] == ["user-1"]  # caches the list

    _insert_binding(store, user_id="user-2", account_id=2)
    store.invalidate_bindings_cache()
    assert sorted(b.user_id for b in store.list_bindings()) == ["user-1"]


def test_cache_disabled_when_ttl_zero(monkeypatch) -> None:
    store = _store(monkeypatch, ttl_seconds=0.0)
    _insert_binding(store, user_id="user-1", account_id=1)
    assert [b.user_id for b in store.list_bindings()] == ["user-1"]

    # TTL=0 disables caching, so a direct DB change is reflected immediately.
    _deactivate_all_bindings_directly(store)
    assert store.list_bindings() == []


def test_owner_revocation_is_checked_before_cached_bindings(monkeypatch) -> None:
    store = _store(monkeypatch)
    _insert_binding(store, user_id="user-1", account_id=1)
    assert store.list_bindings()
    with Session(store._engine) as session:
        session.get(app_models.User, "user-1").is_deployment_owner = False
        session.commit()
    with pytest.raises(DeploymentOwnerError):
        store.list_bindings()


def test_failed_refresh_cannot_label_old_grants_as_current(monkeypatch) -> None:
    store = _store(monkeypatch)
    _insert_binding(store, user_id="user-1", account_id=1)
    with Session(store._engine) as session:
        binding = session.query(app_models.UserStrategyBinding).one()
        binding.autopilot = True
        binding.entries_enabled = True
        session.commit()
    assert store.list_bindings()[0].entries_enabled is True
    with Session(store._engine) as session:
        binding = session.query(app_models.UserStrategyBinding).one()
        binding.entries_enabled = False
        session.commit()

    original = store._load_instrument_map

    def unavailable_map(*args, **kwargs):
        raise OSError("Database read interrupted during binding refresh")

    monkeypatch.setattr(store, "_load_instrument_map", unavailable_map)
    with pytest.raises(OSError, match="interrupted"):
        store.list_bindings()
    monkeypatch.setattr(store, "_load_instrument_map", original)
    assert store.list_bindings()[0].entries_enabled is False
