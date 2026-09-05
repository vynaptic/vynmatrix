"""Replay canonical signals through the execution engine in paper mode."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select

from lib_application.db.models import (
    CanonicalSignal,
    ExecutionDecisionLog,
    Instrument,
    InstrumentPrice,
    OutboxEvent,
    StrategyVersion,
)
from lib_application.services.instrument_resolution import resolve_instrument
from lib_common.internal_events import (
    BrokerRouteSnapshot,
    ExecutionCommandEvent,
    ExecutionPolicySnapshot,
)
from lib_common.logging import get_logger
from lib_strategy.signals.normalization import is_known_signal_action, normalize_signal_action
from lib_strategy.signals.signal import Signal
from lib_strategy.signals.utils import compute_execution_dedup_key, ensure_utc

from .config import BrokerType
from .engine import ExecutionEngine

logger = get_logger(__name__)
_MAX_CURRENCY_CODE_LENGTH = 10


def _canonical_runner_to_strategy_type(value: str | None) -> str:
    normalized = str(value or "indicator").strip().lower()
    if normalized != "indicator":
        msg = f"Unsupported canonical signal source_runner: {value!r}"
        raise ValueError(msg)
    return normalized


@dataclass
class ReplaySummary:
    user_id: str
    strategy_id: str
    signals_processed: int
    signals_skipped_missing_price: int
    executed_results: int
    blocked_results: int
    failed_results: int
    orders_filled: int
    ending_equity: float
    realized_pnl: float
    unrealized_pnl: float
    starting_equity: float


@dataclass(frozen=True)
class ReplayPriceObservation:
    """Exact persisted source row used for a historical paper fill."""

    price: float
    price_id: int
    content_revision: int
    timestamp: datetime
    source: str
    timeframe: str


@dataclass(frozen=True)
class ReplayExecutionContract:
    """Exact scored decision and published command authorizing one replay."""

    decision_id: int
    outbox_event_id: str
    command: ExecutionCommandEvent


class ReplayPriceLookup:
    """Resolve replay prices from InstrumentPrice using next 15-minute bar open fills."""

    def __init__(
        self,
        *,
        session_factory: Any,
        timeframe: str = "15m",
        source: str = "coinbase_live",
        source_timeframe: str = "1m",
    ) -> None:
        self._session_factory = session_factory
        self._timeframe = timeframe
        self._source = source
        self._source_timeframe = source_timeframe

    def next_fill_timestamp(self, signal_ts: datetime) -> datetime:
        """Return the next 15-minute boundary strictly after ``signal_ts``."""
        if self._timeframe != "15m":
            msg = f"Unsupported replay timeframe: {self._timeframe}. Only 15m replay is supported."
            raise ValueError(msg)

        normalized = signal_ts.replace(second=0, microsecond=0)
        minute_mod = normalized.minute % 15
        minutes_to_add = 15 if minute_mod == 0 else 15 - minute_mod
        return normalized + timedelta(minutes=minutes_to_add)

    def next_observation(
        self,
        *,
        instr_id: int,
        signal_ts: datetime,
    ) -> ReplayPriceObservation | None:
        """Return the exact next-bar source observation used for replay."""
        fill_ts = self.next_fill_timestamp(signal_ts)
        with self._session_factory() as session:
            row = (
                session.execute(
                    select(InstrumentPrice)
                    .where(
                        InstrumentPrice.instr_id == instr_id,
                        InstrumentPrice.timeframe == self._source_timeframe,
                        InstrumentPrice.source == self._source,
                        InstrumentPrice.ts == fill_ts,
                    )
                    .limit(1)
                )
                .scalars()
                .first()
            )
        if row is None:
            return None
        timestamp = row.ts if row.ts.tzinfo is not None else row.ts.replace(tzinfo=UTC)
        return ReplayPriceObservation(
            price=float(row.open),
            price_id=int(row.price_id),
            content_revision=int(row.content_revision),
            timestamp=timestamp.astimezone(UTC),
            source=str(row.source),
            timeframe=str(row.timeframe),
        )

    def next_open(self, *, instr_id: int, signal_ts: datetime) -> float | None:
        """Return only the next-bar open for callers that do not need provenance."""
        observation = self.next_observation(instr_id=instr_id, signal_ts=signal_ts)
        return observation.price if observation is not None else None


def _normalized_execution_action(value: Any) -> str:
    if not is_known_signal_action(value):
        msg = f"Replay execution contract has an unknown action: {value!r}"
        raise ValueError(msg)
    return normalize_signal_action(value).value.lower()


def _require_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        msg = f"Canonical replay {field_name} must be a positive integer"
        raise TypeError(msg)
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        msg = f"Canonical replay {field_name} must be a positive integer"
        raise ValueError(msg) from exc
    if normalized <= 0:
        msg = f"Canonical replay {field_name} must be a positive integer"
        raise ValueError(msg)
    return normalized


def _load_scored_replay_decision(
    *,
    session_factory: Any,
    canonical: CanonicalSignal,
    user_id: str,
    broker_account_id: int,
    expected_binding_id: int,
) -> ExecutionDecisionLog:
    """Load the one exact positive scoring decision for a replay signal."""
    canonical_signal_id = _require_positive_int(
        canonical.signal_id,
        field_name="canonical_signal_id",
    )
    action = _normalized_execution_action(canonical.action)
    external_signal_id = str(canonical.external_signal_id or "").strip()
    if not external_signal_id:
        msg = f"Canonical signal {canonical_signal_id} has no external identity"
        raise ValueError(msg)

    with session_factory() as session:
        decisions = (
            session.execute(
                select(ExecutionDecisionLog).where(
                    ExecutionDecisionLog.canonical_signal_id == canonical_signal_id,
                    ExecutionDecisionLog.user_id == user_id,
                    ExecutionDecisionLog.broker_account_id == broker_account_id,
                    ExecutionDecisionLog.should_execute.is_(True),
                    ExecutionDecisionLog.lineage_schema_version == "v1",
                )
            )
            .scalars()
            .all()
        )
    if len(decisions) != 1:
        msg = (
            "Canonical replay requires exactly one persisted should_execute decision "
            f"for signal={canonical_signal_id} user={user_id!r} "
            f"account={broker_account_id}; found {len(decisions)}"
        )
        raise ValueError(msg)
    decision = cast(ExecutionDecisionLog, decisions[0])
    binding_id = _require_positive_int(decision.binding_id, field_name="decision binding_id")
    if binding_id != expected_binding_id:
        msg = (
            f"Canonical replay decision binding {binding_id} differs from current "
            f"binding {expected_binding_id}"
        )
        raise ValueError(msg)
    if _require_positive_int(
        decision.instr_id,
        field_name="decision instrument_id",
    ) != _require_positive_int(canonical.instr_id, field_name="canonical instrument_id"):
        msg = "Canonical replay decision instrument differs from its canonical signal"
        raise ValueError(msg)
    if _normalized_execution_action(decision.action) != action:
        msg = "Canonical replay decision action differs from its canonical signal"
        raise ValueError(msg)
    return decision


def _load_published_replay_command(
    *,
    session_factory: Any,
    canonical: CanonicalSignal,
    decision: ExecutionDecisionLog,
    user_id: str,
) -> tuple[str, ExecutionCommandEvent]:
    """Load the published execution command corresponding to one decision."""
    action = _normalized_execution_action(canonical.action)
    external_signal_id = str(canonical.external_signal_id)
    binding_id = _require_positive_int(
        decision.binding_id,
        field_name="decision binding_id",
    )
    event_key = f"execution-command:{external_signal_id}:{user_id}:{binding_id}:{action}"
    with session_factory() as session:
        events = (
            session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.topic == "execution.commands",
                    OutboxEvent.event_type == "ExecutionCommand",
                    OutboxEvent.event_key == event_key,
                )
            )
            .scalars()
            .all()
        )
    if len(events) != 1:
        msg = (
            "Canonical replay requires exactly one corresponding execution.commands "
            f"outbox event for decision={decision.decision_id}; found {len(events)}"
        )
        raise ValueError(msg)
    outbox = events[0]
    if str(outbox.status) != "published":
        msg = (
            "Canonical replay requires the corresponding execution.commands event "
            f"to be published; event={outbox.event_id} status={outbox.status!r}"
        )
        raise ValueError(msg)
    if str(outbox.aggregate_type or "") != "execution_command":
        msg = "Canonical replay outbox aggregate type is not execution_command"
        raise ValueError(msg)
    expected_aggregate_id = f"{external_signal_id}:{user_id}"
    if str(outbox.aggregate_id or "") != expected_aggregate_id:
        msg = "Canonical replay outbox aggregate identity differs from its signal/user"
        raise ValueError(msg)
    command = ExecutionCommandEvent.model_validate(outbox.payload)
    if str(outbox.schema_version) != command.schema_version:
        msg = "Canonical replay outbox schema version differs from its command payload"
        raise ValueError(msg)
    return str(outbox.event_id), command


def _validate_replay_catalogue_lineage(
    *,
    session_factory: Any,
    canonical: CanonicalSignal,
    command: ExecutionCommandEvent,
    asset_class: str,
) -> None:
    """Require the command to resolve to the exact instrument and strategy version."""
    command_instrument_id = _require_positive_int(
        command.signal.instrument_id,
        field_name="command instrument_id",
    )
    if command_instrument_id != int(canonical.instr_id):
        msg = "Canonical replay command instrument differs from its canonical signal"
        raise ValueError(msg)
    canonical_strategy_version_id = _require_positive_int(
        canonical.strat_ver_id,
        field_name="canonical strategy_version_id",
    )
    with session_factory() as session:
        resolved_command_instrument = resolve_instrument(
            session,
            command.signal.symbol,
            asset_class=asset_class,
        )
        canonical_strategy_version = session.get(
            StrategyVersion,
            canonical_strategy_version_id,
        )
    if resolved_command_instrument is None or int(resolved_command_instrument.instr_id) != int(
        canonical.instr_id
    ):
        msg = "Canonical replay command symbol does not resolve to its canonical instrument"
        raise ValueError(msg)
    if (
        canonical_strategy_version is None
        or canonical_strategy_version.strategy_id != canonical.strategy_id
        or command.signal.strategy_version != canonical_strategy_version.semver
    ):
        msg = "Canonical replay command strategy version differs from its canonical signal"
        raise ValueError(msg)


def _validate_replay_command_lineage(
    *,
    session_factory: Any,
    canonical: CanonicalSignal,
    decision: ExecutionDecisionLog,
    command: ExecutionCommandEvent,
    asset_class: str,
    user_id: str,
    broker_account_id: int,
) -> None:
    """Validate immutable signal, decision, and command identities."""
    action = _normalized_execution_action(canonical.action)
    external_signal_id = str(canonical.external_signal_id)
    if command.user_id != user_id:
        msg = "Canonical replay command user differs from its scored decision"
        raise ValueError(msg)
    if command.signal.external_signal_id != external_signal_id:
        msg = "Canonical replay command external identity differs from its canonical signal"
        raise ValueError(msg)
    if command.signal.strategy_id != canonical.strategy_id:
        msg = "Canonical replay command strategy differs from its canonical signal"
        raise ValueError(msg)
    if command.signal.strategy_type != _canonical_runner_to_strategy_type(canonical.source_runner):
        msg = "Canonical replay command strategy type differs from its canonical signal"
        raise ValueError(msg)
    if command.signal.asset_class != asset_class:
        msg = "Canonical replay command asset class differs from its canonical instrument"
        raise ValueError(msg)
    if _normalized_execution_action(command.signal.action) != action:
        msg = "Canonical replay command action differs from its canonical signal"
        raise ValueError(msg)
    _validate_replay_catalogue_lineage(
        session_factory=session_factory,
        canonical=canonical,
        command=command,
        asset_class=asset_class,
    )
    expected_dedup_key = compute_execution_dedup_key(
        external_signal_id,
        user_id,
        broker_account_id,
        command.signal.symbol,
        action,
    )
    if str(decision.idempotency_key) != expected_dedup_key:
        msg = "Canonical replay decision has a non-canonical execution idempotency key"
        raise ValueError(msg)
    if str(decision.signal_id or "") != command.signal.signal_id:
        msg = "Canonical replay command signal identity differs from its decision"
        raise ValueError(msg)
    if ensure_utc(command.signal.timestamp) != ensure_utc(canonical.ts):
        msg = "Canonical replay command timestamp differs from its canonical signal"
        raise ValueError(msg)
    if command.signal.run_id != canonical.run_id:
        msg = "Canonical replay command signal run differs from its canonical signal"
        raise ValueError(msg)
    if command.signal.metadata.get("canonical_signal_id") != int(canonical.signal_id):
        msg = "Canonical replay command metadata lacks exact canonical signal lineage"
        raise ValueError(msg)
    if command.correlation_id != command.signal.signal_id or (
        command.causation_id != command.signal.signal_id
    ):
        msg = "Canonical replay command correlation lineage is invalid"
        raise ValueError(msg)
    if command.run_id != canonical.run_id or decision.run_id != canonical.run_id:
        msg = "Canonical replay command/decision run lineage differs from its canonical signal"
        raise ValueError(msg)


def _validate_replay_command_authority(
    *,
    canonical: CanonicalSignal,
    decision: ExecutionDecisionLog,
    command: ExecutionCommandEvent,
    user_id: str,
    broker_account_id: int,
) -> None:
    """Validate the frozen scoring policy and broker route without broadening it."""
    action = _normalized_execution_action(canonical.action)
    binding_id = _require_positive_int(
        decision.binding_id,
        field_name="decision binding_id",
    )
    try:
        decision_policy = ExecutionPolicySnapshot.model_validate(decision.binding_config_snapshot)
        decision_route = BrokerRouteSnapshot.model_validate(decision.broker_route_snapshot)
    except ValueError as exc:
        msg = "Canonical replay decision is missing valid policy/route snapshots"
        raise ValueError(msg) from exc
    if decision_policy != command.execution_policy:
        msg = "Canonical replay command policy differs from its decision snapshot"
        raise ValueError(msg)
    if decision_route != command.broker_route:
        msg = "Canonical replay command route differs from its decision snapshot"
        raise ValueError(msg)

    policy = command.execution_policy
    route = command.broker_route
    if (
        policy.user_id != user_id
        or policy.strategy_id != canonical.strategy_id
        or policy.binding_id != binding_id
    ):
        msg = "Canonical replay policy identity differs from its scored decision"
        raise ValueError(msg)
    if policy.config != command.user_strategy_config:
        msg = "Canonical replay command config differs from its frozen policy config"
        raise ValueError(msg)
    if (
        route.broker != "paper"
        or route.broker_environment != "paper"
        or not route.sandbox
        or route.live_enabled
        or route.broker_account_id != broker_account_id
    ):
        msg = "Canonical replay command is not an exact local-paper route"
        raise ValueError(msg)
    if route.allowed_brokers and "paper" not in route.allowed_brokers:
        msg = "Canonical replay route does not allow the paper broker"
        raise ValueError(msg)
    if policy.allowed_brokers and "paper" not in policy.allowed_brokers:
        msg = "Canonical replay policy does not allow the paper broker"
        raise ValueError(msg)
    is_exit = action == "close"
    if (
        not policy.autopilot
        or (is_exit and not policy.exits_enabled)
        or (not is_exit and not policy.entries_enabled)
    ):
        msg = "Canonical replay frozen policy does not authorize this signal action"
        raise ValueError(msg)
    if decision.execution_mode and (
        str(decision.execution_mode) != str(policy.execution_mode)
        or str(decision.execution_mode) != str(route.execution_mode)
    ):
        msg = "Canonical replay execution mode differs across decision policy and route"
        raise ValueError(msg)


def _load_replay_execution_contract(
    *,
    session_factory: Any,
    canonical: CanonicalSignal,
    asset_class: str,
    user_id: str,
    broker_account_id: int,
    expected_binding_id: int,
) -> ReplayExecutionContract:
    """Load and validate the exact scoring/outbox contract for a canonical signal."""
    decision = _load_scored_replay_decision(
        session_factory=session_factory,
        canonical=canonical,
        user_id=user_id,
        broker_account_id=broker_account_id,
        expected_binding_id=expected_binding_id,
    )
    outbox_event_id, command = _load_published_replay_command(
        session_factory=session_factory,
        canonical=canonical,
        decision=decision,
        user_id=user_id,
    )
    _validate_replay_command_lineage(
        session_factory=session_factory,
        canonical=canonical,
        decision=decision,
        command=command,
        asset_class=asset_class,
        user_id=user_id,
        broker_account_id=broker_account_id,
    )
    _validate_replay_command_authority(
        canonical=canonical,
        decision=decision,
        command=command,
        user_id=user_id,
        broker_account_id=broker_account_id,
    )
    return ReplayExecutionContract(
        decision_id=int(decision.decision_id),
        outbox_event_id=outbox_event_id,
        command=command,
    )


def _replay_execution_context(
    *,
    contract: ReplayExecutionContract,
    current_profile: dict[str, Any],
    broker_account_id: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Rebuild the normal command consumer context from the persisted event."""
    command = contract.command
    account_snapshot = dict(
        (current_profile.get("accounts") or {}).get(str(broker_account_id)) or {}
    )
    if not account_snapshot:
        msg = f"Canonical replay current account {broker_account_id} is unavailable"
        raise ValueError(msg)

    profile = dict(command.profile)
    profile["accounts"] = {str(broker_account_id): account_snapshot}
    profile["_broker_route_snapshot"] = command.broker_route.model_dump(mode="json")
    user_strategy_config = dict(command.user_strategy_config)
    user_strategy_config["_execution_policy_snapshot"] = command.execution_policy.model_dump(
        mode="json"
    )
    user_strategy_config["_causation_event_id"] = command.event_id
    score_context = command.score_context.model_dump(exclude_none=True)
    if command.execution_policy.execution_mode:
        score_context.setdefault(
            "recommended_mode",
            command.execution_policy.execution_mode,
        )
    return profile, user_strategy_config, score_context


def _signal_from_replay_contract(
    *,
    contract: ReplayExecutionContract,
    canonical: CanonicalSignal,
    replay_observation: ReplayPriceObservation,
) -> Signal:
    """Create the historical execution signal from its persisted command snapshot."""
    payload = contract.command.signal.model_dump()
    payload["action"] = normalize_signal_action(payload["action"])
    payload["timestamp"] = (
        payload["timestamp"]
        if payload["timestamp"].tzinfo is not None
        else payload["timestamp"].replace(tzinfo=UTC)
    )
    if payload.get("expires_at") is not None and payload["expires_at"].tzinfo is None:
        payload["expires_at"] = payload["expires_at"].replace(tzinfo=UTC)
    payload["entry_price"] = replay_observation.price
    payload["source"] = "paper"
    payload["raw_score"] = float(canonical.raw_score) if canonical.raw_score is not None else None
    payload["features"] = dict(canonical.features or {})
    payload["metadata"] = {
        **dict(payload.get("metadata") or {}),
        "canonical_signal_id": int(canonical.signal_id),
        "replay_decision_id": contract.decision_id,
        "replay_outbox_event_id": contract.outbox_event_id,
        "replay_fill_price": replay_observation.price,
        "price_source": replay_observation.source,
        "price_timeframe": replay_observation.timeframe,
        "source_price_id": replay_observation.price_id,
        "source_content_revision": replay_observation.content_revision,
        "source_price_ts": replay_observation.timestamp.isoformat(),
    }
    return Signal(**payload)


def _replay_broker_context(
    *,
    user_id: str,
    profile: dict[str, Any],
    user_strategy_config: dict[str, Any],
) -> tuple[int, str, str]:
    route_snapshot = dict(profile.get("_broker_route_snapshot") or {})
    route_account_id = route_snapshot.get("broker_account_id")
    profile_account_id = profile.get("broker_account_id")
    if route_account_id is None or profile_account_id is None:
        msg = "Canonical replay requires an explicit routed broker_account_id"
        raise ValueError(msg)
    if str(route_account_id) != str(profile_account_id):
        msg = "Canonical replay broker account differs between route and profile"
        raise ValueError(msg)
    if isinstance(route_account_id, bool):
        msg = "Canonical replay broker_account_id must be a positive integer"
        raise TypeError(msg)
    try:
        broker_account_id = int(route_account_id)
    except (TypeError, ValueError) as exc:
        msg = "Canonical replay broker_account_id must be a positive integer"
        raise ValueError(msg) from exc
    if broker_account_id <= 0:
        msg = "Canonical replay broker_account_id must be a positive integer"
        raise ValueError(msg)

    account_snapshot = dict((profile.get("accounts") or {}).get(str(broker_account_id)) or {})
    if not account_snapshot:
        msg = f"Canonical replay account {broker_account_id} is absent from the profile snapshot"
        raise ValueError(msg)
    if (
        str(account_snapshot.get("broker") or "").strip().lower() != "paper"
        or str(account_snapshot.get("environment") or "").strip().lower() != "paper"
        or account_snapshot.get("status") != "connected"
    ):
        msg = f"Canonical replay account {broker_account_id} is not a connected paper account"
        raise ValueError(msg)
    account_currency = str(account_snapshot.get("base_ccy") or "").strip().upper()
    if (
        not account_currency
        or len(account_currency) > _MAX_CURRENCY_CODE_LENGTH
        or not account_currency.isalnum()
    ):
        msg = f"Canonical replay account {broker_account_id} has invalid base currency"
        raise ValueError(msg)

    refs = {
        str(value).strip()
        for value in (
            user_strategy_config.get("credential_ref"),
            route_snapshot.get("credential_ref"),
            profile.get("credential_ref"),
        )
        if value is not None and str(value).strip()
    }
    if len(refs) != 1:
        msg = "Canonical replay requires one consistent explicit credential_ref"
        raise ValueError(msg)
    credential_ref = refs.pop()
    logger.debug(
        "Resolved replay broker context",
        user_id=user_id,
        broker_account_id=broker_account_id,
        account_currency=account_currency,
    )
    return broker_account_id, account_currency, credential_ref


async def _replay_account_summary(
    *,
    engine: ExecutionEngine,
    user_id: str,
    profile: dict[str, Any],
    user_strategy_config: dict[str, Any],
) -> tuple[float, float, float]:
    broker_account_id, account_currency, credential_ref = _replay_broker_context(
        user_id=user_id,
        profile=profile,
        user_strategy_config=user_strategy_config,
    )
    account_snapshot = dict((profile.get("accounts") or {}).get(str(broker_account_id)) or {})
    broker = await engine._get_broker(
        broker_type=BrokerType.PAPER,
        environment="paper",
        credential_ref=credential_ref,
        credentials=None,
        user_id=user_id,
        broker_account_id=broker_account_id,
        account_currency=account_currency,
        # Local paper cache identity is account-scoped and intentionally ignores
        # settlement; no conversion is performed while reading the summary.
        settlement_currency=account_currency,
        paper_initial_equity=(
            float(account_snapshot["paper_initial_equity"])
            if account_snapshot.get("paper_initial_equity") is not None
            else None
        ),
        paper_initial_cash=(
            float(account_snapshot["paper_initial_cash"])
            if account_snapshot.get("paper_initial_cash") is not None
            else None
        ),
    )
    if broker is None:
        msg = "Canonical replay paper broker is unavailable"
        raise RuntimeError(msg)
    account_info = await broker.get_account_info()
    ending_equity = float(account_info.equity)
    if account_info.unrealized_pnl is None or account_info.realized_pnl is None:
        msg = "Canonical replay paper account omitted P&L state"
        raise RuntimeError(msg)
    unrealized_pnl = float(account_info.unrealized_pnl)
    realized_pnl = float(account_info.realized_pnl)
    return ending_equity, realized_pnl, unrealized_pnl


def _validate_replay_request_context(
    *,
    profile: dict[str, Any],
    user_strategy_config: dict[str, Any],
    broker_account_id: int,
    starting_equity: float,
) -> int:
    account_snapshot = dict((profile.get("accounts") or {}).get(str(broker_account_id)) or {})
    expected_binding_id = _require_positive_int(
        user_strategy_config.get("binding_id") or account_snapshot.get("binding_id"),
        field_name="current binding_id",
    )
    configured_equity = account_snapshot.get("paper_initial_equity")
    if configured_equity is None or float(configured_equity) != float(starting_equity):
        msg = "Replay starting_equity must match the linked paper-account configuration"
        raise ValueError(msg)
    return expected_binding_id


async def replay_canonical_signals(
    *,
    engine: ExecutionEngine,
    session_factory: Any,
    user_id: str,
    strategy_id: str,
    profile: dict[str, Any],
    user_strategy_config: dict[str, Any],
    timeframe: str = "15m",
    source: str = "coinbase_live",
    require_minute_data: bool = True,
    symbols: Sequence[str] | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    max_signals: int | None = None,
    starting_equity: float,
) -> ReplaySummary:
    """Replay canonical signals through the existing paper execution path."""
    broker_account_id, _, _ = _replay_broker_context(
        user_id=user_id,
        profile=profile,
        user_strategy_config=user_strategy_config,
    )
    expected_binding_id = _validate_replay_request_context(
        profile=profile,
        user_strategy_config=user_strategy_config,
        broker_account_id=broker_account_id,
        starting_equity=starting_equity,
    )
    price_lookup = ReplayPriceLookup(
        session_factory=session_factory,
        timeframe=timeframe,
        source=source,
        source_timeframe="1m",
    )

    with session_factory() as session:
        instrument_ids: list[int] | None = None
        if symbols:
            instrument_ids = []
            for symbol in symbols:
                instrument = resolve_instrument(session, symbol)
                if instrument is None:
                    msg = f"Replay symbol does not resolve to a persisted instrument: {symbol!r}"
                    raise ValueError(msg)
                instrument_ids.append(int(instrument.instr_id))
        stmt = (
            select(
                CanonicalSignal,
                Instrument.canonical.label("symbol"),
                Instrument.asset_class.label("asset_class"),
            )
            .join(Instrument, CanonicalSignal.instr_id == Instrument.instr_id)
            .where(CanonicalSignal.strategy_id == strategy_id)
            .order_by(CanonicalSignal.ts.asc())
        )
        if start_date is not None:
            stmt = stmt.where(CanonicalSignal.ts >= start_date)
        if end_date is not None:
            stmt = stmt.where(CanonicalSignal.ts < end_date)
        if instrument_ids:
            stmt = stmt.where(CanonicalSignal.instr_id.in_(instrument_ids))
        if max_signals is not None:
            stmt = stmt.limit(max_signals)
        rows = session.execute(stmt).all()

    processed = 0
    skipped_missing_price = 0
    executed_results = 0
    blocked_results = 0
    failed_results = 0
    orders_filled = 0

    for row in rows:
        canonical = row[0]
        symbol = str(row.symbol)
        if not is_known_signal_action(canonical.action):
            msg = (
                "Replay cannot execute an unknown canonical action: "
                f"{canonical.action!r} for signal {canonical.signal_id}"
            )
            raise ValueError(msg)
        contract = _load_replay_execution_contract(
            session_factory=session_factory,
            canonical=canonical,
            asset_class=str(row.asset_class),
            user_id=user_id,
            broker_account_id=broker_account_id,
            expected_binding_id=expected_binding_id,
        )
        replay_observation = price_lookup.next_observation(
            instr_id=int(canonical.instr_id),
            signal_ts=canonical.ts,
        )
        if replay_observation is None:
            if require_minute_data:
                fill_ts = price_lookup.next_fill_timestamp(canonical.ts)
                msg = (
                    "Replay requires 1m price data for next 15m open fills. "
                    f"Missing {source} 1m price at {fill_ts.isoformat()} for {symbol}."
                )
                raise RuntimeError(msg)
            skipped_missing_price += 1
            logger.warning(
                "Replay skipped signal with missing next-bar price",
                canonical_signal_id=canonical.signal_id,
                strategy_id=strategy_id,
                symbol=symbol,
                signal_ts=canonical.ts.isoformat(),
                replay_timeframe=timeframe,
            )
            continue
        signal = _signal_from_replay_contract(
            contract=contract,
            canonical=canonical,
            replay_observation=replay_observation,
        )
        execution_profile, execution_config, score_context = _replay_execution_context(
            contract=contract,
            current_profile=profile,
            broker_account_id=broker_account_id,
        )

        result = await engine.handle_signal(
            user_id=user_id,
            profile=execution_profile,
            user_strategy_config=execution_config,
            signal=signal,
            score_context=score_context,
            allow_historical_replay=True,
        )
        processed += 1
        orders_filled += result.orders_filled
        if result.success and result.orders_filled > 0:
            executed_results += 1
        elif result.execution_mode == "blocked" or result.success:
            blocked_results += 1
        else:
            failed_results += 1

    ending_equity, realized_pnl, unrealized_pnl = await _replay_account_summary(
        engine=engine,
        user_id=user_id,
        profile=profile,
        user_strategy_config=user_strategy_config,
    )

    return ReplaySummary(
        user_id=user_id,
        strategy_id=strategy_id,
        signals_processed=processed,
        signals_skipped_missing_price=skipped_missing_price,
        executed_results=executed_results,
        blocked_results=blocked_results,
        failed_results=failed_results,
        orders_filled=orders_filled,
        ending_equity=ending_equity,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        starting_equity=starting_equity,
    )
