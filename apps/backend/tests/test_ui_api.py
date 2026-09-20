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
    ExecutionMetric,
    Instrument,
    InstrumentPrice,
    LinkedBrokerAccount,
    Order,
    OrderIntent,
    Position,
    Strategy,
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
    assert swing["bindings"] == [
        {
            "account_id": 1,
            "account_name": "Paper EUR",
            "active": True,
            "autopilot": True,
            "entries_enabled": True,
            "exits_enabled": True,
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


def test_every_column_the_read_models_touch_is_granted_by_migration_0107(
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
    # ``0108`` grants the version endpoint's columns and belongs to the same guard.
    granted_columns = {
        **migration.READ_COLUMNS,
        _load("0108_deployment_record").TABLE: _load("0108_deployment_record").READ_COLUMNS,
    }

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
        used |= (
            {migration.POLICY_KEYS[table]} & set(granted)
            if table in migration.POLICY_KEYS
            else set()
        )
        if used != set(granted):
            mismatches[table] = {
                "ungranted": sorted(used - set(granted)),
                "unused": sorted(set(granted) - used),
            }
    assert mismatches == {}
