"""Config API tests (CRUD + broker onboarding + admin auth).

RLS tenant isolation is Postgres-only and verified separately against a live
``vm_backend_login`` connection; ``tenant_scope`` no-ops on the sqlite fixture here, so these
tests exercise the application logic, secret-write wiring, and auth gate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from backend.api import BindingIn, create_app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lib_application.db.models import (
    ApiAuditLog,
    Base,
    Broker,
    BrokerCredential,
    BrokerEnvironment,
    Instrument,
    LinkedBrokerAccount,
    MarketCalendar,
    MarketSession,
    RiskMandate,
    Strategy,
    StrategyVersion,
    User,
    UserStrategyConfig,
)
from lib_infrastructure.brokers import EnvSecretsProvider

ADMIN_KEY = "test-admin-key"
AUTH = {"X-Admin-Key": ADMIN_KEY}


def test_binding_input_canonicalizes_and_deduplicates_asset_classes() -> None:
    payload = BindingIn(
        broker_account_id=1,
        asset_classes_allowed=["forex", "fx", "commodity", "indices", "ETF"],
    )

    assert payload.asset_classes_allowed == ["fx", "commodities", "index", "etf"]


def test_binding_input_rejects_unknown_asset_class() -> None:
    with pytest.raises(ValueError, match="Unsupported binding asset class"):
        BindingIn(
            broker_account_id=1,
            asset_classes_allowed=["stock"],
        )


def _memory_engine() -> Any:
    # StaticPool + shared connection so the schema is visible across TestClient's
    # request threads (a fresh :memory: connection would be an empty DB).
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


class _FakeSecrets:
    """In-memory stand-in for DbSecretsProvider (records writes)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set_secret(
        self,
        secret_ref: str,
        plaintext: str,
        *,
        account_id: int,
        session: Any,
    ) -> None:
        del account_id, session
        self.store[secret_ref] = plaintext

    def get_secret(self, secret_ref: str) -> str | None:
        return self.store.get(secret_ref)


@pytest.fixture
def env() -> dict[str, Any]:
    engine = _memory_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as s:
        s.add(
            User(
                user_id="demo_user",
                email="demo_user@example.invalid",
                base_ccy="EUR",
                is_deployment_owner=True,
            )
        )
        s.add(User(user_id="demo_peer", email="demo_peer@example.invalid", base_ccy="INR"))
        s.add_all(
            [
                Broker(broker_id=1, code="coinbase", name="Coinbase"),
                Broker(broker_id=2, code="deribit", name="Deribit"),
                Broker(broker_id=3, code="delta", name="Delta Exchange"),
                Broker(broker_id=4, code="ibkr", name="Interactive Brokers"),
                Broker(broker_id=5, code="zerodha", name="Zerodha"),
                Broker(broker_id=6, code="paper", name="Paper"),
                Broker(broker_id=7, code="saxo", name="Saxo Bank"),
            ]
        )
        environment_rows = [
            (1, 1, "paper", "US"),
            (2, 1, "live", "US"),
            (3, 2, "paper", "global"),
            (4, 2, "live", "global"),
            (5, 3, "paper", "global"),
            (6, 3, "paper", "india"),
            (7, 3, "live", "global"),
            (8, 3, "live", "india"),
            (9, 4, "paper", "US"),
            (10, 4, "live", "US"),
            (11, 5, "paper", "IN"),
            (12, 5, "live", "IN"),
            (13, 6, "paper", "global"),
            (14, 7, "paper", "global"),
            (15, 7, "live", "global"),
        ]
        s.add_all(
            [
                BrokerEnvironment(
                    broker_env_id=broker_env_id,
                    broker_id=broker_id,
                    environment=environment,
                    region=region,
                    base_urls={},
                    rate_limits={},
                )
                for broker_env_id, broker_id, environment, region in environment_rows
            ]
        )
        s.add_all(
            [
                Instrument(
                    instr_id=100,
                    asset_class="equity",
                    canonical="AAPL",
                    exchange="XNAS",
                    settlement_currency="USD",
                ),
                Instrument(
                    instr_id=101,
                    asset_class="crypto",
                    canonical="BTC/USD",
                    exchange="COINBASE",
                    settlement_currency="USD",
                ),
            ]
        )
        s.add(
            LinkedBrokerAccount(
                account_id=1,
                user_id="demo_user",
                broker_id=1,
                environment="paper",
                display_name="Demo User paper",
                base_ccy="EUR",
                paper_initial_equity=Decimal("100000"),
                paper_initial_cash=Decimal("100000"),
                status="connected",
            )
        )
        s.add(
            Strategy(
                strategy_id="swing_high_low_pmo_v1",
                strategy_name="Swing High Low PMO",
                asset_class="crypto",
            )
        )
        s.add(
            Strategy(
                strategy_id="test_strategy_alpha_v1",
                strategy_name="EMA Cross Scalper",
                asset_class="crypto",
            )
        )
        s.add(
            Strategy(
                strategy_id="us_quality_compounder_v1",
                strategy_name="SP500 Quality Momentum Rotation",
                asset_class="equity",
                is_active=True,
            )
        )
        s.add(
            StrategyVersion(
                strat_ver_id=100,
                strategy_id="us_quality_compounder_v1",
                semver="1.2.0",
                param_schema={},
                default_params={},
                status="active",
            )
        )
        s.commit()
    secrets = _FakeSecrets()
    app = create_app(session_factory=factory, secrets_provider=secrets, admin_api_key=ADMIN_KEY)
    return {"client": TestClient(app), "factory": factory, "secrets": secrets}


def test_health_needs_no_auth(env: dict[str, Any]) -> None:
    assert env["client"].get("/health").json() == {"status": "ok"}


def test_admin_key_enforced(env: dict[str, Any]) -> None:
    assert env["client"].get("/bindings").status_code == 401


def test_wrong_admin_key_rejected(env: dict[str, Any]) -> None:
    r = env["client"].get("/bindings", headers={"X-Admin-Key": "wrong-key"})
    assert r.status_code == 401


def _xnas_calendar_payload() -> dict[str, Any]:
    return {
        "source_kind": "exchange",
        "provider": "nasdaq",
        "source_reference": "https://www.nasdaqtrader.com/Trader.aspx?id=Calendar",
        "observed_at": "2025-01-02T22:00:00+00:00",
        "coverage_start": "2025-01-02T00:00:00+00:00",
        "coverage_end": "2025-01-03T00:00:00+00:00",
        "sessions": [
            {
                "opens_at": "2025-01-02T14:30:00+00:00",
                "closes_at": "2025-01-02T21:00:00+00:00",
            }
        ],
        "instrument_ids": [100],
    }


def test_market_calendar_sync_requires_admin_auth(env: dict[str, Any]) -> None:
    response = env["client"].put(
        "/market-calendars/XNAS",
        json=_xnas_calendar_payload(),
    )

    assert response.status_code == 401


def test_market_calendar_sync_atomically_persists_official_coverage(
    env: dict[str, Any],
) -> None:
    response = env["client"].put(
        "/market-calendars/XNAS",
        json=_xnas_calendar_payload(),
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert response.json()["code"] == "XNAS"
    assert response.json()["provider"] == "nasdaq"
    assert response.json()["session_count"] == 1
    assert response.json()["instrument_ids"] == [100]
    with env["factory"]() as session:
        calendar = session.query(MarketCalendar).filter_by(code="XNAS").one()
        instrument = session.get(Instrument, 100)
        assert instrument is not None
        assert instrument.market_calendar_id == calendar.calendar_id
        assert session.query(MarketSession).filter_by(calendar_id=calendar.calendar_id).count() == 1


def test_market_calendar_empty_complete_coverage_means_closed(
    env: dict[str, Any],
) -> None:
    payload = {
        **_xnas_calendar_payload(),
        "observed_at": "2025-01-01T23:00:00+00:00",
        "coverage_start": "2025-01-01T00:00:00+00:00",
        "coverage_end": "2025-01-02T00:00:00+00:00",
        "sessions": [],
    }
    response = env["client"].put(
        "/market-calendars/XNAS",
        json=payload,
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert response.json()["session_count"] == 0


@pytest.mark.parametrize(
    ("override", "expected_detail"),
    [
        (
            {
                "source_reference": "http://calendar.invalid",
            },
            "HTTPS",
        ),
        (
            {
                "observed_at": "2099-01-01T00:00:00+00:00",
            },
            "future",
        ),
        (
            {
                "instrument_ids": [999],
            },
            "unknown instrument",
        ),
        (
            {
                "instrument_ids": [101],
            },
            "Crypto",
        ),
        (
            {
                "sessions": [
                    {
                        "opens_at": "2025-01-02T14:30:00+00:00",
                        "closes_at": "2025-01-02T19:00:00+00:00",
                    },
                    {
                        "opens_at": "2025-01-02T18:00:00+00:00",
                        "closes_at": "2025-01-02T21:00:00+00:00",
                    },
                ]
            },
            "overlap",
        ),
    ],
)
def test_market_calendar_sync_rejects_untrusted_or_inconsistent_batches(
    env: dict[str, Any],
    override: dict[str, Any],
    expected_detail: str,
) -> None:
    response = env["client"].put(
        "/market-calendars/XNAS",
        json={**_xnas_calendar_payload(), **override},
        headers=AUTH,
    )

    assert response.status_code == 400
    assert expected_detail.lower() in response.text.lower()


def _bare_factory() -> Any:
    engine = _memory_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_missing_admin_key_fails_closed_at_startup() -> None:
    # No key + no explicit anon opt-out → the app must refuse to build.
    with pytest.raises(RuntimeError, match="BACKEND_ADMIN_API_KEY"):
        create_app(session_factory=_bare_factory(), admin_api_key="", allow_anon=False)


def test_missing_admin_key_fails_closed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Env-driven path: unset key + unset opt-out (defaults to false) → refuse.
    monkeypatch.delenv("BACKEND_ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("BACKEND_ALLOW_ANON", raising=False)
    with pytest.raises(RuntimeError, match="refusing to start"):
        create_app(session_factory=_bare_factory())


def test_allow_anon_opt_out_permits_unauthenticated_dev_access() -> None:
    # Explicit local-dev escape hatch: no key + BACKEND_ALLOW_ANON=true → open.
    app = create_app(session_factory=_bare_factory(), admin_api_key="", allow_anon=True)
    client = TestClient(app)
    r = client.get("/bindings")
    assert r.status_code == 503
    assert "deployment owner" in r.json()["detail"]


def test_binding_create_update_list_deactivate(env: dict[str, Any]) -> None:
    client = env["client"]
    # Create
    r = client.post(
        "/bindings",
        json={
            "strategy_id": "swing_high_low_pmo_v1",
            "broker_account_id": 1,
            "asset_score_threshold": 0.7,
            "max_total_exposure_pct": 0.75,
            "max_open_positions": 3,
            "entry_cash_buffer_bps": 25.0,
        },
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    binding_id = r.json()["binding_id"]
    assert r.json()["asset_score_threshold"] == 0.7
    assert r.json()["max_total_exposure_pct"] == 0.75
    assert r.json()["entry_cash_buffer_bps"] == 25.0
    assert r.json()["autopilot"] is False
    assert r.json()["is_active"] is False

    # Update the same explicit strategy/account binding.
    r2 = client.post(
        "/bindings",
        json={
            "strategy_id": "swing_high_low_pmo_v1",
            "broker_account_id": 1,
            "asset_score_threshold": 0.9,
        },
        headers=AUTH,
    )
    assert r2.json()["binding_id"] == binding_id
    assert r2.json()["asset_score_threshold"] == 0.9
    assert r2.json()["max_total_exposure_pct"] == 0.5
    assert r2.json()["entry_cash_buffer_bps"] is None
    assert r2.json()["autopilot"] is False
    assert r2.json()["is_active"] is False

    # Activation is an explicit operator decision, never an omitted-field default.
    activated = client.post(
        "/bindings",
        json={
            "strategy_id": "swing_high_low_pmo_v1",
            "broker_account_id": 1,
            "asset_score_threshold": 0.9,
            "autopilot": True,
            "is_active": True,
            "entries_enabled": True,
            "exits_enabled": True,
        },
        headers=AUTH,
    )
    assert activated.json()["binding_id"] == binding_id
    assert activated.json()["autopilot"] is True
    assert activated.json()["is_active"] is True
    assert activated.json()["entries_enabled"] is True
    assert activated.json()["exits_enabled"] is True

    # List
    listing = client.get("/bindings", headers=AUTH).json()
    assert len(listing) == 1
    assert listing[0]["binding_id"] == binding_id

    # Deactivate
    d = client.delete(f"/bindings/{binding_id}", headers=AUTH)
    assert d.json() == {
        "binding_id": binding_id,
        "is_active": False,
        "entries_enabled": False,
        "exits_enabled": False,
    }


def test_binding_paths_cannot_cross_tenants_without_rls(env: dict[str, Any]) -> None:
    client = env["client"]
    created = client.post(
        "/bindings",
        json={"broker_account_id": 1},
        headers=AUTH,
    )
    binding_id = created.json()["binding_id"]

    assert client.get("/users/demo_peer/bindings", headers=AUTH).status_code == 404
    response = client.delete(f"/users/demo_peer/bindings/{binding_id}", headers=AUTH)
    assert response.status_code == 404

    owner_listing = client.get("/bindings", headers=AUTH).json()
    assert owner_listing[0]["is_active"] is False
    assert owner_listing[0]["autopilot"] is False


def test_strategy_config_upsert_review_deactivate_is_tenant_scoped_and_audited(
    env: dict[str, Any],
) -> None:
    client = env["client"]
    path = "/strategy-configs/us_quality_compounder_v1"

    created = client.put(
        path,
        json={
            "execution_mode": "spot",
            "is_active": True,
            "parameters": {
                "require_stop_loss": False,
                "require_explicit_scoring_inputs": True,
            },
        },
        headers=AUTH,
    )
    assert created.status_code == 200, created.text
    config_id = created.json()["config_id"]
    assert created.json()["parameters"] == {
        "require_stop_loss": False,
        "require_explicit_scoring_inputs": True,
    }
    assert created.json()["is_active"] is True

    updated = client.put(
        path,
        json={
            "execution_mode": "paper",
            "is_active": True,
            "parameters": {
                "require_stop_loss": False,
                "require_explicit_scoring_inputs": True,
            },
        },
        headers=AUTH,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["config_id"] == config_id
    assert updated.json()["execution_mode"] == "paper"

    listing = client.get("/strategy-configs", headers=AUTH)
    assert listing.status_code == 200
    assert [row["config_id"] for row in listing.json()] == [config_id]
    assert client.get("/users/demo_peer/strategy-configs", headers=AUTH).status_code == 404

    cross_tenant_delete = client.delete(
        "/users/demo_peer/strategy-configs/us_quality_compounder_v1",
        headers=AUTH,
    )
    assert cross_tenant_delete.status_code == 404

    deactivated = client.delete(path, headers=AUTH)
    assert deactivated.status_code == 200
    assert deactivated.json()["config_id"] == config_id
    assert deactivated.json()["is_active"] is False

    with env["factory"]() as session:
        rows = session.query(UserStrategyConfig).all()
        assert len(rows) == 1
        assert rows[0].user_id == "demo_user"
        assert rows[0].parameters == {
            "require_stop_loss": False,
            "require_explicit_scoring_inputs": True,
        }
        audits = (
            session.query(ApiAuditLog)
            .filter(ApiAuditLog.user_id == "demo_user")
            .order_by(ApiAuditLog.audit_id.asc())
            .all()
        )
        assert [row.action for row in audits] == [
            "strategy_config.upsert",
            "strategy_config.upsert",
            "strategy_config.deactivate",
        ]
        assert all(row.status == "ok" for row in audits)


def test_strategy_config_rejects_unreviewed_parameters_and_inactive_catalogue(
    env: dict[str, Any],
) -> None:
    client = env["client"]
    path = "/strategy-configs/us_quality_compounder_v1"

    unreviewed = client.put(
        path,
        json={
            "parameters": {
                "require_stop_loss": False,
                "api_secret": "must-not-be-stored",
            }
        },
        headers=AUTH,
    )
    assert unreviewed.status_code == 422
    assert "api_secret" not in str(client.get("/strategy-configs", headers=AUTH).json())
    coerced_switch = client.put(
        path,
        json={"parameters": {"require_stop_loss": "false"}},
        headers=AUTH,
    )
    assert coerced_switch.status_code == 422

    with env["factory"]() as session:
        strategy = session.get(Strategy, "us_quality_compounder_v1")
        assert strategy is not None
        strategy.is_active = False
        session.commit()
    inactive = client.put(path, json={"is_active": True}, headers=AUTH)
    assert inactive.status_code == 409
    assert inactive.json()["detail"] == "strategy is not active"


def test_drawdown_mandate_upsert_is_append_only_tenant_owned_and_audited(
    env: dict[str, Any],
) -> None:
    client = env["client"]
    path = "/risk-mandates/drawdown"

    created = client.put(path, json={"max_drawdown_pct": "0.20"}, headers=AUTH)
    assert created.status_code == 200, created.text
    first_id = created.json()["mandate_id"]
    assert Decimal(str(created.json()["max_drawdown_pct"])) == Decimal("0.20")

    repeated = client.put(path, json={"max_drawdown_pct": "0.20"}, headers=AUTH)
    assert repeated.status_code == 200
    assert repeated.json()["mandate_id"] == first_id

    tightened = client.put(path, json={"max_drawdown_pct": "0.15"}, headers=AUTH)
    assert tightened.status_code == 200
    assert tightened.json()["mandate_id"] != first_id

    loosened = client.put(path, json={"max_drawdown_pct": "0.20"}, headers=AUTH)
    assert loosened.status_code == 409
    assert "cannot loosen" in loosened.json()["detail"]

    listing = client.get(path, headers=AUTH)
    assert listing.status_code == 200
    assert [Decimal(str(row["max_drawdown_pct"])) for row in listing.json()] == [
        Decimal("0.15"),
        Decimal("0.20"),
    ]
    assert client.get("/users/demo_peer/risk-mandates/drawdown", headers=AUTH).status_code == 404

    with env["factory"]() as session:
        mandates = session.query(RiskMandate).order_by(RiskMandate.mandate_id.asc()).all()
        assert len(mandates) == 2
        assert {row.user_id for row in mandates} == {"demo_user"}
        audits = (
            session.query(ApiAuditLog)
            .filter(ApiAuditLog.action == "risk_mandate.drawdown_upsert")
            .order_by(ApiAuditLog.audit_id.asc())
            .all()
        )
        assert [row.resp["outcome"] for row in audits] == [
            "created",
            "unchanged",
            "created",
        ]


@pytest.mark.parametrize(
    "payload",
    [
        {"max_drawdown_pct": "0.0499"},
        {"max_drawdown_pct": "0.5001"},
        {"max_drawdown_pct": "0.12345"},
        {"max_drawdown_pct": "0.20", "unreviewed_rule": True},
    ],
)
def test_drawdown_mandate_rejects_invalid_or_unreviewed_rules(
    env: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    response = env["client"].put(
        "/risk-mandates/drawdown",
        json=payload,
        headers=AUTH,
    )

    assert response.status_code == 422


def test_strategy_policy_and_drawdown_mandate_routes_require_admin_auth(
    env: dict[str, Any],
) -> None:
    client = env["client"]

    assert client.get("/strategy-configs").status_code == 401
    assert (
        client.put(
            "/risk-mandates/drawdown",
            json={"max_drawdown_pct": "0.20"},
        ).status_code
        == 401
    )


def test_binding_rejects_overlapping_active_strategy_scope(env: dict[str, Any]) -> None:
    client = env["client"]
    first = client.post(
        "/bindings",
        json={
            "strategy_id": "swing_high_low_pmo_v1",
            "broker_account_id": 1,
            "instruments_allowed": ["BTC-USD"],
            "is_active": True,
            "autopilot": False,
            "entries_enabled": False,
            "exits_enabled": True,
        },
        headers=AUTH,
    )
    assert first.status_code == 200, first.text
    assert first.json()["instruments_allowed"] == ["BTC/USD"]
    assert first.json()["entries_enabled"] is False
    assert first.json()["exits_enabled"] is True

    conflicting = client.post(
        "/bindings",
        json={
            "strategy_id": "test_strategy_alpha_v1",
            "broker_account_id": 1,
            "instruments_allowed": ["101"],
            "is_active": True,
            "autopilot": True,
            "entries_enabled": True,
            "exits_enabled": True,
        },
        headers=AUTH,
    )

    assert conflicting.status_code == 409
    assert "overlapping instrument scope" in conflicting.json()["detail"]


def test_onboard_broker_account_stores_encrypted_secret(env: dict[str, Any]) -> None:
    client, secrets = env["client"], env["secrets"]
    r = client.post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "coinbase",
            "environment": "live",
            "credentials": {
                "api_key": "AK-SECRET",
                "api_secret": "SK-SECRET",
            },
            "base_ccy": "EUR",
        },
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    secret_ref = body["secret_ref"]
    assert secret_ref == f"users/demo_user/broker-accounts/{body['account_id']}"
    # Key material is stored via the secret provider, never returned in the response.
    assert "AK-SECRET" not in r.text
    assert "SK-SECRET" not in r.text
    assert "AK-SECRET" in secrets.store[secret_ref]

    listing = client.get("/broker-accounts", headers=AUTH).json()
    onboarded = next(row for row in listing if row["account_id"] == body["account_id"])
    assert onboarded["secret_ref"] == secret_ref
    assert onboarded["broker_code"] == "coinbase"
    with env["factory"]() as session:
        account = session.get(LinkedBrokerAccount, body["account_id"])
        assert account is not None
        assert account.base_ccy == "EUR"


@pytest.mark.parametrize("base_ccy", [None, "", "usd", "US", "USD-EUR"])
def test_onboard_requires_canonical_base_currency(
    env: dict[str, Any],
    base_ccy: str | None,
) -> None:
    payload: dict[str, Any] = {
        "config_key": "primary",
        "broker_code": "coinbase",
        "environment": "live",
        "credentials": {"api_key": "x", "api_secret": "y"},
    }
    if base_ccy is not None:
        payload["base_ccy"] = base_ccy

    response = env["client"].post(
        "/broker-accounts",
        json=payload,
        headers=AUTH,
    )

    assert response.status_code == 422


def test_onboard_paper_account_requires_explicit_capital(env: dict[str, Any]) -> None:
    payload = {
        "config_key": "primary",
        "broker_code": "coinbase",
        "environment": "paper",
        "credentials": {},
        "base_ccy": "EUR",
    }

    missing = env["client"].post(
        "/broker-accounts",
        json=payload,
        headers=AUTH,
    )
    assert missing.status_code == 422

    invalid = env["client"].post(
        "/broker-accounts",
        json={
            **payload,
            "paper_initial_equity": "10000",
            "paper_initial_cash": "11000",
        },
        headers=AUTH,
    )
    assert invalid.status_code == 422

    created = env["client"].post(
        "/broker-accounts",
        json={
            **payload,
            "paper_initial_equity": "10000",
            "paper_initial_cash": "8000",
        },
        headers=AUTH,
    )
    assert created.status_code == 200, created.text
    assert created.json()["paper_initial_equity"] == "10000"
    assert created.json()["paper_initial_cash"] == "8000"


def test_onboard_live_account_rejects_local_paper_capital(env: dict[str, Any]) -> None:
    response = env["client"].post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "coinbase",
            "environment": "live",
            "credentials": {
                "api_key": "live-key",
                "api_secret": "live-secret",
            },
            "base_ccy": "EUR",
            "paper_initial_equity": "10000",
            "paper_initial_cash": "10000",
        },
        headers=AUTH,
    )

    assert response.status_code == 422


def test_onboard_unknown_broker_404(env: dict[str, Any]) -> None:
    r = client_post_unknown(env)
    assert r.status_code == 404


def client_post_unknown(env: dict[str, Any]) -> Any:
    return env["client"].post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "nope",
            "environment": "live",
            "credentials": {"api_key": "x", "api_secret": "y"},
            "base_ccy": "EUR",
        },
        headers=AUTH,
    )


def test_env_secrets_backend_keeps_reads_available_and_rejects_writes_without_mutation(
    env: dict[str, Any],
) -> None:
    factory = env["factory"]
    app = create_app(
        session_factory=factory,
        secrets_provider=EnvSecretsProvider(prefix="TEST_BROKER_CREDS"),
        admin_api_key=ADMIN_KEY,
    )
    client = TestClient(app)
    expected_detail = (
        "credential writes are unavailable with the configured secrets backend; "
        "set SECRETS_BACKEND=db and configure SECRETS_MASTER_KEYS"
    )
    with factory() as session:
        before = (
            session.query(LinkedBrokerAccount).count(),
            session.query(BrokerCredential).count(),
        )

    bindings = client.get("/bindings", headers=AUTH)
    accounts = client.get("/broker-accounts", headers=AUTH)
    onboard = client.post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "coinbase",
            "environment": "live",
            "credentials": {"api_key": "x", "api_secret": "y"},
            "base_ccy": "EUR",
        },
        headers=AUTH,
    )
    rotate = client.put(
        "/broker-accounts/1/credentials",
        json={"api_key": "x", "api_secret": "y"},
        headers=AUTH,
    )

    assert bindings.status_code == 200
    assert accounts.status_code == 200
    assert len(accounts.json()) == 1
    assert onboard.status_code == 503
    assert onboard.json() == {"detail": expected_detail}
    assert rotate.status_code == 503
    assert rotate.json() == {"detail": expected_detail}
    with factory() as session:
        after = (
            session.query(LinkedBrokerAccount).count(),
            session.query(BrokerCredential).count(),
        )
    assert after == before


def test_local_paper_account_requires_no_external_credentials(env: dict[str, Any]) -> None:
    response = env["client"].post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "paper",
            "environment": "paper",
            "credentials": {},
            "base_ccy": "EUR",
            "paper_initial_equity": "25000",
            "paper_initial_cash": "25000",
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert response.json()["secret_ref"] == ""
    assert env["secrets"].store == {}
    with env["factory"]() as session:
        assert (
            session.query(BrokerCredential)
            .filter_by(account_id=response.json()["account_id"])
            .count()
            == 0
        )


def test_linked_paper_account_rejects_remote_sandbox_credentials(
    env: dict[str, Any],
) -> None:
    response = env["client"].post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "coinbase",
            "environment": "paper",
            "credentials": {
                "api_key": "sandbox-key",
                "api_secret": "sandbox-secret",
            },
            "base_ccy": "EUR",
            "paper_initial_equity": "25000",
            "paper_initial_cash": "25000",
        },
        headers=AUTH,
    )

    assert response.status_code == 422
    assert "unsupported fields" in response.text


def test_local_paper_broker_cannot_be_onboarded_as_live(env: dict[str, Any]) -> None:
    response = env["client"].post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "paper",
            "environment": "live",
            "credentials": {},
            "base_ccy": "EUR",
        },
        headers=AUTH,
    )

    assert response.status_code == 422
    assert "does not support environment" in response.text


def test_delta_onboarding_requires_explicit_region(env: dict[str, Any]) -> None:
    base_payload = {
        "config_key": "primary",
        "broker_code": "delta",
        "environment": "live",
        "base_ccy": "INR",
    }
    missing = env["client"].post(
        "/broker-accounts",
        json={
            **base_payload,
            "credentials": {"api_key": "delta-key", "api_secret": "delta-secret"},
        },
        headers=AUTH,
    )
    assert missing.status_code == 422
    assert "region" in missing.text

    created = env["client"].post(
        "/broker-accounts",
        json={
            **base_payload,
            "credentials": {
                "api_key": "delta-key",
                "api_secret": "delta-secret",
                "region": "india",
            },
        },
        headers=AUTH,
    )
    assert created.status_code == 200, created.text
    stored = json.loads(env["secrets"].store[created.json()["secret_ref"]])
    assert stored["region"] == "india"


def test_ibkr_onboarding_uses_account_session_credentials(env: dict[str, Any]) -> None:
    missing_tls = env["client"].post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "ibkr",
            "environment": "live",
            "base_ccy": "EUR",
            "credentials": {
                "subaccount": "U1234567",
                "gateway_url": "https://localhost:5000",
            },
        },
        headers=AUTH,
    )
    assert missing_tls.status_code == 422
    assert "ca_cert" in missing_tls.text

    created = env["client"].post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "ibkr",
            "environment": "live",
            "base_ccy": "EUR",
            "credentials": {
                "subaccount": "U1234567",
                "gateway_url": "https://localhost:5000",
                "ca_cert": "/run/secrets/ibkr-ca.pem",
            },
        },
        headers=AUTH,
    )
    assert created.status_code == 200, created.text
    stored = json.loads(env["secrets"].store[created.json()["secret_ref"]])
    assert "api_key" not in stored
    assert stored["subaccount"] == "U1234567"


@pytest.mark.parametrize(
    "expires_at",
    [
        "2035-01-01T00:00:00",
        "2020-01-01T00:00:00+00:00",
    ],
)
def test_zerodha_rejects_unusable_access_token_expiry(
    env: dict[str, Any],
    expires_at: str,
) -> None:
    response = env["client"].post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "zerodha",
            "environment": "live",
            "base_ccy": "INR",
            "credentials": {
                "api_key": "kite-key",
                "api_secret": "kite-secret",
                "access_token": "daily-access-token",
                "access_token_expires_at": expires_at,
            },
        },
        headers=AUTH,
    )

    assert response.status_code == 422


def test_zerodha_persists_current_access_token_expiry(env: dict[str, Any]) -> None:
    expiry = datetime.now(tz=UTC) + timedelta(hours=8)
    response = env["client"].post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "zerodha",
            "environment": "live",
            "base_ccy": "INR",
            "credentials": {
                "api_key": "kite-key",
                "api_secret": "kite-secret",
                "access_token": "daily-access-token",
                "access_token_expires_at": expiry.isoformat(),
            },
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    with env["factory"]() as session:
        credential = (
            session.query(BrokerCredential)
            .filter(BrokerCredential.account_id == response.json()["account_id"])
            .one()
        )
        assert credential.expires_at == expiry.replace(tzinfo=None)


def _saxo_credentials(*, suffix: str) -> dict[str, str]:
    access_expiry = datetime.now(tz=UTC) + timedelta(hours=1)
    refresh_expiry = access_expiry + timedelta(days=30)
    return {
        "api_key": f"app-key-{suffix}",
        "api_secret": f"app-secret-{suffix}",
        "access_token": f"access-token-{suffix}",
        "access_token_expires_at": access_expiry.isoformat(),
        "refresh_token": f"refresh-token-{suffix}",
        "refresh_token_expires_at": refresh_expiry.isoformat(),
        "account_key": f"account-key-{suffix}",
        "client_key": f"client-key-{suffix}",
    }


def test_saxo_rotation_atomically_replaces_complete_snapshot(env: dict[str, Any]) -> None:
    original = _saxo_credentials(suffix="old")
    created = env["client"].post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "saxo",
            "environment": "live",
            "base_ccy": "EUR",
            "credentials": original,
        },
        headers=AUTH,
    )
    assert created.status_code == 200, created.text
    account_id = created.json()["account_id"]
    secret_ref = created.json()["secret_ref"]

    incomplete = env["client"].put(
        f"/broker-accounts/{account_id}/credentials",
        json={"access_token": "partial-token"},
        headers=AUTH,
    )
    assert incomplete.status_code == 422
    assert json.loads(env["secrets"].store[secret_ref]) == original

    replacement = _saxo_credentials(suffix="new")
    rotated = env["client"].put(
        f"/broker-accounts/{account_id}/credentials",
        json=replacement,
        headers=AUTH,
    )
    assert rotated.status_code == 200, rotated.text
    assert "access-token-new" not in rotated.text
    assert "refresh-token-new" not in rotated.text
    assert json.loads(env["secrets"].store[secret_ref]) == replacement
    with env["factory"]() as session:
        credential = (
            session.query(BrokerCredential).filter(BrokerCredential.account_id == account_id).one()
        )
        assert credential.last_rotated_at is not None


def test_credential_rotation_cannot_cross_tenants(env: dict[str, Any]) -> None:
    created = env["client"].post(
        "/broker-accounts",
        json={
            "config_key": "primary",
            "broker_code": "coinbase",
            "environment": "live",
            "base_ccy": "EUR",
            "credentials": {
                "api_key": "owner-key",
                "api_secret": "owner-secret",
            },
        },
        headers=AUTH,
    )
    assert created.status_code == 200, created.text
    account_id = created.json()["account_id"]
    secret_ref = created.json()["secret_ref"]

    denied = env["client"].put(
        f"/users/demo_peer/broker-accounts/{account_id}/credentials",
        json={"api_key": "other-key", "api_secret": "other-secret"},
        headers=AUTH,
    )
    assert denied.status_code == 404
    assert json.loads(env["secrets"].store[secret_ref])["api_key"] == "owner-key"
