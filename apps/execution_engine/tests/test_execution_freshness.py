from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from execution_engine.brokers.base import AccountInfo, BrokerCapabilities, BrokerOrderResult
from execution_engine.config import BrokerType
from execution_engine.engine import ExecutionEngine
from execution_engine.execution_result import (
    RETRYABLE_BLOCK_REASONS,
    ExecutionResult,
    is_retryable_failure,
)
from execution_engine.models import OrderIntent
from execution_engine.order_builder import OrderBuilder
from execution_engine.state_provider import StateProvider
from lib_common.config_validation import (
    ExecutionFreshnessConfig,
    ExecutionRuntimeConfig,
    load_execution_engine_config,
)
from lib_strategy.signals.signal import Signal, SignalAction


class _FakeOrderBuilder(OrderBuilder):
    def build(self, req, account, market_data):  # type: ignore[no-untyped-def]
        return [
            OrderIntent(
                broker_code="coinbase",
                symbol=req.signal.symbol,
                side="BUY",
                quantity=1.0,
                order_type="market",
                metadata={"signal_id": req.signal.signal_id},
            )
        ]


class _FakeBroker:
    def __init__(self, *, fetched_at: datetime | None = None) -> None:
        self._connected = False
        self.fetched_at = fetched_at or datetime.now(tz=UTC)
        self.submit_calls = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker_type=BrokerType.COINBASE,
            supports_spot=True,
            supports_margin=False,
            supports_futures=False,
            supports_perpetual=False,
            supports_options=False,
            supported_assets=["crypto"],
            has_paper_trading=True,
        )

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def get_account_info(self) -> AccountInfo:
        return AccountInfo(
            broker=BrokerType.COINBASE,
            account_id="acct-1",
            equity=10_000.0,
            available_balance=10_000.0,
            margin_used=0.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            positions=[],
            currency="USD",
            fetched_at=self.fetched_at,
        )

    async def submit_order(self, intent: OrderIntent) -> BrokerOrderResult:
        self.submit_calls += 1
        return BrokerOrderResult.filled(order_id="o-1", quantity=intent.quantity, price=100.0)

    async def submit_options_order(self, intent):  # type: ignore[no-untyped-def]
        raise AssertionError("Options flow not expected")

    async def cancel_order(self, order_id: str) -> bool:
        return True

    async def get_order_status(self, order_id: str) -> BrokerOrderResult:
        return BrokerOrderResult.filled(order_id=order_id, quantity=1.0, price=100.0)

    async def get_positions(self):  # type: ignore[no-untyped-def]
        return []


class _FailingMarketDataProvider:
    async def get_quote(
        self,
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> dict[str, object]:
        del user_id, environment
        msg = f"quote unavailable for {symbol}"
        raise RuntimeError(msg)


def _signal() -> Signal:
    return Signal(
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTC-USD",
        asset_class="crypto",
        action=SignalAction.LONG,
        confidence=0.8,
        timestamp=datetime.now(tz=UTC),
        entry_price=100.0,
        stop_loss=95.0,
        external_signal_id="ext-execution-freshness",
    )


def _write_valid_marker(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "status": "passed",
                "completed_at": datetime.now(tz=UTC).isoformat(),
                "commit": "abc123",
                "symbols": ["BTC-USD"],
                "paper_window_days": 14,
                "duplicate_submission_count": 0,
                "operator": "tester",
                "sandbox_smoke_evidence": (
                    "apps/execution_engine/tests/test_coinbase_sandbox_integration.py"
                ),
                "paper_soak_evidence": ".artifacts/paper-soak/summary.json",
                "reconciliation_summary": ".artifacts/paper-soak/reconciliation.json",
            }
        ),
        encoding="utf-8",
    )


def _account_profile(
    *,
    broker: str = "coinbase",
    environment: str = "live",
    live_enabled: bool = True,
    fetched_at: datetime | None = None,
) -> dict[str, object]:
    account_id = 42
    credential_ref = f"{broker}-account-{account_id}"
    account: dict[str, object] = {
        "user_id": "user-1",
        "broker": broker,
        "environment": environment,
        "status": "connected",
        "base_ccy": "USD",
        # Production profiles (providers_db) carry the per-account credential
        # pointer here; credential resolution is strictly account-scoped.
        "credential_ref": credential_ref,
    }
    if environment == "paper":
        account.update(
            paper_initial_equity=10_000.0,
            paper_initial_cash=10_000.0,
        )
    return {
        "broker": broker,
        "broker_account_id": account_id,
        "base_ccy": "USD",
        "sandbox": environment == "paper",
        "live_enabled": live_enabled,
        "equity": 10_000.0,
        "available_cash": 10_000.0,
        "margin_used": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "positions": [],
        "last_synced_at": (fetched_at or datetime.now(tz=UTC)).isoformat(),
        "accounts": {str(account_id): account},
        "brokers": {broker: {"credential_ref": credential_ref}},
        "_broker_route_snapshot": {
            "broker": broker,
            "broker_environment": environment,
            "broker_account_id": account_id,
            "credential_ref": credential_ref,
            "sandbox": environment == "paper",
        },
    }


def _install_owned_account_route(engine: ExecutionEngine, monkeypatch: Any) -> None:
    currencies: dict[int, str] = {}

    def _resolve_account_id(
        *,
        user_id,
        broker_type,
        environment,
        profile,
    ):  # type: ignore[no-untyped-def]
        account_id = int(profile["broker_account_id"])
        route = profile["_broker_route_snapshot"]
        assert int(route["broker_account_id"]) == account_id
        account = profile["accounts"][str(account_id)]
        assert account["user_id"] == user_id
        assert account["broker"] == broker_type.value
        assert account["environment"] == environment
        assert account["status"] == "connected"
        currency = str(account["base_ccy"])
        assert currency
        currencies[account_id] = currency
        return account_id

    def _resolve_account_currency(*, user_id, account_id):  # type: ignore[no-untyped-def]
        del user_id
        return currencies[int(account_id)]

    monkeypatch.setattr(engine._route_resolver, "resolve_account_id", _resolve_account_id)
    monkeypatch.setattr(
        engine._route_resolver,
        "resolve_account_currency",
        _resolve_account_currency,
    )


def _engine(
    *,
    broker: _FakeBroker,
    monkeypatch: Any,
    market_data_provider: Any = None,
    sandbox_marker: Path | None = None,
) -> ExecutionEngine:
    runtime = (
        ExecutionRuntimeConfig(sandbox_certification_marker=sandbox_marker)
        if sandbox_marker is not None
        else ExecutionRuntimeConfig()
    )
    engine = ExecutionEngine(
        order_builder=_FakeOrderBuilder(),
        market_data_provider=market_data_provider,
        allow_live=True,
        freshness=ExecutionFreshnessConfig(
            max_signal_age_seconds=300,
            max_market_data_age_seconds=60,
            max_account_state_age_seconds=60,
        ),
        runtime_config=runtime,
    )
    # These tests isolate freshness gates after startup recovery has completed.
    engine.mark_reconciliation_health(healthy=True)
    _install_owned_account_route(engine, monkeypatch)

    async def _fake_get_broker(
        broker_type,
        environment,
        credential_ref,
        credentials=None,  # type: ignore[no-untyped-def]
        user_id=None,
        **_kwargs,
    ):
        return broker

    monkeypatch.setattr(engine, "_get_broker", _fake_get_broker)
    return engine


def _run(
    engine: ExecutionEngine,
    signal: Signal,
    *,
    mode: str = "live",
    profile: dict[str, object] | None = None,
    allow_historical_replay: bool = False,
) -> ExecutionResult:
    resolved_profile = profile if profile is not None else _account_profile()
    return asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=resolved_profile,
            user_strategy_config={"execution_mode": "spot", "mode": mode, "live_enabled": True},
            signal=signal,
            allow_historical_replay=allow_historical_replay,
        )
    )


def test_execution_freshness_config_is_schema_validated(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("EXECUTION_MAX_SIGNAL_AGE_SECONDS", "120")
    monkeypatch.setenv("EXECUTION_MAX_MARKET_DATA_AGE_SECONDS", "30")
    monkeypatch.setenv("EXECUTION_MAX_DELAYED_PAPER_MARKET_DATA_AGE_SECONDS", "1500")
    monkeypatch.setenv("EXECUTION_MAX_ACCOUNT_STATE_AGE_SECONDS", "45")
    monkeypatch.setenv("EXECUTION_MAX_MARKET_SESSION_AGE_SECONDS", "1800")

    cfg = load_execution_engine_config()

    assert cfg.freshness.max_signal_age_seconds == 120
    assert cfg.freshness.max_market_data_age_seconds == 30
    assert cfg.freshness.max_delayed_paper_market_data_age_seconds == 1500
    assert cfg.freshness.max_account_state_age_seconds == 45
    assert cfg.freshness.max_market_session_age_seconds == 1800


def test_live_stale_signal_blocks_before_broker_submit(monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    broker = _FakeBroker()
    engine = _engine(
        broker=broker,
        monkeypatch=monkeypatch,
        sandbox_marker=marker,
    )
    stale_signal = replace(_signal(), timestamp=datetime.now(tz=UTC) - timedelta(minutes=10))

    result = _run(engine, stale_signal)

    assert result.execution_mode == "blocked"
    assert "signal age" in (result.error_message or "")
    assert broker.submit_calls == 0
    # A stale SIGNAL is terminal — retrying won't make the signal fresh (RG-2).
    assert is_retryable_failure(result) is False


def test_paper_stale_signal_blocks_before_broker_submit(monkeypatch) -> None:
    """Paper catch-up must not turn an old model entry into a new order."""
    broker = _FakeBroker()
    engine = _engine(broker=broker, monkeypatch=monkeypatch)
    stale_signal = replace(_signal(), timestamp=datetime.now(tz=UTC) - timedelta(minutes=10))

    result = _run(
        engine,
        stale_signal,
        mode="paper",
        profile=_account_profile(
            broker="paper",
            environment="paper",
            live_enabled=False,
        ),
    )

    assert result.execution_mode == "blocked"
    assert "signal age" in (result.error_message or "")
    assert broker.submit_calls == 0


def test_paper_expired_entry_blocks_even_when_timestamp_is_recent(monkeypatch) -> None:
    broker = _FakeBroker()
    engine = _engine(broker=broker, monkeypatch=monkeypatch)
    expired_signal = replace(
        _signal(),
        expires_at=datetime.now(tz=UTC) - timedelta(seconds=1),
    )

    result = _run(
        engine,
        expired_signal,
        mode="paper",
        profile=_account_profile(
            broker="paper",
            environment="paper",
            live_enabled=False,
        ),
    )

    assert result.execution_mode == "blocked"
    assert "signal expired" in (result.error_message or "")
    assert broker.submit_calls == 0


def test_paper_expired_close_remains_reduce_only_exempt(monkeypatch, caplog) -> None:
    engine = _engine(broker=_FakeBroker(), monkeypatch=monkeypatch)
    expired_close = replace(
        _signal(),
        action=SignalAction.CLOSE,
        stop_loss=None,
        expires_at=datetime.now(tz=UTC) - timedelta(seconds=1),
    )

    with caplog.at_level(logging.WARNING):
        error = engine._gatekeeper.check_signal_freshness(
            signal=expired_close,
            environment="paper",
            trace_ctx={},
        )

    assert error is None
    assert "Expired CLOSE signal allowed" in caplog.text


def test_internal_historical_replay_has_an_idempotent_dedup_namespace(monkeypatch) -> None:
    """A normal rejection must not suppress one explicit replay or permit repeats."""
    broker = _FakeBroker()
    engine = _engine(broker=broker, monkeypatch=monkeypatch)
    stale_signal = replace(
        _signal(),
        timestamp=datetime(2026, 7, 14, 11, tzinfo=UTC),
        metadata={
            "source_price_id": 8783,
            "source_content_revision": 1,
            "source_price_ts": "2026-07-14T11:15:00+00:00",
        },
    )

    normal_result = _run(
        engine,
        stale_signal,
        mode="paper",
        profile=_account_profile(
            broker="paper",
            environment="paper",
            live_enabled=False,
        ),
    )
    replay_result = _run(
        engine,
        stale_signal,
        mode="paper",
        profile=_account_profile(
            broker="paper",
            environment="paper",
            live_enabled=False,
        ),
        allow_historical_replay=True,
    )
    repeated_replay_result = _run(
        engine,
        stale_signal,
        mode="paper",
        profile=_account_profile(
            broker="paper",
            environment="paper",
            live_enabled=False,
        ),
        allow_historical_replay=True,
    )

    assert normal_result.execution_mode == "blocked"
    assert replay_result.success is True
    assert replay_result.orders_filled == 1
    assert repeated_replay_result.execution_mode == "dedup"
    assert broker.submit_calls == 1


def test_live_missing_signal_timestamp_blocks(monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    broker = _FakeBroker()
    engine = _engine(
        broker=broker,
        monkeypatch=monkeypatch,
        sandbox_marker=marker,
    )
    signal = replace(_signal(), timestamp=None)  # type: ignore[arg-type]

    result = _run(engine, signal)

    assert result.execution_mode == "blocked"
    assert "signal timestamp unavailable" in (result.error_message or "")
    assert broker.submit_calls == 0


def test_live_future_signal_timestamp_blocks(monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    broker = _FakeBroker()
    engine = _engine(
        broker=broker,
        monkeypatch=monkeypatch,
        sandbox_marker=marker,
    )
    signal = replace(_signal(), timestamp=datetime.now(tz=UTC) + timedelta(hours=1))

    result = _run(engine, signal)

    assert result.execution_mode == "blocked"
    assert "signal timestamp" in (result.error_message or "")
    assert "future" in (result.error_message or "")
    assert broker.submit_calls == 0


def test_live_missing_market_data_timestamp_blocks(monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    broker = _FakeBroker()
    engine = _engine(
        broker=broker,
        monkeypatch=monkeypatch,
        sandbox_marker=marker,
    )

    async def _market_data_without_timestamp(
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> dict[str, float]:
        del symbol, user_id, environment
        return {"price": 100.0}

    monkeypatch.setattr(engine, "_get_market_data", _market_data_without_timestamp)

    result = _run(engine, _signal())

    assert result.execution_mode == "blocked"
    assert "market data timestamp unavailable" in (result.error_message or "")
    assert broker.submit_calls == 0
    # Stale/absent market data is transient — a fresh poll fixes it, so retry (RG-2).
    assert is_retryable_failure(result) is True
    assert result.block_reason in RETRYABLE_BLOCK_REASONS


def test_live_future_market_data_timestamp_blocks(monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    broker = _FakeBroker()
    engine = _engine(
        broker=broker,
        monkeypatch=monkeypatch,
        sandbox_marker=marker,
    )

    async def _future_market_data(
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> dict[str, object]:
        del symbol, user_id, environment
        return {"price": 100.0, "timestamp": datetime.now(tz=UTC) + timedelta(hours=1)}

    monkeypatch.setattr(engine, "_get_market_data", _future_market_data)

    result = _run(engine, _signal())

    assert result.execution_mode == "blocked"
    assert "market data timestamp" in (result.error_message or "")
    assert "future" in (result.error_message or "")
    assert broker.submit_calls == 0


def test_live_quote_fetch_failure_blocks_without_signal_price_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    broker = _FakeBroker()
    engine = _engine(
        broker=broker,
        monkeypatch=monkeypatch,
        market_data_provider=_FailingMarketDataProvider(),
        sandbox_marker=marker,
    )

    result = _run(engine, _signal())

    assert result.execution_mode == "blocked"
    assert "market data unavailable" in (result.error_message or "")
    assert broker.submit_calls == 0
    # Market data unavailable is transient → retryable (RG-2).
    assert is_retryable_failure(result) is True
    assert result.block_reason == "market_data_missing"


def test_paper_quote_fetch_failure_warns_and_uses_signal_price(caplog, monkeypatch) -> None:
    broker = _FakeBroker()
    engine = _engine(
        broker=broker,
        monkeypatch=monkeypatch,
        market_data_provider=_FailingMarketDataProvider(),
    )

    with caplog.at_level(logging.ERROR):
        result = _run(
            engine,
            _signal(),
            mode="paper",
            profile=_account_profile(
                broker="coinbase",
                environment="paper",
                live_enabled=False,
            ),
        )

    assert result.success is True
    assert result.orders_submitted == 1
    assert broker.submit_calls == 1
    assert "Error fetching market data" in caplog.text


def test_live_stale_account_state_blocks_before_order_build(monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    broker = _FakeBroker(fetched_at=datetime.now(tz=UTC) - timedelta(minutes=10))
    engine = _engine(
        broker=broker,
        monkeypatch=monkeypatch,
        sandbox_marker=marker,
    )

    result = _run(engine, _signal())

    assert result.execution_mode == "blocked"
    assert "account state age" in (result.error_message or "")
    assert broker.submit_calls == 0
    # A stale account snapshot is transient — a fresh fetch refreshes it (RG-2).
    assert is_retryable_failure(result) is True
    assert result.block_reason == "account_state_stale"


def test_live_future_account_state_timestamp_blocks(monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    broker = _FakeBroker(fetched_at=datetime.now(tz=UTC) + timedelta(hours=1))
    engine = _engine(
        broker=broker,
        monkeypatch=monkeypatch,
        sandbox_marker=marker,
    )

    result = _run(engine, _signal())

    assert result.execution_mode == "blocked"
    assert "account state timestamp" in (result.error_message or "")
    assert "future" in (result.error_message or "")
    assert broker.submit_calls == 0


def test_freshness_reasons_are_retryable(monkeypatch) -> None:
    """Drift guard (RG-2): every transient block_reason the engine can emit must be
    in RETRYABLE_BLOCK_REASONS — otherwise that block would be classified terminal,
    the dedup row would go 'failed', and the relay would drop a retriable command.

    Drives every branch of both freshness helpers + the two _execute.py inline
    literals and asserts the emitted reasons are EXACTLY the allow-list, so adding a
    new transient reason without allow-listing it (or vice versa) fails this test.
    """
    engine = _engine(broker=_FakeBroker(), monkeypatch=monkeypatch)
    now = datetime.now(tz=UTC)
    base = StateProvider.account_from_profile(
        _account_profile(fetched_at=now),
        BrokerType.COINBASE,
    )
    reasons: set[str] = set()

    for account_state in (
        replace(base, fetched_at=None),  # type: ignore[arg-type]
        replace(base, fetched_at=now + timedelta(hours=1)),
        replace(base, fetched_at=now - timedelta(hours=1)),
    ):
        result = engine._gatekeeper.check_account_state_freshness(
            account_state=account_state, environment="live", trace_ctx={}
        )
        if result is not None:
            reasons.add(result[0])

    for market_data in (
        {},
        {"price": None},
        {"price": 100.0},
        {"price": 100.0, "timestamp": now + timedelta(hours=1)},
        {"price": 100.0, "timestamp": now - timedelta(hours=1)},
    ):
        result = engine._gatekeeper.check_market_data_freshness(
            market_data=market_data, environment="live", trace_ctx={}
        )
        if result is not None:
            reasons.add(result[0])

    # Transient reasons passed as inline literals or emitted by collaborators
    # not driven through the account/market-data fixtures above.
    reasons.update(
        {
            "account_state_unavailable",
            "market_session_unavailable",
            "risk_baseline_unavailable",
            "sizing_state_unavailable",
        }
    )

    assert reasons == RETRYABLE_BLOCK_REASONS


def test_paper_rebalance_requires_fresh_current_session_quote(monkeypatch) -> None:
    engine = _engine(broker=_FakeBroker(), monkeypatch=monkeypatch)
    now = datetime.now(tz=UTC)
    gate = engine._gatekeeper.check_rebalance_market_data_freshness

    cases = (
        ({}, now - timedelta(minutes=1), "rebalance_market_data_missing"),
        (
            {"price": float("nan"), "timestamp": now},
            now - timedelta(minutes=1),
            "rebalance_market_data_price_invalid",
        ),
        (
            {"price": 100.0},
            now - timedelta(minutes=1),
            "rebalance_market_data_timestamp_missing",
        ),
        (
            {"price": 100.0, "timestamp": now - timedelta(minutes=10)},
            now - timedelta(minutes=5),
            "rebalance_market_data_before_session",
        ),
        (
            {"price": 100.0, "timestamp": now + timedelta(minutes=10)},
            now - timedelta(minutes=1),
            "rebalance_market_data_timestamp_future",
        ),
        (
            {"price": 100.0, "timestamp": now - timedelta(minutes=10)},
            now - timedelta(hours=1),
            "rebalance_market_data_stale",
        ),
    )
    for market_data, expected_open_at, expected_reason in cases:
        result = gate(
            market_data=market_data,
            expected_open_at=expected_open_at,
            environment="paper",
            trace_ctx={},
        )
        assert result is not None
        assert result[0] == expected_reason

    assert (
        gate(
            market_data={"price": 105.0, "timestamp": now},
            expected_open_at=now - timedelta(minutes=1),
            environment="paper",
            trace_ctx={},
        )
        is None
    )


def test_delayed_paper_quote_uses_separate_age_limit_and_is_rejected_for_live(
    monkeypatch,
) -> None:
    engine = _engine(broker=_FakeBroker(), monkeypatch=monkeypatch)
    now = datetime.now(tz=UTC)
    delayed = {
        "price": 100.0,
        "timestamp": now - timedelta(minutes=20),
        "execution_authority": "paper_only",
    }

    assert (
        engine._gatekeeper.check_market_data_freshness(
            market_data=delayed,
            environment="paper",
            trace_ctx={},
        )
        is None
    )
    live_result = engine._gatekeeper.check_market_data_freshness(
        market_data=delayed,
        environment="live",
        trace_ctx={},
    )
    assert live_result is not None
    assert live_result[0] == "paper_only_market_data_live"


def test_profile_fallback_preserves_last_synced_timestamp() -> None:
    last_synced = datetime.now(tz=UTC) - timedelta(minutes=10)

    account = StateProvider.account_from_profile(
        _account_profile(
            broker="paper",
            environment="paper",
            live_enabled=False,
            fetched_at=last_synced,
        ),
        BrokerType.PAPER,
    )

    assert account.fetched_at == last_synced
    assert account.source == "profile_cache"


def test_live_stale_close_exempt_from_freshness_gate(monkeypatch, caplog) -> None:
    """D2: a stale CLOSE (reduce-only exit) passes the live freshness gate with a
    WARNING, while a stale entry is still rejected — an exit must never be
    trapped (H2 principle)."""
    engine = _engine(broker=_FakeBroker(), monkeypatch=monkeypatch)
    stale_ts = datetime.now(tz=UTC) - timedelta(minutes=10)
    stale_close = replace(_signal(), action=SignalAction.CLOSE, timestamp=stale_ts)
    stale_long = replace(_signal(), timestamp=stale_ts)

    with caplog.at_level(logging.WARNING):
        close_error = engine._gatekeeper.check_signal_freshness(
            signal=stale_close, environment="live", trace_ctx={}
        )
    long_error = engine._gatekeeper.check_signal_freshness(
        signal=stale_long,
        environment="live",
        trace_ctx={},
    )

    assert close_error is None
    assert "Stale CLOSE signal allowed" in caplog.text
    assert long_error is not None
    assert "signal age" in long_error


def test_live_stale_close_reaches_broker_while_stale_long_blocked(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """D2 end-to-end: a delayed/retried CLOSE for a live route still reaches the
    broker; a stale LONG with the same age is blocked before any broker call."""
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    broker = _FakeBroker()
    engine = _engine(
        broker=broker,
        monkeypatch=monkeypatch,
        sandbox_marker=marker,
    )

    async def _fresh_market_data(
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> dict[str, object]:
        del symbol, user_id, environment
        return {"price": 100.0, "timestamp": datetime.now(tz=UTC)}

    monkeypatch.setattr(engine, "_get_market_data", _fresh_market_data)
    stale_ts = datetime.now(tz=UTC) - timedelta(minutes=10)

    long_result = _run(engine, replace(_signal(), timestamp=stale_ts))
    assert long_result.execution_mode == "blocked"
    assert broker.submit_calls == 0

    close_result = _run(
        engine,
        replace(_signal(), action=SignalAction.CLOSE, stop_loss=None, timestamp=stale_ts),
    )
    assert close_result.execution_mode != "blocked"
    assert close_result.success is True
    assert "signal age" not in (close_result.error_message or "")
    assert broker.submit_calls == 1
