from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import execution_engine.replay as replay_module
from execution_engine._execute import _attach_historical_replay_fill
from execution_engine.config import BrokerType, ExecutionMode
from execution_engine.models import HISTORICAL_REPLAY_FILL_POLICY, OrderIntent
from execution_engine.replay import ReplayPriceLookup, replay_canonical_signals
from lib_common.internal_events import (
    BrokerRouteSnapshot,
    CanonicalSignalSnapshot,
    ExecutionCommandEvent,
    ExecutionPolicySnapshot,
    ScoreContextSnapshot,
)
from lib_strategy.signals.normalization import normalize_signal_action
from lib_strategy.signals.utils import compute_execution_dedup_key
from scripts.replay_canonical_signals import (
    _run as run_replay_cli,
)
from scripts.replay_canonical_signals import (
    parse_args as parse_replay_args,
)

_ACCOUNT_ID = 42
_BINDING_ID = 7


def test_controlled_replay_attaches_exact_fill_provenance() -> None:
    intent = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="BUY",
        quantity=1,
        order_type="market",
        metadata={"signal_id": "signal-1"},
    )
    resolved = SimpleNamespace(
        allow_historical_replay=True,
        broker_type=BrokerType.PAPER,
        exec_mode=ExecutionMode.SPOT,
        signal=SimpleNamespace(
            metadata={
                "source_price_id": 101,
                "source_content_revision": 3,
                "source_price_ts": "2026-07-14T11:15:00+00:00",
            }
        ),
    )

    _attach_historical_replay_fill([intent], resolved)  # type: ignore[arg-type]

    assert intent.metadata == {
        "signal_id": "signal-1",
        "historical_replay": True,
        "source_price_id": 101,
        "source_content_revision": 3,
        "source_price_ts": "2026-07-14T11:15:00+00:00",
        "trigger_policy_version": HISTORICAL_REPLAY_FILL_POLICY,
    }


class _ScalarResult:
    def __init__(self, value):  # type: ignore[no-untyped-def]
        self._value = value

    def scalars(self):  # type: ignore[no-untyped-def]
        return self

    def first(self):  # type: ignore[no-untyped-def]
        return self._value

    def all(self):  # type: ignore[no-untyped-def]
        return [] if self._value is None else [self._value]


class _RowsResult:
    def __init__(self, rows):  # type: ignore[no-untyped-def]
        self._rows = rows

    def all(self):  # type: ignore[no-untyped-def]
        return list(self._rows)

    def scalars(self):  # type: ignore[no-untyped-def]
        return self

    def first(self):  # type: ignore[no-untyped-def]
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, *, rows=None, scalar=None):  # type: ignore[no-untyped-def]
        self._rows = rows
        self._scalar = scalar
        self.statements: list[Any] = []

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        return None

    def execute(self, stmt):  # type: ignore[no-untyped-def]
        self.statements.append(stmt)
        if self._rows is not None:
            return _RowsResult(self._rows)
        return _ScalarResult(self._scalar)

    def get(self, _model, _identity):  # type: ignore[no-untyped-def]
        return self._scalar


class _SequencedSessionFactory:
    def __init__(self, sessions):  # type: ignore[no-untyped-def]
        self._sessions = list(sessions)

    def __call__(self):  # type: ignore[no-untyped-def]
        return self._sessions.pop(0)


class _ReplayRow:
    def __init__(self, canonical, symbol: str, asset_class: str) -> None:  # type: ignore[no-untyped-def]
        self._canonical = canonical
        self.symbol = symbol
        self.asset_class = asset_class

    def __getitem__(self, index: int):  # type: ignore[no-untyped-def]
        if index == 0:
            return self._canonical
        raise IndexError(index)


class _ReplayBroker:
    async def get_account_info(self):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            equity=100_100.0,
            unrealized_pnl=0.0,
            realized_pnl=100.0,
        )


def _source_price(*, price: float = 101.25) -> SimpleNamespace:
    return SimpleNamespace(
        open=price,
        price_id=9001,
        content_revision=1,
        ts=datetime(2026, 3, 6, 10, 30, tzinfo=UTC),
        source="coinbase_live",
        timeframe="1m",
    )


def _strategy_version(*, semver: str = "1.0.1") -> SimpleNamespace:
    return SimpleNamespace(
        strat_ver_id=1108,
        strategy_id="swing_high_low_pmo_v1",
        semver=semver,
    )


def _patch_command_instrument_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expected_symbol: str,
    instr_id: int = 11,
) -> list[tuple[str, str | None]]:
    calls: list[tuple[str, str | None]] = []

    def _resolve(
        _session: Any,
        symbol: str,
        *,
        asset_class: str | None = None,
    ) -> SimpleNamespace:
        calls.append((symbol, asset_class))
        assert symbol == expected_symbol
        return SimpleNamespace(instr_id=instr_id, canonical="BTC/USDC")

    monkeypatch.setattr(replay_module, "resolve_instrument", _resolve)
    return calls


class _RecordingReplayEngine:
    def __init__(self) -> None:
        self.signals = []
        self.calls = []

    async def handle_signal(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        self.signals.append(kwargs["signal"])
        return SimpleNamespace(success=True, orders_filled=1, execution_mode="paper")

    async def _get_broker(self, **_kwargs):  # type: ignore[no-untyped-def]
        return _ReplayBroker()


def _replay_context() -> tuple[dict[str, object], dict[str, object]]:
    account_id = _ACCOUNT_ID
    credential_ref = "paper-account-42"
    profile: dict[str, object] = {
        "broker": "paper",
        "broker_account_id": account_id,
        "credential_ref": credential_ref,
        "sandbox": True,
        "_broker_route_snapshot": {
            "broker": "paper",
            "broker_environment": "paper",
            "broker_account_id": account_id,
            "credential_ref": credential_ref,
        },
        "accounts": {
            str(account_id): {
                "broker": "paper",
                "environment": "paper",
                "status": "connected",
                "base_ccy": "USD",
                "paper_initial_equity": 100_000.0,
                "paper_initial_cash": 100_000.0,
                "binding_id": _BINDING_ID,
            }
        },
    }
    strategy_config: dict[str, object] = {
        "broker": "paper",
        "mode": "paper",
        "binding_id": _BINDING_ID,
        "credential_ref": credential_ref,
    }
    return profile, strategy_config


def _execution_contract_rows(
    canonical: SimpleNamespace,
    *,
    symbol: str,
    outbox_status: str = "published",
    command_policy: ExecutionPolicySnapshot | None = None,
    command_strategy_version: str = "1.0.1",
) -> tuple[SimpleNamespace, SimpleNamespace]:
    action = normalize_signal_action(canonical.action).value.lower()
    signal_id = f"domain-{canonical.signal_id}"
    policy_config = {
        "user_id": "user-1",
        "binding_id": _BINDING_ID,
        "strategy_id": canonical.strategy_id,
        "broker_account_id": _ACCOUNT_ID,
        "broker": "paper",
        "execution_mode": "spot",
        "execution_modes_allowed": ["spot"],
        "allowed_brokers": ["paper"],
        "autopilot": True,
        "entries_enabled": True,
        "exits_enabled": True,
        "sizing": {"method": "fixed_pct", "fixed_pct": 0.03},
        "risk_caps": {"max_position_pct": 0.08},
    }
    policy = command_policy or ExecutionPolicySnapshot(
        user_id="user-1",
        strategy_id=canonical.strategy_id,
        binding_id=_BINDING_ID,
        autopilot=True,
        entries_enabled=True,
        exits_enabled=True,
        execution_mode="spot",
        execution_modes_allowed=["spot"],
        allowed_brokers=["paper"],
        sizing=policy_config["sizing"],
        risk_caps=policy_config["risk_caps"],
        config=policy_config,
    )
    route = BrokerRouteSnapshot(
        broker="paper",
        broker_account_id=_ACCOUNT_ID,
        broker_environment="paper",
        credential_ref=None,
        allowed_brokers=["paper"],
        route_source="scoring_policy",
        live_enabled=False,
        sandbox=True,
        asset_class="crypto",
        execution_mode="spot",
    )
    command = ExecutionCommandEvent(
        run_id=canonical.run_id,
        correlation_id=signal_id,
        causation_id=signal_id,
        producer="scoring_engine",
        user_id="user-1",
        signal=CanonicalSignalSnapshot(
            signal_id=signal_id,
            strategy_id=canonical.strategy_id,
            strategy_type="indicator",
            symbol=symbol,
            action=action,
            confidence=float(canonical.confidence),
            timestamp=canonical.ts,
            expected_return=canonical.expected_return,
            predicted_risk=canonical.predicted_risk,
            entry_price=canonical.entry_price,
            stop_loss=(canonical.signal_meta or {}).get("stop_loss"),
            take_profit=(canonical.signal_meta or {}).get("take_profit"),
            asset_class="crypto",
            instrument_id=str(canonical.instr_id),
            strategy_version=command_strategy_version,
            run_id=canonical.run_id,
            source="paper",
            external_signal_id=canonical.external_signal_id,
            metadata={
                **dict(canonical.signal_meta or {}),
                "canonical_signal_id": canonical.signal_id,
            },
        ),
        score_context=ScoreContextSnapshot(
            asset_score=0.75,
            recommended_mode="spot",
            score_target=symbol,
        ),
        execution_policy=policy,
        broker_route=route,
        profile={
            "user_id": "user-1",
            "broker": "paper",
            "broker_account_id": _ACCOUNT_ID,
            "broker_environment": "paper",
            "sandbox": True,
        },
        user_strategy_config=policy.config,
    )
    decision = SimpleNamespace(
        decision_id=101,
        binding_id=_BINDING_ID,
        instr_id=canonical.instr_id,
        canonical_signal_id=canonical.signal_id,
        broker_account_id=_ACCOUNT_ID,
        should_execute=True,
        lineage_schema_version="v1",
        signal_id=signal_id,
        action=action,
        run_id=canonical.run_id,
        idempotency_key=compute_execution_dedup_key(
            canonical.external_signal_id,
            "user-1",
            _ACCOUNT_ID,
            symbol,
            action,
        ),
        execution_mode="spot",
        binding_config_snapshot=policy.model_dump(mode="json"),
        broker_route_snapshot=route.model_dump(mode="json"),
    )
    event_key = f"execution-command:{canonical.external_signal_id}:user-1:{_BINDING_ID}:{action}"
    outbox = SimpleNamespace(
        event_id="outbox-101",
        topic="execution.commands",
        event_type="ExecutionCommand",
        schema_version="v1",
        aggregate_type="execution_command",
        aggregate_id=f"{canonical.external_signal_id}:user-1",
        event_key=event_key,
        status=outbox_status,
        payload=command.model_dump(mode="json"),
    )
    return decision, outbox


def test_replay_price_lookup_rounds_to_next_15m_boundary() -> None:
    lookup = ReplayPriceLookup(session_factory=lambda: _FakeSession(scalar=None), timeframe="15m")

    assert lookup.next_fill_timestamp(datetime(2026, 3, 6, 10, 15)) == datetime(2026, 3, 6, 10, 30)
    assert lookup.next_fill_timestamp(datetime(2026, 3, 6, 10, 16, 42)) == datetime(
        2026, 3, 6, 10, 30
    )


def test_replay_price_lookup_requires_supported_timeframe() -> None:
    lookup = ReplayPriceLookup(session_factory=lambda: _FakeSession(scalar=None), timeframe="1m")

    try:
        lookup.next_fill_timestamp(datetime(2026, 3, 6, 10, 15))
    except ValueError as exc:
        assert "Only 15m replay is supported" in str(exc)  # noqa: PT017
    else:
        raise AssertionError("Expected unsupported timeframe error")


def test_replay_price_lookup_uses_exact_next_15m_open() -> None:
    session = _FakeSession(scalar=_source_price())
    lookup = ReplayPriceLookup(
        session_factory=lambda: session,
        timeframe="15m",
        source="coinbase_live",
    )

    price = lookup.next_open(
        instr_id=11,
        signal_ts=datetime(2026, 3, 6, 10, 16),
    )

    assert price == 101.25
    statement = session.statements[0]
    compiled = statement.compile()
    assert "prices.source = " in str(statement)
    assert "prices.source LIKE " not in str(statement)
    assert "coinbase_live" in compiled.params.values()


def test_replay_requires_minute_data_for_missing_next_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = SimpleNamespace(
        signal_id=1,
        external_signal_id="external-sig-1",
        instr_id=11,
        strat_ver_id=1108,
        ts=datetime(2026, 3, 6, 10, 16),
        strategy_id="swing_high_low_pmo_v1",
        source_runner="indicator",
        action="long",
        confidence=0.7,
        expected_return=0.02,
        predicted_risk=None,
        entry_price=100.0,
        signal_meta={},
        run_id="run-1",
        raw_score=None,
        features={},
    )
    decision, outbox = _execution_contract_rows(canonical, symbol="BTC-USD")
    _patch_command_instrument_resolution(monkeypatch, expected_symbol="BTC-USD")
    session_factory = _SequencedSessionFactory(
        [
            _FakeSession(rows=[_ReplayRow(canonical, "BTC-USD", "crypto")]),
            _FakeSession(rows=[decision]),
            _FakeSession(rows=[outbox]),
            _FakeSession(scalar=_strategy_version()),
            _FakeSession(scalar=None),
        ]
    )
    profile, strategy_config = _replay_context()

    async def _run() -> None:
        await replay_canonical_signals(
            engine=SimpleNamespace(),
            session_factory=session_factory,
            user_id="user-1",
            strategy_id="swing_high_low_pmo_v1",
            profile=profile,
            user_strategy_config=strategy_config,
            timeframe="15m",
            require_minute_data=True,
            starting_equity=100_000.0,
        )

    try:
        asyncio.run(_run())
    except RuntimeError as exc:
        assert "Replay requires 1m price data for next 15m open fills" in str(exc)  # noqa: PT017
        assert "BTC-USD" in str(exc)  # noqa: PT017
    else:
        raise AssertionError("Expected missing minute data error")


def test_replay_resolves_command_alias_and_carries_canonical_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = SimpleNamespace(
        signal_id=2,
        external_signal_id="external-sig-2",
        instr_id=11,
        strat_ver_id=1108,
        ts=datetime(2026, 3, 6, 10, 16),
        strategy_id="swing_high_low_pmo_v1",
        source_runner="indicator",
        action="long",
        confidence=0.7,
        expected_return=0.02,
        predicted_risk=None,
        entry_price=100.0,
        signal_meta={},
        run_id="run-2",
        raw_score=None,
        features={},
    )
    decision, outbox = _execution_contract_rows(canonical, symbol="BTCUSDC")
    resolution_calls = _patch_command_instrument_resolution(
        monkeypatch,
        expected_symbol="BTCUSDC",
    )
    session_factory = _SequencedSessionFactory(
        [
            _FakeSession(rows=[_ReplayRow(canonical, "BTC/USDC", "crypto")]),
            _FakeSession(rows=[decision]),
            _FakeSession(rows=[outbox]),
            _FakeSession(scalar=_strategy_version()),
            _FakeSession(scalar=_source_price()),
        ]
    )
    engine = _RecordingReplayEngine()
    profile, strategy_config = _replay_context()

    summary = asyncio.run(
        replay_canonical_signals(
            engine=engine,  # type: ignore[arg-type]
            session_factory=session_factory,
            user_id="user-1",
            strategy_id="swing_high_low_pmo_v1",
            profile=profile,
            user_strategy_config=strategy_config,
            starting_equity=100_000.0,
        )
    )

    assert summary.signals_processed == 1
    assert resolution_calls == [("BTCUSDC", "crypto")]
    assert engine.signals[0].symbol == "BTCUSDC"
    assert engine.signals[0].external_signal_id == "external-sig-2"
    assert engine.signals[0].instrument_id == "11"
    assert engine.signals[0].metadata["replay_decision_id"] == 101
    assert engine.signals[0].metadata["replay_outbox_event_id"] == "outbox-101"
    assert engine.calls[0]["score_context"]["asset_score"] == 0.75
    assert engine.calls[0]["profile"]["_broker_route_snapshot"]["route_source"] == "scoring_policy"
    assert (
        engine.calls[0]["user_strategy_config"]["_execution_policy_snapshot"]["sizing"]["fixed_pct"]
        == 0.03
    )
    assert engine.calls[0]["allow_historical_replay"] is True


def test_replay_requires_exact_canonical_strategy_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = SimpleNamespace(
        signal_id=6,
        external_signal_id="external-sig-6",
        instr_id=11,
        strat_ver_id=1108,
        ts=datetime(2026, 3, 6, 10, 16),
        strategy_id="swing_high_low_pmo_v1",
        source_runner="indicator",
        action="long",
        confidence=0.7,
        expected_return=0.02,
        predicted_risk=None,
        entry_price=100.0,
        signal_meta={},
        run_id="run-6",
        raw_score=None,
        features={},
    )
    decision, outbox = _execution_contract_rows(
        canonical,
        symbol="BTCUSDC",
        command_strategy_version="1.0.0",
    )
    _patch_command_instrument_resolution(monkeypatch, expected_symbol="BTCUSDC")
    session_factory = _SequencedSessionFactory(
        [
            _FakeSession(rows=[_ReplayRow(canonical, "BTC/USDC", "crypto")]),
            _FakeSession(rows=[decision]),
            _FakeSession(rows=[outbox]),
            _FakeSession(scalar=_strategy_version(semver="1.0.1")),
        ]
    )
    profile, strategy_config = _replay_context()

    with pytest.raises(ValueError, match="strategy version differs"):
        asyncio.run(
            replay_canonical_signals(
                engine=_RecordingReplayEngine(),  # type: ignore[arg-type]
                session_factory=session_factory,
                user_id="user-1",
                strategy_id="swing_high_low_pmo_v1",
                profile=profile,
                user_strategy_config=strategy_config,
                starting_equity=100_000.0,
            )
        )


def test_replay_end_date_is_exclusive() -> None:
    query_session = _FakeSession(rows=[])
    session_factory = _SequencedSessionFactory([query_session])
    profile, strategy_config = _replay_context()

    asyncio.run(
        replay_canonical_signals(
            engine=_RecordingReplayEngine(),  # type: ignore[arg-type]
            session_factory=session_factory,
            user_id="user-1",
            strategy_id="swing_high_low_pmo_v1",
            profile=profile,
            user_strategy_config=strategy_config,
            end_date=datetime(2026, 3, 7),
            starting_equity=100_000.0,
        )
    )

    statement = str(query_session.statements[0])
    assert "canonical_signals.ts < " in statement
    assert "canonical_signals.ts <= " not in statement


def test_replay_rejects_unknown_canonical_action() -> None:
    canonical = SimpleNamespace(
        signal_id="sig-unknown",
        external_signal_id="external-sig-unknown",
        instr_id=11,
        strat_ver_id=1108,
        ts=datetime(2026, 3, 6, 10, 16),
        strategy_id="swing_high_low_pmo_v1",
        source_runner="indicator",
        action="unexpected",
        confidence=0.7,
        expected_return=0.02,
        predicted_risk=None,
        entry_price=100.0,
        signal_meta={},
        run_id="run-unknown",
        raw_score=None,
        features={},
    )
    session_factory = _SequencedSessionFactory(
        [
            _FakeSession(rows=[_ReplayRow(canonical, "BTC-USDC", "crypto")]),
            _FakeSession(scalar=_source_price()),
        ]
    )
    profile, strategy_config = _replay_context()

    async def _run() -> None:
        await replay_canonical_signals(
            engine=_RecordingReplayEngine(),  # type: ignore[arg-type]
            session_factory=session_factory,
            user_id="user-1",
            strategy_id="swing_high_low_pmo_v1",
            profile=profile,
            user_strategy_config=strategy_config,
            starting_equity=100_000.0,
        )

    with pytest.raises(ValueError, match="unknown canonical action"):
        asyncio.run(_run())


def test_replay_requires_exact_persisted_should_execute_decision() -> None:
    canonical = SimpleNamespace(
        signal_id=3,
        external_signal_id="external-sig-3",
        instr_id=11,
        strat_ver_id=1108,
        ts=datetime(2026, 3, 6, 10, 16),
        strategy_id="swing_high_low_pmo_v1",
        source_runner="indicator",
        action="long",
        confidence=0.7,
        expected_return=0.02,
        predicted_risk=None,
        entry_price=100.0,
        signal_meta={},
        run_id="run-3",
        raw_score=None,
        features={},
    )
    session_factory = _SequencedSessionFactory(
        [
            _FakeSession(rows=[_ReplayRow(canonical, "BTC-USDC", "crypto")]),
            _FakeSession(rows=[]),
        ]
    )
    profile, strategy_config = _replay_context()

    with pytest.raises(ValueError, match="exactly one persisted should_execute decision"):
        asyncio.run(
            replay_canonical_signals(
                engine=_RecordingReplayEngine(),  # type: ignore[arg-type]
                session_factory=session_factory,
                user_id="user-1",
                strategy_id="swing_high_low_pmo_v1",
                profile=profile,
                user_strategy_config=strategy_config,
                starting_equity=100_000.0,
            )
        )


def test_replay_requires_published_execution_command() -> None:
    canonical = SimpleNamespace(
        signal_id=4,
        external_signal_id="external-sig-4",
        instr_id=11,
        strat_ver_id=1108,
        ts=datetime(2026, 3, 6, 10, 16),
        strategy_id="swing_high_low_pmo_v1",
        source_runner="indicator",
        action="long",
        confidence=0.7,
        expected_return=0.02,
        predicted_risk=None,
        entry_price=100.0,
        signal_meta={},
        run_id="run-4",
        raw_score=None,
        features={},
    )
    decision, outbox = _execution_contract_rows(
        canonical,
        symbol="BTC-USDC",
        outbox_status="failed",
    )
    session_factory = _SequencedSessionFactory(
        [
            _FakeSession(rows=[_ReplayRow(canonical, "BTC-USDC", "crypto")]),
            _FakeSession(rows=[decision]),
            _FakeSession(rows=[outbox]),
        ]
    )
    profile, strategy_config = _replay_context()

    with pytest.raises(ValueError, match="event to be published"):
        asyncio.run(
            replay_canonical_signals(
                engine=_RecordingReplayEngine(),  # type: ignore[arg-type]
                session_factory=session_factory,
                user_id="user-1",
                strategy_id="swing_high_low_pmo_v1",
                profile=profile,
                user_strategy_config=strategy_config,
                starting_equity=100_000.0,
            )
        )


def test_replay_rejects_command_policy_different_from_decision_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = SimpleNamespace(
        signal_id=5,
        external_signal_id="external-sig-5",
        instr_id=11,
        strat_ver_id=1108,
        ts=datetime(2026, 3, 6, 10, 16),
        strategy_id="swing_high_low_pmo_v1",
        source_runner="indicator",
        action="long",
        confidence=0.7,
        expected_return=0.02,
        predicted_risk=None,
        entry_price=100.0,
        signal_meta={},
        run_id="run-5",
        raw_score=None,
        features={},
    )
    decision, outbox = _execution_contract_rows(canonical, symbol="BTC-USDC")
    decision.binding_config_snapshot = {
        **decision.binding_config_snapshot,
        "entries_enabled": False,
    }
    _patch_command_instrument_resolution(monkeypatch, expected_symbol="BTC-USDC")
    session_factory = _SequencedSessionFactory(
        [
            _FakeSession(rows=[_ReplayRow(canonical, "BTC-USDC", "crypto")]),
            _FakeSession(rows=[decision]),
            _FakeSession(rows=[outbox]),
            _FakeSession(scalar=_strategy_version()),
        ]
    )
    profile, strategy_config = _replay_context()

    with pytest.raises(ValueError, match="command policy differs"):
        asyncio.run(
            replay_canonical_signals(
                engine=_RecordingReplayEngine(),  # type: ignore[arg-type]
                session_factory=session_factory,
                user_id="user-1",
                strategy_id="swing_high_low_pmo_v1",
                profile=profile,
                user_strategy_config=strategy_config,
                starting_equity=100_000.0,
            )
        )


def test_replay_cli_constructs_bounded_canary_arguments() -> None:
    args = parse_replay_args(
        [
            "--user-id",
            "demo_user",
            "--broker-account-id",
            "42",
            "--strategy-id",
            "swing_high_low_pmo_v1",
            "--symbols",
            "BTC-USDC",
            "--start-date",
            "2026-07-14",
            "--end-date",
            "2026-07-15",
            "--timeframe",
            "15m",
            "--source",
            "coinbase_live",
            "--max-signals",
            "1",
            "--require-minute-data",
            "--no-enable-shorting",
        ]
    )

    assert args.broker_account_id == 42
    assert args.require_minute_data is True
    assert args.enable_shorting is False
    assert not hasattr(args, "max_open_positions")
    assert not hasattr(args, "max_daily_trades")


def test_replay_cli_refuses_operator_shorting_before_database_access() -> None:
    args = parse_replay_args(
        [
            "--user-id",
            "demo_user",
            "--broker-account-id",
            "42",
            "--symbols",
            "BTC-USDC",
            "--enable-shorting",
        ]
    )

    with pytest.raises(ValueError, match="cannot grant shorting"):
        asyncio.run(run_replay_cli(args))


@pytest.mark.parametrize("requested_owner", ["owner", "foreign"])
def test_replay_cli_scopes_metadata_and_disposes_on_failure(monkeypatch, requested_owner):
    from contextlib import contextmanager

    import scripts.replay_canonical_signals as cli

    events = []
    database_engine = object()

    class StopAfterMetadataError(Exception):
        pass

    class MetadataSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def query(self, *args):
            assert events[-1] == "scope:owner"
            events.append("metadata")
            raise StopAfterMetadataError

    @contextmanager
    def scope(session, *, user_id):
        events.append(f"scope:{user_id}")
        try:
            yield
        finally:
            events.append("scope-exit")

    monkeypatch.setattr(cli, "create_engine_for_env", lambda **kwargs: database_engine)
    monkeypatch.setattr(cli, "get_session_factory", lambda **kwargs: MetadataSession)
    monkeypatch.setattr(cli, "dispose_engine", lambda engine: events.append("dispose"))
    monkeypatch.setattr(cli, "require_deployment_owner_id", lambda session: "owner")
    monkeypatch.setattr(cli, "tenant_scope", scope)
    args = parse_replay_args(
        ["--user-id", requested_owner, "--broker-account-id", "42", "--symbols", "BTC-USDC"]
    )
    with pytest.raises(StopAfterMetadataError if requested_owner == "owner" else ValueError):
        asyncio.run(run_replay_cli(args))
    assert events == (
        ["scope:owner", "metadata", "scope-exit", "dispose"]
        if requested_owner == "owner"
        else ["dispose"]
    )
