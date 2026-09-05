"""Per-user broker credential routing (C-1).

Proves that each user's own ``BrokerCredential.secret_ref`` is propagated from the
DB into the scoring profile and resolved on the execution side — instead of every
user collapsing onto a shared ``{broker}-{environment}`` default. Two users with
their own broker accounts must never resolve to the same credential reference.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from execution_engine.broker_bridge import resolve_credential_ref
from execution_engine.config import BrokerType
from lib_application.db.models import (
    Base,
    Broker,
    BrokerCredential,
    LinkedBrokerAccount,
    Strategy,
    User,
    UserStrategyBinding,
    UserStrategyConfig,
)
from lib_application.services.deployment_owner import DeploymentOwnerError
from scoring_engine.providers_db import DBProfileProvider, DBStrategyConfigProvider


def _build_provider() -> DBProfileProvider:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)

    with session_local() as s:
        s.add(
            User(user_id="user-a", email="a@example.com", base_ccy="USD", is_deployment_owner=True)
        )
        s.add(User(user_id="user-b", email="b@example.com", base_ccy="EUR"))
        coinbase = Broker(
            code="coinbase", name="Coinbase", capabilities={"asset_classes": ["crypto"]}
        )
        s.add(coinbase)
        s.flush()
        # Each user gets their own live account on the same broker.
        s.add(
            LinkedBrokerAccount(
                account_id=1,
                user_id="user-a",
                broker_id=coinbase.broker_id,
                environment="live",
                display_name="A Coinbase",
                base_ccy="USD",
                status="connected",
            )
        )
        s.add(
            LinkedBrokerAccount(
                account_id=2,
                user_id="user-b",
                broker_id=coinbase.broker_id,
                environment="live",
                display_name="B Coinbase",
                base_ccy="USD",
                status="connected",
            )
        )
        s.flush()
        s.add(
            BrokerCredential(account_id=1, secret_ref="users/user-a/coinbase/live", status="active")
        )
        s.add(
            BrokerCredential(account_id=2, secret_ref="users/user-b/coinbase/live", status="active")
        )
        # A disabled credential must be ignored.
        s.add(
            BrokerCredential(
                account_id=1, secret_ref="users/user-a/coinbase/STALE", status="disabled"
            )
        )
        s.commit()

    @contextmanager
    def factory() -> Iterator[Session]:
        s = session_local()
        try:
            yield s
        finally:
            s.close()

    return DBProfileProvider(factory)


def test_profile_carries_per_user_active_credential_ref() -> None:
    provider = _build_provider()

    profile_a = provider("user-a")
    with pytest.raises(DeploymentOwnerError, match="deployment owner"):
        provider("user-b")

    # Active per-account secret_ref reaches the per-broker profile entry...
    assert profile_a["brokers"]["coinbase"]["credential_ref"] == "users/user-a/coinbase/live"
    # ...and the default-broker top-level pointer.
    assert profile_a["credential_ref"] == "users/user-a/coinbase/live"

    # Tenant isolation: the two users never share a credential reference.
    assert "2" not in profile_a["accounts"]
    assert "org_id" not in profile_a


def test_execution_resolves_per_user_credential_ref() -> None:
    provider = _build_provider()
    profile_a = provider("user-a")

    resolved = resolve_credential_ref(
        broker_type=BrokerType.COINBASE,
        profile=profile_a,
        user_strategy_config={},
        environment="live",
        broker_account_id=1,
    )
    assert resolved == "users/user-a/coinbase/live"


def test_second_account_at_same_broker_never_receives_the_live_accounts_secret() -> None:
    """Two accounts at one broker: resolution is strictly account-scoped.

    The old broker-code-keyed fallback preferred the connected live account's
    credential_ref, so the OTHER account's route resolved the wrong secret and
    validate_current_authority blocked every execution on it.
    """
    provider = _build_provider()
    profile_a = provider("user-a")

    # Route to an account that carries no credential pointer of its own: the
    # live account's secret must NOT bleed in; live fails to the unresolvable
    # sentinel instead.
    resolved = resolve_credential_ref(
        broker_type=BrokerType.COINBASE,
        profile=profile_a,
        user_strategy_config={},
        environment="live",
        broker_account_id=999,
    )
    assert resolved.startswith("__unresolved__:")
    assert "user-a" not in resolved


def test_live_without_credential_returns_unresolvable_sentinel_not_shared_default() -> None:
    # No per-account credential in the profile + live env must NOT fabricate a
    # guessable shared ref like "coinbase-live" (which would collide users).
    resolved = resolve_credential_ref(
        broker_type=BrokerType.COINBASE,
        profile={},
        user_strategy_config={},
        environment="live",
    )
    assert resolved.startswith("__unresolved__:")
    assert "coinbase-live" not in resolved


def test_paper_without_credential_uses_deterministic_non_secret_ref() -> None:
    resolved = resolve_credential_ref(
        broker_type=BrokerType.COINBASE,
        profile={},
        user_strategy_config={},
        environment="paper",
    )
    assert resolved == "coinbase-paper"


def test_live_account_preferred_over_linked_paper_account() -> None:
    # A separately linked paper account must not hijack a live-configured user.
    # A connected LIVE account for a broker is an explicit opt-in and must win.
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)
    with session_local() as s:
        s.add(
            User(
                user_id="user-live",
                email="live@example.com",
                base_ccy="USDC",
                is_deployment_owner=True,
            )
        )
        cb = Broker(code="coinbase", name="Coinbase", capabilities={"asset_classes": ["crypto"]})
        s.add(cb)
        s.flush()
        s.add(
            LinkedBrokerAccount(
                account_id=1,
                user_id="user-live",
                broker_id=cb.broker_id,
                environment="live",
                display_name="Live",
                base_ccy="USDC",
                status="connected",
            )
        )
        # Explicit paper account for the SAME broker.
        s.add(
            LinkedBrokerAccount(
                account_id=2,
                user_id="user-live",
                broker_id=cb.broker_id,
                environment="paper",
                display_name="Coinbase Paper",
                base_ccy="USD",
                paper_initial_equity=Decimal("100000"),
                paper_initial_cash=Decimal("100000"),
                status="connected",
            )
        )
        s.flush()
        s.add(
            BrokerCredential(
                account_id=1, secret_ref="users/user-live/coinbase/live", status="active"
            )
        )
        s.commit()

    @contextmanager
    def factory() -> Iterator[Session]:
        s = session_local()
        try:
            yield s
        finally:
            s.close()

    profile = DBProfileProvider(factory)("user-live")

    # Coinbase resolves LIVE with the live credential, not the paper account...
    assert profile["brokers"]["coinbase"]["environment"] == "live"
    assert profile["brokers"]["coinbase"]["credential_ref"] == "users/user-live/coinbase/live"
    # ...and the top-level default (drives the broker_route environment) is live too.
    assert profile["broker_environment"] == "live"


def test_expired_credential_is_not_selected() -> None:
    # M-10: an expired (active-but-past-expiry) credential must not be handed to
    # a broker — it resolves to no credential_ref (→ live blocked).
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)
    with session_local() as s:
        s.add(
            User(user_id="user-x", email="x@example.com", base_ccy="USD", is_deployment_owner=True)
        )
        cb = Broker(code="coinbase", name="Coinbase", capabilities={"asset_classes": ["crypto"]})
        s.add(cb)
        s.flush()
        s.add(
            LinkedBrokerAccount(
                account_id=10,
                user_id="user-x",
                broker_id=cb.broker_id,
                environment="live",
                display_name="X Coinbase",
                base_ccy="USD",
                status="connected",
            )
        )
        s.flush()
        s.add(
            BrokerCredential(
                account_id=10,
                secret_ref="users/user-x/coinbase/EXPIRED",
                status="active",
                expires_at=datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(days=1),
            )
        )
        s.commit()

    @contextmanager
    def factory() -> Iterator[Session]:
        s = session_local()
        try:
            yield s
        finally:
            s.close()

    profile = DBProfileProvider(factory)("user-x")
    assert profile["brokers"]["coinbase"]["credential_ref"] is None


def _config_provider_with_binding(allowed_brokers: list[str] | None) -> DBStrategyConfigProvider:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)
    with session_local() as s:
        s.add(
            User(user_id="user-a", email="a@example.com", base_ccy="USD", is_deployment_owner=True)
        )
        s.add(Broker(broker_id=1, code="paper", name="Paper"))
        s.flush()
        s.add(
            LinkedBrokerAccount(
                account_id=1,
                user_id="user-a",
                broker_id=1,
                environment="paper",
                display_name="User A paper",
                base_ccy="USD",
                paper_initial_equity=Decimal("100000"),
                paper_initial_cash=Decimal("100000"),
                status="connected",
            )
        )
        s.add(
            UserStrategyBinding(
                user_id="user-a",
                strategy_id="test_strategy_alpha_v1",
                broker_account_id=1,
                allowed_brokers=allowed_brokers,
                is_active=True,
            )
        )
        s.commit()

    @contextmanager
    def factory() -> Iterator[Session]:
        s = session_local()
        try:
            yield s
        finally:
            s.close()

    return DBStrategyConfigProvider(factory)


def test_single_broker_allow_list_steers_config_broker() -> None:
    # D3: allowed_brokers with EXACTLY ONE broker is a routing steer — the config
    # emits that broker so the route snapshot / execution broker resolver pin it,
    # instead of resolving the profile default and then being vetoed downstream.
    config = _config_provider_with_binding(["paper"])("user-a", "test_strategy_alpha_v1")
    assert config["broker"] == "paper"

    pinned = _config_provider_with_binding(["coinbase"])("user-a", "test_strategy_alpha_v1")
    assert pinned["broker"] == "coinbase"


def test_multi_broker_allow_list_does_not_steer() -> None:
    config = _config_provider_with_binding(["paper", "coinbase"])(
        "user-a", "test_strategy_alpha_v1"
    )
    assert "broker" not in config


def test_empty_allow_list_does_not_steer() -> None:
    assert "broker" not in _config_provider_with_binding([])("user-a", "test_strategy_alpha_v1")
    assert "broker" not in _config_provider_with_binding(None)("user-a", "test_strategy_alpha_v1")


def test_strategy_config_provider_resolves_exact_account_scoped_binding() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)
    with session_local() as session:
        session.add(
            User(
                user_id="multi-user",
                email="multi@example.com",
                base_ccy="EUR",
                is_deployment_owner=True,
            )
        )
        session.add(Strategy(strategy_id="multi-v1", strategy_name="Multi Account"))
        session.add_all(
            [
                Broker(broker_id=1, code="coinbase", name="Coinbase"),
                Broker(broker_id=2, code="ibkr", name="Interactive Brokers"),
            ]
        )
        session.flush()
        session.add_all(
            [
                LinkedBrokerAccount(
                    account_id=101,
                    user_id="multi-user",
                    broker_id=1,
                    environment="paper",
                    display_name="Coinbase paper",
                    base_ccy="EUR",
                    paper_initial_equity=Decimal("100000"),
                    paper_initial_cash=Decimal("100000"),
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=202,
                    user_id="multi-user",
                    broker_id=2,
                    environment="paper",
                    display_name="IBKR paper",
                    base_ccy="EUR",
                    paper_initial_equity=Decimal("100000"),
                    paper_initial_cash=Decimal("100000"),
                    status="connected",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                UserStrategyBinding(
                    binding_id=301,
                    user_id="multi-user",
                    strategy_id="multi-v1",
                    broker_account_id=101,
                    allowed_brokers=["coinbase"],
                    is_active=True,
                ),
                UserStrategyBinding(
                    binding_id=302,
                    user_id="multi-user",
                    strategy_id="multi-v1",
                    broker_account_id=202,
                    allowed_brokers=["ibkr"],
                    is_active=True,
                ),
            ]
        )
        session.commit()

    @contextmanager
    def factory() -> Iterator[Session]:
        session = session_local()
        try:
            yield session
        finally:
            session.close()

    provider = DBStrategyConfigProvider(factory)
    coinbase = provider("multi-user", "multi-v1", 301, 101)
    ibkr = provider("multi-user", "multi-v1", 302, 202)

    assert coinbase["binding_id"] == 301
    assert coinbase["broker_account_id"] == 101
    assert coinbase["broker"] == "coinbase"
    assert ibkr["binding_id"] == 302
    assert ibkr["broker_account_id"] == 202
    assert ibkr["broker"] == "ibkr"
    assert provider("multi-user", "multi-v1", 301, 202) == {}


def _config_provider_with_policy_rows(
    *,
    exact_active: bool,
) -> DBStrategyConfigProvider:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)
    with session_local() as s:
        s.add(
            User(
                user_id="policy-user",
                email="policy@example.com",
                base_ccy="USD",
                is_deployment_owner=True,
            )
        )
        s.add(Broker(broker_id=1, code="paper", name="Paper"))
        s.add(
            Strategy(
                strategy_id="keep_strategy_v1",
                strategy_name="KeepStrategy",
                asset_class="crypto",
            )
        )
        s.add(
            Strategy(
                strategy_id="other_strategy_v1",
                strategy_name="OtherStrategy",
                asset_class="crypto",
            )
        )
        s.flush()
        s.add(
            LinkedBrokerAccount(
                account_id=1,
                user_id="policy-user",
                broker_id=1,
                environment="paper",
                display_name="Policy paper",
                base_ccy="USD",
                paper_initial_equity=Decimal("100000"),
                paper_initial_cash=Decimal("100000"),
                status="connected",
            )
        )
        s.add(
            UserStrategyBinding(
                user_id="policy-user",
                strategy_id="keep_strategy_v1",
                broker_account_id=1,
                preferred_mode="spot",
                execution_modes_allowed=["spot"],
                allowed_brokers=["paper"],
                max_position_pct=0.05,
                max_total_exposure_pct=0.90,
                entry_cash_buffer_bps=25.0,
                is_active=True,
            )
        )
        s.add(
            UserStrategyConfig(
                config_id="policy-exact",
                user_id="policy-user",
                strategy_id="keep_strategy_v1",
                execution_mode="live",
                is_active=exact_active,
                parameters={
                    "require_explicit_scoring_inputs": True,
                    "require_stop_loss": True,
                    "allowed_brokers": ["unreviewed-live-broker"],
                    "risk_caps": {"max_position_pct": 1.0},
                    "api_secret": "must-not-cross-boundary",
                    "unknown_policy": True,
                },
            )
        )
        s.add(
            UserStrategyConfig(
                config_id="policy-mismatch",
                user_id="policy-user",
                strategy_id="other_strategy_v1",
                execution_mode="spot",
                is_active=True,
                parameters={"require_explicit_scoring_inputs": False},
            )
        )
        s.commit()

    @contextmanager
    def factory() -> Iterator[Session]:
        s = session_local()
        try:
            yield s
        finally:
            s.close()

    return DBStrategyConfigProvider(factory)


def test_strategy_policy_allowlist_merges_exact_active_safety_flags_only() -> None:
    config = _config_provider_with_policy_rows(exact_active=True)(
        "policy-user",
        "keep_strategy_v1",
    )

    assert config["require_explicit_scoring_inputs"] is True
    assert config["require_stop_loss"] is True
    assert config["execution_mode"] == "spot"
    assert config["broker"] == "paper"
    assert config["risk_caps"]["max_position_pct"] == 0.05
    assert config["risk_caps"]["max_total_exposure_pct"] == 0.9
    assert config["risk_caps"]["entry_cash_buffer_bps"] == 25.0
    assert "allowed_brokers" not in config
    assert "api_secret" not in config
    assert "unknown_policy" not in config


def test_strategy_policy_ignores_inactive_and_mismatched_config_rows() -> None:
    config = _config_provider_with_policy_rows(exact_active=False)(
        "policy-user",
        "keep_strategy_v1",
    )

    assert "require_explicit_scoring_inputs" not in config
    assert "require_stop_loss" not in config


def test_strategy_config_rejects_caller_selected_foreign_owner() -> None:
    provider = _config_provider_with_binding(["coinbase"])
    with pytest.raises(DeploymentOwnerError, match="deployment owner"):
        provider("foreign-owner")
