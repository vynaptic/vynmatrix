"""Owner UI: read-only API, owner isolation, projections read as recorded, static shell.

RLS is PostgreSQL-only and ``tenant_scope`` no-ops on this SQLite fixture, so the
explicit owner filters in ``ui_queries`` are what these tests exercise; the
service-role integration suite covers the grants and policies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from backend import ui_queries
from backend.api import create_app
from backend.ui_api import CONTENT_SECURITY_POLICY, UI_DIRECTORY
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lib_application.db.models import (
    Base,
    Broker,
    CanonicalSignal,
    DailyNav,
    Execution,
    ExecutionDecisionLog,
    ExecutionMetric,
    Instrument,
    InstrumentPrice,
    LinkedBrokerAccount,
    Order,
    OrderIntent,
    Position,
    RiskBreach,
    Strategy,
    StrategyRuntimeState,
    StrategyVersion,
    User,
    UserStrategyBinding,
)

ADMIN_KEY = "test-admin-key"
AUTH = {"X-Admin-Key": ADMIN_KEY}
ROUTES = (
    "/api/ui/overview",
    "/api/ui/strategies",
    "/api/ui/pnl",
    "/api/ui/fills",
    "/api/ui/version",
)
NOW = datetime.now(tz=UTC).replace(microsecond=0)
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _fill(
    session: Any,
    *,
    number: int,
    user: str,
    account: int,
    instr: int,
    side: str,
    hours_ago: int | None = None,
    at: datetime | None = None,
) -> None:
    session.add(
        OrderIntent(
            intent_id=number,
            user_id=user,
            account_id=account,
            strategy_id="swing_v1",
            canonical_signal_id=1,
            side=side,
            execution_mode="spot",
            broker_environment="paper",
            method="SPOT",
            payload={"currency_context": {"secret": "never-returned"}},
            status="routed",
        )
    )
    session.add(
        Order(
            order_id=number,
            intent_id=number,
            broker_id=6,
            account_id=account,
            settlement_currency="USDC",
            client_order_id=f"client-order-{number}",
            broker_order_ref=f"broker-ref-{number}",
            state="filled",
        )
    )
    session.add(
        Execution(
            exec_id=number,
            order_id=number,
            instr_id=instr,
            fill_ts=_naive(at or NOW - timedelta(hours=hours_ago or 10 - number)),
            qty=Decimal("0.5"),
            price=Decimal("60000.10"),
            fee_ccy="USDC",
            fee_amount=Decimal("1.25"),
            venue="paper",
            trade_id=f"trade-{number}",
        )
    )


def _metric(
    session: Any,
    *,
    key: str,
    user: str,
    account: int,
    symbol: str,
    mode: str,
    realized: str,
    equity: str,
    minutes_ago: int,
) -> None:
    session.add(
        ExecutionMetric(
            metric_id=key,
            user_id=user,
            account_id=account,
            strategy_id="swing_v1",
            symbol=symbol,
            execution_mode=mode,
            broker="paper",
            realized_pnl=Decimal(realized),
            equity=Decimal(equity),
            created_at=NOW - timedelta(minutes=minutes_ago),
        )
    )


@pytest.fixture
def factory() -> Any:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    with session_factory() as s:
        s.add(
            User(
                user_id="owner",
                email="owner@example.invalid",
                full_name="Demo Owner",
                base_ccy="EUR",
                is_deployment_owner=True,
            )
        )
        s.add(User(user_id="peer", email="peer@example.invalid", base_ccy="INR"))
        s.add(Broker(broker_id=6, code="paper", name="Paper"))
        s.add(
            LinkedBrokerAccount(
                account_id=1,
                user_id="owner",
                broker_id=6,
                environment="paper",
                display_name="Paper EUR",
                external_ref="vault://never-returned",
                config_key="canary-never-returned",
                base_ccy="EUR",
                paper_initial_equity=Decimal("100000"),
                paper_initial_cash=Decimal("100000"),
            )
        )
        s.add(
            LinkedBrokerAccount(
                account_id=2,
                user_id="peer",
                broker_id=6,
                environment="paper",
                display_name="Peer account",
                base_ccy="INR",
                paper_initial_equity=Decimal("5000"),
                paper_initial_cash=Decimal("5000"),
            )
        )
        s.add_all(
            [
                Instrument(
                    instr_id=1,
                    asset_class="crypto",
                    canonical="BTCUSDC",
                    settlement_currency="USDC",
                ),
                Instrument(
                    instr_id=2,
                    asset_class="crypto",
                    canonical="ETHUSDC",
                    settlement_currency="USDC",
                ),
                Strategy(
                    strategy_id="swing_v1",
                    strategy_name="Swing High Low",
                    asset_class="crypto",
                    description="Trades swing breaks.",
                    is_active=True,
                ),
                Strategy(strategy_id="quiet_v1", strategy_name="Quiet One", asset_class="equity"),
                Strategy(
                    strategy_id="ported_v1",
                    strategy_name="Freshly Ported",
                    asset_class="crypto",
                    description="Registered from disk, never activated.",
                ),
                StrategyVersion(
                    strategy_id="ported_v1", semver="1.0.0", param_schema={}, status="registered"
                ),
                StrategyVersion(
                    strategy_id="swing_v1", semver="1.0.0", param_schema={}, status="deprecated"
                ),
            ]
        )
        s.flush()
        s.add(
            StrategyVersion(
                strategy_id="swing_v1",
                semver="1.1.0",
                param_schema={},
                status="active",
                released_at=NOW,
            )
        )
        s.add(
            UserStrategyBinding(
                user_id="owner",
                strategy_id="swing_v1",
                broker_account_id=1,
                is_active=True,
                autopilot=True,
                entries_enabled=True,
                exits_enabled=True,
            )
        )
        for index, action in enumerate(("long", "flat", "short"), start=1):
            s.add(
                CanonicalSignal(
                    signal_id=index,
                    strategy_id="swing_v1",
                    instr_id=1,
                    action=action,
                    confidence=Decimal("0.7000"),
                    external_signal_id=f"ext-{index}",
                    features={"secret_feature": 1},
                    ts=_naive(NOW - timedelta(hours=index)),
                )
            )
        for number, side in ((1, "BUY"), (2, "SELL"), (3, "BUY")):
            _fill(s, number=number, user="owner", account=1, instr=1, side=side)
        _fill(s, number=4, user="peer", account=2, instr=2, side="BUY")
        # Replayed history: the highest id is the oldest fill.
        _fill(s, number=5, user="owner", account=1, instr=2, side="SELL", hours_ago=20)

        _metric(
            s,
            key="m1",
            user="owner",
            account=1,
            symbol="BTCUSDC",
            mode="spot",
            realized="-10",
            equity="99990",
            minutes_ago=30,
        )
        _metric(
            s,
            key="m2",
            user="owner",
            account=1,
            symbol="BTCUSDC",
            mode="spot",
            realized="-25",
            equity="99975",
            minutes_ago=20,
        )
        _metric(
            s,
            key="m3",
            user="owner",
            account=1,
            symbol="ETHUSDC",
            mode="spot",
            realized="5",
            equity="99980",
            minutes_ago=10,
        )
        _metric(
            s,
            key="m4",
            user="owner",
            account=1,
            symbol="ETHUSDC",
            mode="blocked",
            realized="999",
            equity="1",
            minutes_ago=5,
        )
        _metric(
            s,
            key="m5",
            user="peer",
            account=2,
            symbol="ETHUSDC",
            mode="spot",
            realized="777",
            equity="555",
            minutes_ago=1,
        )

        # Anchor to NOW's UTC date, not the host's local one: the equity source
        # compares a nav date against a metric's UTC date, and for the hours
        # where the two calendars disagree the expected source flips.
        today = NOW.date()
        s.add_all(
            [
                DailyNav(
                    user_id="owner",
                    account_id=1,
                    date=today - timedelta(days=1),
                    nav_ccy="EUR",
                    nav_value=Decimal("99990.00"),
                ),
                DailyNav(
                    user_id="owner",
                    account_id=1,
                    date=today,
                    nav_ccy="EUR",
                    nav_value=Decimal("99980.00"),
                ),
                DailyNav(
                    user_id="owner",
                    account_id=1,
                    date=today - timedelta(days=2),
                    nav_ccy="USD",
                    nav_value=Decimal("1.00"),
                ),
                DailyNav(
                    user_id="peer",
                    account_id=2,
                    date=today,
                    nav_ccy="INR",
                    nav_value=Decimal("123.00"),
                ),
                Position(
                    account_id=1,
                    instr_id=1,
                    qty=Decimal("0.5"),
                    avg_price=Decimal("60000"),
                    last_mark=Decimal("61000"),
                    gross_notional=Decimal("30500"),
                    notional_currency="USDC",
                ),
                Position(account_id=1, instr_id=2, qty=Decimal("0")),
                Position(
                    account_id=2,
                    instr_id=2,
                    qty=Decimal("9"),
                    gross_notional=Decimal("900"),
                    notional_currency="USDC",
                ),
                InstrumentPrice(
                    instr_id=1,
                    ts=_naive(NOW - timedelta(minutes=2)),
                    timeframe="1m",
                    open=1,
                    high=1,
                    low=1,
                    close=Decimal("61000"),
                    volume=1,
                    source="coinbase_live",
                ),
            ]
        )
        s.commit()
    return session_factory


@pytest.fixture
def client(factory: Any) -> TestClient:
    return TestClient(create_app(session_factory=factory, admin_api_key=ADMIN_KEY))


@pytest.mark.parametrize("route", ROUTES)
def test_every_route_requires_the_admin_key(client: TestClient, route: str) -> None:
    assert client.get(route).status_code == 401
    assert client.get(route, headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert client.get(route, headers=AUTH).status_code == 200


def test_anonymous_development_mode_needs_no_key(
    factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BACKEND_ADMIN_API_KEY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "dev")
    app = create_app(session_factory=factory, allow_anon=True)
    assert TestClient(app).get("/api/ui/overview").status_code == 200


def test_a_caller_supplied_user_id_is_refused(client: TestClient) -> None:
    assert client.get("/api/ui/overview?user_id=peer", headers=AUTH).status_code == 422


def test_overview_reports_only_the_owner_and_reads_projections_as_recorded(
    client: TestClient,
) -> None:
    body = client.get("/api/ui/overview", headers=AUTH).json()

    assert body["safety"] == {"execution_mode": "paper", "allow_live": False, "environment": "dev"}
    assert body["owner"] == {
        "display_name": "Demo Owner",
        "base_currency": "EUR",
        "timezone": "Europe/Amsterdam",
    }
    assert [account["account_id"] for account in body["accounts"]] == [1]
    account = body["accounts"][0]
    assert account["currency"] == "EUR"
    # Latest row of each non-blocked partition: -25 + 5. Never -10 - 25 + 5, never 999.
    assert Decimal(account["realized_pnl"]["value"]) == Decimal("-20")
    assert Decimal(account["equity"]["value"]) == Decimal("1")  # newest snapshot, as recorded
    assert account["equity"]["source"] == "execution_snapshot"
    assert account["open_positions"] == 1
    assert account["fills"] == 4
    assert body["strategies"] == {"catalogue": 3, "released": 1, "bound": 1, "active": 1}
    assert body["freshness"]["price_feeds"] == [
        {"timeframe": "1m", "last_at": ui_queries.iso_utc(NOW - timedelta(minutes=2))}
    ]
    # The newest signal (one hour old) is newer than the newest fill (seven hours old).
    assert body["activity"][0]["kind"] == "signal"
    assert body["freshness"]["last_signal_at"] == body["activity"][0]["at"]
    assert body["freshness"]["last_fill_at"] == max(
        item["at"] for item in body["activity"] if item["kind"] == "fill"
    )
    times = [item["at"] for item in body["activity"]]
    assert times == sorted(times, reverse=True)
    assert {item["kind"] for item in body["activity"]} == {"signal", "fill"}
    assert len(body["activity"]) <= 10


def test_equity_prefers_a_daily_snapshot_that_is_newer_than_the_last_execution(
    client: TestClient, factory: Any
) -> None:
    """A quiet account must not keep showing the equity of its last trade."""
    with factory() as s:
        s.add(
            LinkedBrokerAccount(
                account_id=3,
                user_id="owner",
                broker_id=6,
                environment="paper",
                display_name="Quiet EUR",
                base_ccy="EUR",
                paper_initial_equity=Decimal("1000"),
                paper_initial_cash=Decimal("1000"),
            )
        )
        _metric(
            s,
            key="q1",
            user="owner",
            account=3,
            symbol="BTCUSDC",
            mode="spot",
            realized="0",
            equity="1005",
            minutes_ago=5 * 24 * 60,
        )
        s.add(
            DailyNav(
                user_id="owner",
                account_id=3,
                date=NOW.date() - timedelta(days=1),
                nav_ccy="EUR",
                nav_value=Decimal("1010.00"),
            )
        )
        s.commit()

    accounts = client.get("/api/ui/overview", headers=AUTH).json()["accounts"]
    quiet = next(account for account in accounts if account["account_id"] == 3)
    assert quiet["equity"]["source"] == "daily_nav"
    assert Decimal(quiet["equity"]["value"]) == Decimal("1010.00")
    busy = next(account for account in accounts if account["account_id"] == 1)
    assert busy["equity"]["source"] == "execution_snapshot"  # same day: the timestamped row wins


def test_no_secret_or_internal_field_reaches_any_payload(client: TestClient) -> None:
    for route in ROUTES:
        text = client.get(route, headers=AUTH).text
        for forbidden in (
            "never-returned",
            "external_ref",
            "config_key",
            "client-order",
            "broker-ref",
            "secret_feature",
            "@example.invalid",
            "Peer account",
            '"_at"',
            '"cursor"',
        ):
            assert forbidden not in text, (route, forbidden)


def test_strategies_lists_the_catalogue_with_binding_signal_and_recorded_pnl(
    client: TestClient,
) -> None:
    rows = client.get("/api/ui/strategies", headers=AUTH).json()["strategies"]
    by_id = {row["strategy_id"]: row for row in rows}

    swing = by_id["swing_v1"]
    # The deprecated 1.0.0 row was released later in this fixture; the active version wins.
    assert (swing["version"], swing["status"]) == ("1.1.0", "active")
    assert swing["released"] is True
    # binding_id addresses the row for a change; mode names the legal flag
    # combination the control offers instead of four independent switches.
    # The readiness checklist has its own tests below.
    assert [
        {key: value for key, value in binding.items() if key != "readiness"}
        for binding in swing["bindings"]
    ] == [
        {
            "binding_id": 1,
            "account_id": 1,
            "account_name": "Paper EUR",
            "mode": "trading",
            "active": True,
            "autopilot": True,
            "entries_enabled": True,
            "exits_enabled": True,
            # A binding made before scoping covers every instrument.
            "instruments_allowed": None,
        }
    ]
    assert swing["last_signal"]["action"] == "long"
    assert swing["last_signal"]["symbol"] == "BTCUSDC"
    assert [(r["currency"], Decimal(r["value"])) for r in swing["realized_pnl"]] == [
        ("EUR", Decimal("-20"))
    ]

    quiet = by_id["quiet_v1"]
    assert quiet["bindings"] == []
    assert quiet["last_signal"] is None
    assert quiet["version"] is None
    assert quiet["realized_pnl"] == []


def test_a_newly_registered_strategy_is_listed_with_its_version_before_it_ever_trades(
    client: TestClient,
) -> None:
    """The state every freshly ported strategy is in: registered, unbound, silent.

    It must still appear, with its version and status, or an owner who added
    strategies cannot tell registration from a failed import.
    """
    rows = client.get("/api/ui/strategies", headers=AUTH).json()["strategies"]
    ported = next(row for row in rows if row["strategy_id"] == "ported_v1")

    assert (ported["version"], ported["status"]) == ("1.0.0", "registered")
    assert ported["name"] == "Freshly Ported"
    # Fail-closed: the admin API refuses to bind a strategy that is not released.
    assert ported["released"] is False
    assert ported["bindings"] == []
    assert ported["last_signal"] is None
    assert ported["realized_pnl"] == []

    counts = client.get("/api/ui/overview", headers=AUTH).json()["strategies"]
    assert counts == {"catalogue": 3, "released": 1, "bound": 1, "active": 1}


def test_the_dashboard_counts_released_exactly_as_the_bind_gate_does(
    client: TestClient, factory: Any
) -> None:
    """is_active without an active version is not released on any page."""
    with factory() as session:
        session.get(Strategy, "ported_v1").is_active = True
        session.commit()

    counts = client.get("/api/ui/overview", headers=AUTH).json()["strategies"]
    rows = client.get("/api/ui/strategies", headers=AUTH).json()["strategies"]
    assert counts["released"] == 1
    assert counts["released"] == sum(row["released"] for row in rows)


# ------------------------------------------------------- readiness checklist


def _runtime_state(
    session: Any, *, strategy_id: str = "swing_v1", minutes_ago: int | None, bar: int = 60
) -> None:
    session.add(
        StrategyRuntimeState(
            worker_id=f"worker-{strategy_id}",
            strategy_id=strategy_id,
            strategy_version="1.1.0",
            symbols=["BTC-USDC"],
            source="coinbase_live",
            timeframe="1m",
            consolidation_minutes=bar,
            config_fingerprint="f" * 64,
            state_schema_version=1,
            state_payload={"secret_model_state": 1},
            last_source_ts=None
            if minutes_ago is None
            else _naive(NOW - timedelta(minutes=minutes_ago)),
        )
    )


def _decision(
    session: Any,
    *,
    key: str,
    user: str,
    status: str,
    reason: str | None,
    minutes_ago: int,
    binding: int | None = 1,
) -> None:
    session.add(
        ExecutionDecisionLog(
            user_id=user,
            binding_id=binding,
            instr_id=1,
            should_execute=status != "skipped",
            status=status,
            idempotency_key=key,
            reasoning={"trigger_reason": reason, "risk_caps": {"secret_cap": 1}},
            binding_config_snapshot={"secret_snapshot": 1},
            ts=NOW - timedelta(minutes=minutes_ago),
        )
    )


def _checks(client: TestClient, strategy_id: str = "swing_v1") -> dict[str, Any]:
    rows = client.get("/api/ui/strategies", headers=AUTH).json()["strategies"]
    (binding,) = next(row for row in rows if row["strategy_id"] == strategy_id)["bindings"]
    readiness = binding["readiness"]
    assert [check["key"] for check in readiness["checks"]] == [
        "release",
        "mode",
        "account",
        "worker",
        "decision",
        "risk",
    ]
    return {"ready": readiness["ready"], **{c["key"]: c for c in readiness["checks"]}}


def test_readiness_names_the_missing_worker_as_what_blocks_a_bound_strategy(
    client: TestClient,
) -> None:
    """Released, switched on, connected -- and silent, because no worker runs it."""
    checks = _checks(client)

    assert checks["ready"] is False
    assert (checks["release"]["state"], checks["mode"]["state"]) == ("ok", "ok")
    assert checks["account"]["state"] == "ok"
    assert checks["worker"]["state"] == "blocked"
    assert "STRATEGY_LIST" in checks["worker"]["detail"]
    assert checks["decision"]["state"] == "unknown"
    assert checks["decision"]["at"] is None
    assert checks["risk"]["state"] == "ok"


def test_readiness_is_ready_with_a_fresh_worker_and_explains_the_last_skip(
    client: TestClient, factory: Any
) -> None:
    with factory() as s:
        _runtime_state(s, minutes_ago=5)
        _decision(s, key="old", user="owner", status="executed", reason="met", minutes_ago=90)
        _decision(
            s,
            key="new",
            user="owner",
            status="skipped",
            reason="Asset score 0.31 (abs 0.31) below threshold 0.6",
            minutes_ago=10,
        )
        # Another user's newer decision on the same binding id is never the owner's.
        _decision(
            s, key="peer", user="peer", status="failed", reason="peer-only-reason", minutes_ago=1
        )
        s.add(
            RiskBreach(
                user_id="owner",
                broker_account_id=1,
                rule_code="max_total_exposure_pct",
                severity="block",
                context={"secret_context": 1},
                occurred_at=NOW - timedelta(hours=2),
            )
        )
        s.add(
            RiskBreach(
                user_id="owner",
                broker_account_id=1,
                rule_code="stale_warning",
                severity="warn",
                occurred_at=NOW - timedelta(minutes=1),
            )
        )
        s.commit()

    checks = _checks(client)

    assert checks["ready"] is True
    assert checks["worker"]["state"] == "ok"
    assert checks["worker"]["at"] is not None
    assert checks["decision"]["state"] == "warn"
    assert "below threshold 0.6" in checks["decision"]["detail"]
    assert checks["risk"]["state"] == "warn"
    # Only blocks count; a warning is not the reason an order did not happen.
    assert "max_total_exposure_pct" in checks["risk"]["detail"]
    body = client.get("/api/ui/strategies", headers=AUTH).text
    for secret in (
        "secret_model_state",
        "secret_cap",
        "secret_snapshot",
        "secret_context",
        "peer-only-reason",
    ):
        assert secret not in body


def test_readiness_reports_a_stale_worker_and_a_switched_off_binding(
    client: TestClient, factory: Any
) -> None:
    with factory() as s:
        # Hourly bars: three missed bars, not three missed minutes, make it stale.
        _runtime_state(s, minutes_ago=150, bar=60)
        binding = s.get(UserStrategyBinding, 1)
        binding.is_active = False
        binding.autopilot = False
        binding.entries_enabled = False
        binding.exits_enabled = False
        s.commit()
    checks = _checks(client)
    assert checks["ready"] is False
    assert checks["mode"]["state"] == "blocked"
    assert checks["worker"]["state"] == "ok"

    with factory() as s:
        s.get(StrategyRuntimeState, "worker-swing_v1").last_source_ts = _naive(
            NOW - timedelta(minutes=200)
        )
        s.commit()
    assert _checks(client)["worker"]["state"] == "warn"


def test_readiness_explains_an_unreleased_binding_and_a_revoked_account(
    client: TestClient, factory: Any
) -> None:
    with factory() as s:
        s.get(LinkedBrokerAccount, 1).status = "revoked"
        s.add(
            UserStrategyBinding(
                user_id="owner",
                strategy_id="ported_v1",
                broker_account_id=1,
                is_active=False,
                autopilot=False,
                entries_enabled=False,
                exits_enabled=False,
            )
        )
        _runtime_state(s, strategy_id="ported_v1", minutes_ago=None)
        s.commit()
    checks = _checks(client, "ported_v1")
    assert checks["release"]["state"] == "blocked"
    assert checks["account"]["state"] == "blocked"
    assert "revoked" in checks["account"]["detail"]
    # A worker that has never processed a bar is not a running pipeline.
    assert checks["worker"]["state"] == "warn"


def test_a_failing_readiness_query_degrades_only_the_checklist(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend import ui_readiness
    from sqlalchemy import text

    def _refused(session: Any, *_args: Any, **_kwargs: Any) -> Any:
        return session.execute(text("SELECT nothing FROM a_table_that_is_not_there")).all()

    monkeypatch.setattr(ui_readiness, "_worker_states", _refused)
    rows = client.get("/api/ui/strategies", headers=AUTH).json()["strategies"]
    swing = next(row for row in rows if row["strategy_id"] == "swing_v1")
    assert swing["bindings"][0]["readiness"] is None
    assert swing["released"] is True
    assert swing["last_signal"]["action"] == "long"


def test_pnl_is_per_account_in_the_account_currency(client: TestClient) -> None:
    body = client.get("/api/ui/pnl?days=30", headers=AUTH).json()
    assert [account["account_id"] for account in body["accounts"]] == [1]
    account = body["accounts"][0]

    assert [Decimal(point["value"]) for point in account["equity_daily"]] == [
        Decimal("99990.00"),
        Decimal("99980.00"),
    ]  # the USD row is not comparable and is left out
    assert [Decimal(point["value"]) for point in account["equity_by_trade"]] == [
        Decimal("99990"),
        Decimal("99975"),
        Decimal("99980"),
    ]  # oldest first, blocked snapshots excluded
    assert [point["trade"] for point in account["equity_by_trade"]] == [1, 2, 3]
    assert {(row["symbol"], Decimal(row["value"])) for row in account["realized"]} == {
        ("BTCUSDC", Decimal("-25")),
        ("ETHUSDC", Decimal("5")),
    }
    assert Decimal(account["realized_total"]) == Decimal("-20")
    assert [position["symbol"] for position in account["positions"]] == ["BTCUSDC"]
    assert account["fees"] == [{"currency": "USDC", "value": "5.00000000"}]


@pytest.mark.parametrize("query", ["days=0", "days=731", "days=abc"])
def test_pnl_rejects_an_out_of_range_window(client: TestClient, query: str) -> None:
    assert client.get(f"/api/ui/pnl?{query}", headers=AUTH).status_code == 422


def test_fills_page_by_fill_time_not_by_insertion_order_and_stay_owner_only(
    client: TestClient,
) -> None:
    first = client.get("/api/ui/fills?limit=2", headers=AUTH).json()
    assert [fill["id"] for fill in first["fills"]] == [3, 2]
    assert first["fills"][0]["side"] == "buy"
    assert first["fills"][0]["currency"] == "USDC"
    assert "cursor" not in first["fills"][0]

    second = client.get(
        "/api/ui/fills", params={"limit": 2, "before": first["next_before"]}, headers=AUTH
    ).json()
    # Id 5 was inserted last but filled first, so it closes the list; the peer's id 4 never shows.
    assert [fill["id"] for fill in second["fills"]] == [1, 5]
    assert second["next_before"] is None

    for query in ("limit=0", "limit=201", "before=not-a-cursor", "before=2026-01-01_x"):
        assert client.get(f"/api/ui/fills?{query}", headers=AUTH).status_code == 422


def test_a_failing_section_degrades_to_null_inside_a_savepoint(
    client: TestClient, factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On PostgreSQL a failed statement aborts the transaction; only a savepoint keeps the
    later sections, and the transaction-local tenant scope, alive."""
    from sqlalchemy import event, text

    def _refused(session: Any, _account_ids: Any) -> Any:
        return session.execute(text("SELECT nothing FROM a_table_that_is_not_there")).all()

    statements: list[str] = []
    engine = factory.kw["bind"]

    def _capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    monkeypatch.setattr(ui_queries, "_fill_stats", _refused)
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        body = client.get("/api/ui/overview", headers=AUTH).json()
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    account = body["accounts"][0]
    assert account["fills"] is None
    assert account["open_positions"] == 1  # later sections still ran in the same transaction
    assert Decimal(account["realized_pnl"]["value"]) == Decimal("-20")
    assert body["freshness"]["last_fill_at"] is None
    failing = next(i for i, sql in enumerate(statements) if "a_table_that_is_not_there" in sql)
    assert statements[failing - 1].startswith("SAVEPOINT")
    assert statements[failing + 1].startswith("ROLLBACK TO SAVEPOINT")


def test_activity_is_ordered_by_the_instant_not_by_its_text(
    client: TestClient, factory: Any
) -> None:
    """A whole-second signal and a fractional fill in the same second: "...:00Z" sorts after
    "...:00.250000Z" as text, yet the fill happened later."""
    moment = NOW - timedelta(minutes=5)
    with factory() as s:
        s.add(
            CanonicalSignal(
                signal_id=90,
                strategy_id="swing_v1",
                instr_id=1,
                action="long",
                external_signal_id="ext-same-second",
                ts=_naive(moment),
            )
        )
        _fill(
            s,
            number=6,
            user="owner",
            account=1,
            instr=1,
            side="BUY",
            at=moment + timedelta(milliseconds=250),
        )
        s.commit()

    activity = client.get("/api/ui/overview", headers=AUTH).json()["activity"]
    assert [item["kind"] for item in activity[:2]] == ["fill", "signal"]


def test_a_binding_without_a_strategy_is_counted_nowhere(client: TestClient, factory: Any) -> None:
    """Both pages must agree; an unassigned binding cannot be listed under a strategy."""
    with factory() as s:
        s.add(
            UserStrategyBinding(
                user_id="owner",
                strategy_id=None,
                broker_account_id=1,
                is_active=False,
                autopilot=False,
                entries_enabled=False,
                exits_enabled=False,
            )
        )
        s.commit()

    assert client.get("/api/ui/overview", headers=AUTH).json()["strategies"]["bound"] == 1
    rows = client.get("/api/ui/strategies", headers=AUTH).json()["strategies"]
    assert sum(len(row["bindings"]) for row in rows) == 1


def test_the_shell_is_public_static_and_carries_the_ui_content_security_policy(
    client: TestClient,
) -> None:
    response = client.get("/ui/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert ADMIN_KEY not in response.text

    script = client.get("/ui/app.js")
    assert script.status_code == 200
    assert script.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    # The policy belongs to the UI only; the JSON API keeps the shared headers.
    assert "content-security-policy" not in client.get("/api/ui/overview", headers=AUTH).headers


def test_the_root_path_opens_the_ui(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code in {302, 307}
    assert response.headers["location"] == "/ui/"


def test_assets_are_same_origin_with_no_inline_script_or_style() -> None:
    assets = [path for path in UI_DIRECTORY.rglob("*") if path.is_file()]
    assert {path.name for path in assets} >= {"index.html", "app.js", "app.css"}
    for path in assets:
        assert path.suffix in {".html", ".js", ".css", ".svg"}, path
        text = path.read_text(encoding="utf-8").replace(_SVG_NAMESPACE, "")
        assert "http://" not in text, path
        assert "https://" not in text, path
        assert "innerHTML" not in text, path  # untrusted labels go through textContent
    index = (UI_DIRECTORY / "index.html").read_text(encoding="utf-8")
    assert "<style" not in index
    assert " style=" not in index
    assert "<script>" not in index
    assert " onclick=" not in index


def test_every_column_the_read_models_touch_is_granted_by_migrations_0107_and_0109(
    client: TestClient, factory: Any
) -> None:
    """SQLite cannot refuse a column, PostgreSQL will: keep the queries inside the grants."""
    import importlib.util
    import re
    from pathlib import Path

    from sqlalchemy import event

    versions = Path(__file__).resolve().parents[3] / "scripts/db/alembic/versions"

    def _load(name: str) -> Any:
        spec = importlib.util.spec_from_file_location(name, versions / f"{name}.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    migration = _load("0107_backend_ui_read")
    readiness = _load("0109_backend_ui_readiness")
    # ``0108`` grants the version endpoint's columns and ``0109`` the readiness
    # checklist's; both belong to the same guard.
    granted_columns = {
        **migration.READ_COLUMNS,
        _load("0108_deployment_record").TABLE: _load("0108_deployment_record").READ_COLUMNS,
        **readiness.READ_COLUMNS,
    }
    policy_keys = {**migration.POLICY_KEYS, **readiness.POLICY_KEYS}
    with factory() as s:
        # Every readiness query must run for the capture to see its columns.
        _runtime_state(s, minutes_ago=5)
        _decision(s, key="grant", user="owner", status="skipped", reason="r", minutes_ago=5)
        s.add(RiskBreach(user_id="owner", broker_account_id=1, rule_code="x", severity="block"))
        s.commit()

    statements: list[str] = []
    engine = factory.kw["bind"]

    def _capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        for route in ROUTES:
            assert client.get(route, headers=AUTH).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    sql = "\n".join(statements)
    # Exactly the granted columns, no more and no less: an ungranted column is
    # refused by PostgreSQL, and an unused grant is excess privilege.
    mismatches = {}
    for table, granted in granted_columns.items():
        used = set(re.findall(rf"\b{table}\.(\w+)\b", sql))
        # A row policy filters on its key column even where no query names it.
        used |= {policy_keys[table]} & set(granted) if table in policy_keys else set()
        if used != set(granted):
            mismatches[table] = {
                "ungranted": sorted(used - set(granted)),
                "unused": sorted(set(granted) - used),
            }
    assert mismatches == {}


# --------------------------------------------------------- owner control


CONTROL = "/api/ui/control"


def test_control_requires_the_admin_key(client: TestClient) -> None:
    assert client.get(CONTROL).status_code == 401
    assert client.get(CONTROL, headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert client.get(CONTROL, headers=AUTH).status_code == 200


def test_control_returns_the_identifiers_an_edit_must_echo_but_never_a_secret(
    client: TestClient,
) -> None:
    """A deliberate widening: PATCH requires `expected`, so the owner must see it.

    ``config_key`` and ``external_ref`` are identifiers the owner chose, not key
    material, and they are returned on this route only.
    """
    body = client.get(CONTROL, headers=AUTH)
    payload = body.json()
    account = payload["accounts"][0]

    assert account["config_key"] == "canary-never-returned"
    assert account["external_ref"] == "vault://never-returned"
    # Credential material is still unreachable: presence and status only.
    assert set(account["credential"]) == {"present", "status", "expires_at"}
    for forbidden in ("secret_ref", "api_key", "api_secret", "passphrase", "access_token"):
        assert forbidden not in body.text
    # And the read-only pages keep their narrower surface.
    for route in ROUTES:
        assert "never-returned" not in client.get(route, headers=AUTH).text


def test_control_shows_only_the_deployment_owner(client: TestClient) -> None:
    payload = client.get(CONTROL, headers=AUTH).json()

    assert payload["owner"]["user_id"] == "owner"
    assert [account["account_id"] for account in payload["accounts"]] == [1]
    assert "Peer account" not in client.get(CONTROL, headers=AUTH).text
    assert all(binding["binding_id"] for binding in payload["bindings"])


def test_released_means_what_the_bind_gate_means(client: TestClient, factory: Any) -> None:
    """is_active alone is not a release; the gate also needs an active version."""
    payload = client.get(CONTROL, headers=AUTH).json()
    released = {row["strategy_id"]: row["released"] for row in payload["strategies"]}

    assert released["swing_v1"] is True
    assert released["ported_v1"] is False  # registered version, never activated
    assert released["quiet_v1"] is False  # no version at all

    with factory() as session:
        strategy = session.get(Strategy, "ported_v1")
        strategy.is_active = True
        session.commit()
    after = client.get(CONTROL, headers=AUTH).json()
    still = {row["strategy_id"]: row["released"] for row in after["strategies"]}
    # is_active is now true, but no version is active, so the gate still refuses.
    assert still["ported_v1"] is False


def test_binding_mode_change_is_fenced_and_audited(client: TestClient, factory: Any) -> None:
    binding_id = client.get(CONTROL, headers=AUTH).json()["bindings"][0]["binding_id"]

    stale = client.post(
        f"/api/ui/bindings/{binding_id}",
        json={"expected": {"mode": "off"}, "changes": {"mode": "close_only"}},
        headers=AUTH,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "stale expected value for mode"

    applied = client.post(
        f"/api/ui/bindings/{binding_id}",
        json={"expected": {"mode": "trading"}, "changes": {"mode": "close_only"}},
        headers=AUTH,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["mode"] == "close_only"

    with factory() as session:
        row = session.get(UserStrategyBinding, binding_id)
        # The whole flag combination moved together, never one switch at a time.
        assert (row.is_active, row.entries_enabled, row.exits_enabled, row.autopilot) == (
            True,
            False,
            True,
            False,
        )
    activity = client.get(CONTROL, headers=AUTH).json()["activity"]
    assert activity[0]["action"] == "binding.patch"
    assert activity[0]["status"] == "ok"
    assert activity[0]["fields"] == ["mode"]


def test_a_refused_change_is_audited_too(client: TestClient) -> None:
    """A rolled-back transaction carries no audit row, so refusals write their own."""
    binding_id = client.get(CONTROL, headers=AUTH).json()["bindings"][0]["binding_id"]

    refused = client.post(
        f"/api/ui/bindings/{binding_id}",
        json={"expected": {"mode": "off"}, "changes": {"mode": "close_only"}},
        headers=AUTH,
    )
    assert refused.status_code == 409

    activity = client.get(CONTROL, headers=AUTH).json()["activity"]
    assert activity[0]["action"] == "binding.patch"
    assert activity[0]["status"] == "error"


def test_a_change_already_applied_is_accepted_rather_than_fought_over(
    client: TestClient,
) -> None:
    """Two tabs, same intent: the second must not fail on a value it already holds.

    This is the account contract's idempotence tolerance -- the expected value
    may be stale as long as the row already holds what the caller is asking for.
    """
    binding_id = client.get(CONTROL, headers=AUTH).json()["bindings"][0]["binding_id"]

    repeat = client.post(
        f"/api/ui/bindings/{binding_id}",
        json={"expected": {"mode": "off"}, "changes": {"mode": "trading"}},
        headers=AUTH,
    )

    assert repeat.status_code == 200
    assert repeat.json()["mode"] == "trading"


def test_a_patch_may_only_name_the_fields_the_control_offers(client: TestClient) -> None:
    binding_id = client.get(CONTROL, headers=AUTH).json()["bindings"][0]["binding_id"]

    for expected, changes in (
        ({"is_active": True}, {"is_active": False}),
        ({"strategy_id": "swing_v1"}, {"strategy_id": "ported_v1"}),
        ({"broker_account_id": 1}, {"broker_account_id": 2}),
        # The instrument scope is patchable; its other dimensions are not.
        ({"asset_classes_allowed": ["crypto"]}, {"asset_classes_allowed": ["equity"]}),
    ):
        refused = client.post(
            f"/api/ui/bindings/{binding_id}",
            json={"expected": expected, "changes": changes},
            headers=AUTH,
        )
        assert refused.status_code == 422, (changes, refused.text)
        assert "unsupported fields" in refused.json()["detail"]

    mismatched = client.post(
        f"/api/ui/bindings/{binding_id}",
        json={"expected": {}, "changes": {"mode": "off"}},
        headers=AUTH,
    )
    assert mismatched.status_code == 422
    assert "expected current value" in mismatched.json()["detail"]


def test_binding_a_strategy_requires_a_release_before_it_can_trade(
    client: TestClient,
) -> None:
    armed = client.post(
        "/api/ui/bindings",
        json={"strategy_id": "ported_v1", "broker_account_id": 1, "mode": "trading"},
        headers=AUTH,
    )
    assert armed.status_code == 409
    assert armed.json()["detail"] == "strategy is not released for trading"

    # Preparing it while it waits for a release is allowed: it cannot trade.
    prepared = client.post(
        "/api/ui/bindings",
        json={"strategy_id": "ported_v1", "broker_account_id": 1, "mode": "off"},
        headers=AUTH,
    )
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["mode"] == "off"


def test_binding_rejects_an_account_that_is_not_the_owners(client: TestClient) -> None:
    refused = client.post(
        "/api/ui/bindings",
        json={"strategy_id": "ported_v1", "broker_account_id": 2, "mode": "off"},
        headers=AUTH,
    )

    assert refused.status_code == 400
    assert "connected account owned by this user" in refused.json()["detail"]


def test_binding_twice_points_at_the_existing_row_instead_of_duplicating(
    client: TestClient,
) -> None:
    again = client.post(
        "/api/ui/bindings",
        json={"strategy_id": "swing_v1", "broker_account_id": 1, "mode": "off"},
        headers=AUTH,
    )

    assert again.status_code == 409
    assert "already bound" in again.json()["detail"]


def _release(session: Any, strategy_id: str, universe: Any) -> None:
    session.add(
        Strategy(
            strategy_id=strategy_id,
            strategy_name=strategy_id,
            asset_class="crypto",
            is_active=True,
        )
    )
    session.flush()
    session.add(
        StrategyVersion(
            strategy_id=strategy_id,
            semver="1.0.0",
            param_schema={},
            default_params={"universe": universe, "asset_class": "crypto"},
            status="active",
        )
    )


def test_two_released_strategies_with_disjoint_universes_both_trade_one_account(
    client: TestClient, factory: Any
) -> None:
    """Each binding is scoped to its strategy's universe; only a shared instrument conflicts."""
    with factory() as session:
        _release(session, "btc_v1", "BTC-USDC")
        _release(session, "eth_v1", ["ETHUSDC"])
        _release(session, "majors_v1", "BTCUSDC,ETHUSDC")
        session.commit()

    def bind(strategy_id: str) -> Any:
        return client.post(
            "/api/ui/bindings",
            json={"strategy_id": strategy_id, "broker_account_id": 1, "mode": "trading"},
            headers=AUTH,
        )

    # The fixture's swing binding names no scope, so it holds every instrument.
    blocked = bind("btc_v1")
    assert blocked.status_code == 409
    assert "swing_v1 is already switched on for every instrument" in blocked.json()["detail"]
    swing = client.get(CONTROL, headers=AUTH).json()["bindings"][0]["binding_id"]
    switched_off = client.post(
        f"/api/ui/bindings/{swing}",
        json={"expected": {"mode": "trading"}, "changes": {"mode": "off"}},
        headers=AUTH,
    )
    assert switched_off.status_code == 200, switched_off.text

    for strategy_id in ("btc_v1", "eth_v1"):
        bound = bind(strategy_id)
        assert bound.status_code == 200, bound.text
        assert bound.json()["mode"] == "trading"

    overlapping = bind("majors_v1")
    assert overlapping.status_code == 409
    detail = overlapping.json()["detail"]
    assert "btc_v1 is already switched on for BTCUSDC" in detail
    assert "switch btc_v1 off, narrow one of the two scopes" in detail
    assert "or bind this strategy to another account" in detail

    rows = {
        row["strategy_id"]: row
        for row in client.get("/api/ui/strategies", headers=AUTH).json()["strategies"]
    }
    assert rows["btc_v1"]["bindings"][0]["instruments_allowed"] == ["BTCUSDC"]
    assert rows["eth_v1"]["bindings"][0]["instruments_allowed"] == ["ETHUSDC"]
    assert rows["swing_v1"]["bindings"][0]["instruments_allowed"] is None
    assert rows["majors_v1"]["bindings"] == []
    activity = client.get(CONTROL, headers=AUTH).json()["activity"]
    assert (activity[0]["action"], activity[0]["status"]) == ("binding.create", "error")
    assert activity[1]["fields"] == ["instruments_allowed", "mode"]


def test_narrowing_a_binding_shares_its_account_but_never_strands_a_position(
    client: TestClient, factory: Any
) -> None:
    """The fixture's swing binding covers every instrument and holds BTC long, ETH short."""
    with factory() as session:
        session.add(
            Instrument(
                instr_id=3, asset_class="crypto", canonical="SOLUSDC", settlement_currency="USDC"
            )
        )
        _release(session, "sol_v1", "SOLUSDC")
        session.commit()
    swing = client.get(CONTROL, headers=AUTH).json()["bindings"][0]["binding_id"]

    def bind_sol() -> Any:
        return client.post(
            "/api/ui/bindings",
            json={"strategy_id": "sol_v1", "broker_account_id": 1, "mode": "trading"},
            headers=AUTH,
        )

    def scope(expected: Any, changes: Any) -> Any:
        return client.post(
            f"/api/ui/bindings/{swing}",
            json={
                "expected": {"instruments_allowed": expected},
                "changes": {"instruments_allowed": changes},
            },
            headers=AUTH,
        )

    assert bind_sol().status_code == 409

    stranding = scope(None, ["BTCUSDC"])
    assert stranding.status_code == 409
    assert "swing_v1 still holds an open position in ETHUSDC" in stranding.json()["detail"]

    narrowed = scope(None, ["BTC-USDC", "ETHUSDC"])
    assert narrowed.status_code == 200, narrowed.text
    assert narrowed.json()["mode"] == "trading"
    shared = bind_sol()
    assert shared.status_code == 200, shared.text
    assert shared.json()["mode"] == "trading"

    widening = scope(["ETHUSDC", "BTCUSDC"], ["BTCUSDC", "ETHUSDC", "SOLUSDC"])
    assert widening.status_code == 409
    assert "sol_v1 is already switched on for SOLUSDC" in widening.json()["detail"]

    rows = {
        row["strategy_id"]: row
        for row in client.get("/api/ui/strategies", headers=AUTH).json()["strategies"]
    }
    assert rows["swing_v1"]["bindings"][0]["instruments_allowed"] == ["BTCUSDC", "ETHUSDC"]
    assert rows["sol_v1"]["bindings"][0]["instruments_allowed"] == ["SOLUSDC"]
    assert {rows[name]["bindings"][0]["mode"] for name in ("swing_v1", "sol_v1")} == {"trading"}
    activity = client.get(CONTROL, headers=AUTH).json()["activity"]
    assert (activity[0]["action"], activity[0]["status"]) == ("binding.patch", "error")
    assert activity[0]["fields"] == ["instruments_allowed"]


def test_every_column_a_write_touches_is_granted_for_update(
    client: TestClient, factory: Any
) -> None:
    """SQLite cannot refuse a column, PostgreSQL will -- keep writes inside the grants.

    The read guard above covers ``SELECT``. This is its write half: it captures
    the ``UPDATE`` statements the control surface emits and checks each one
    against the column-level grant ``0102`` actually made. A scope patch also
    reads the execution ledger to find open positions, so those reads are held
    to the ``0107`` read grant here: the read guard runs only the GET routes.
    """
    import importlib.util
    import re
    from pathlib import Path

    from sqlalchemy import event

    versions = Path(__file__).resolve().parents[3] / "scripts/db/alembic/versions"

    def _load(name: str) -> Any:
        spec = importlib.util.spec_from_file_location(name, versions / f"{name}.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    migration = _load("0102_owner_control_plane")
    granted = {
        "users": {name.strip() for name in migration._PROFILE_COLUMNS.split(",")},
        "linked_broker_accounts": {name.strip() for name in migration._ACCOUNT_COLUMNS.split(",")},
    }
    ledger_grants = {
        table: set(columns)
        for table, columns in _load("0107_backend_ui_read").READ_COLUMNS.items()
        if table in {"executions", "orders", "order_intents"}
    }

    statements: list[str] = []
    engine = factory.kw["bind"]

    def _capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    binding_id = client.get(CONTROL, headers=AUTH).json()["bindings"][0]["binding_id"]
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        scoped = client.post(
            f"/api/ui/bindings/{binding_id}",
            json={
                "expected": {"instruments_allowed": None},
                "changes": {"instruments_allowed": ["BTCUSDC", "ETHUSDC"]},
            },
            headers=AUTH,
        )
        assert scoped.status_code == 200, scoped.text
        assert (
            client.post(
                f"/api/ui/bindings/{binding_id}",
                json={"expected": {"mode": "trading"}, "changes": {"mode": "off"}},
                headers=AUTH,
            ).status_code
            == 200
        )
        assert (
            client.patch(
                "/owner",
                json={"expected": {"full_name": "Demo Owner"}, "changes": {"full_name": "Owner"}},
                headers=AUTH,
            ).status_code
            == 200
        )
        assert (
            client.patch(
                "/broker-accounts/1",
                json={"expected": {"display_name": "Paper EUR"}, "changes": {"display_name": "P"}},
                headers=AUTH,
            ).status_code
            == 200
        )
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    ungranted: dict[str, set[str]] = {}
    for statement in statements:
        match = re.match(r"UPDATE (\w+) SET (.+?) WHERE ", statement, re.DOTALL)
        if match is None or match.group(1) not in granted:
            continue
        table = match.group(1)
        touched = set(re.findall(r"(\w+)\s*=\s*\?", match.group(2)))
        excess = touched - granted[table]
        if excess:
            ungranted.setdefault(table, set()).update(excess)

    assert ungranted == {}, ungranted
    sql = "\n".join(statements)
    ledger_reads = {table: set(re.findall(rf"\b{table}\.(\w+)\b", sql)) for table in ledger_grants}
    assert {table: used - ledger_grants[table] for table, used in ledger_reads.items()} == {
        table: set() for table in ledger_grants
    }
    # The position guard did read the ledger, so the check above is not vacuous.
    assert all(ledger_reads.values()), ledger_reads
    # And the guard has teeth: these writes really did emit an UPDATE per table.
    updated = {
        match.group(1)
        for match in (re.match(r"UPDATE (\w+) ", s) for s in statements)
        if match is not None
    }
    assert {"users", "linked_broker_accounts", "user_strategy_bindings"} <= updated
