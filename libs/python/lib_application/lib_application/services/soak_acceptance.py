"""Programmatic go-live soak acceptance checks (Phase 5 promotion gate).

Encodes the ``docs/DEPLOYMENT.md`` *Promotion acceptance criteria* 14-day soak
signals as a single pass/fail verdict so the certification has real teeth: the
criteria are queried from the live database + environment, not eyeballed and
self-reported to the marker script. ``scripts/check_soak_acceptance.py`` is the
thin CLI wrapper; ``write_sandbox_certification_marker.py`` consumes the JSON
report so a ``passed`` marker cannot be written while any signal is red.

The checks mirror the documented criteria one-for-one:

* ``feedback_liveness``     — ``service_heartbeats`` for the feedback loop is recent.
* ``market_data_freshness`` — the newest ``prices`` row is recent.
* ``signal_activity``       — the newest ``canonical_signals`` row is recent (no stall).
* ``outbox_backlog``        — ``outbox_events`` backlog bounded + no dead letters.
* ``execution_fills``       — attributed canonical OMS fills have positive economics.
* ``duplicate_submissions`` — canonical venue trade identities are unique.
* ``positions_consistency`` — no negative-quantity (spot is long-only) position.
* ``nav_recorded``          — ``daily_nav`` is recent (the RiskGuard cap baseline).
* ``alert_sink``            — an ``ALERT_*`` sink is configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from lib_application.db.models import (
    CanonicalSignal,
    DailyNav,
    Execution,
    ExecutionLog,
    InstrumentPrice,
    LinkedBrokerAccount,
    Order,
    OrderIntent,
    OutboxEvent,
    Position,
    ServiceHeartbeat,
    Strategy,
)

# Defaults tuned for a Coinbase 1m-ingest crypto soak; override per environment.
DEFAULT_HEARTBEAT_MAX_AGE_S = 2 * 3600  # feedback loop runs as a periodic one-shot
DEFAULT_MARKET_DATA_MAX_AGE_S = 600  # 60s ingest poll -> 10min is generously stale
DEFAULT_SIGNAL_MAX_AGE_S = 1800  # a 1m strategy soak should emit well within 30min
DEFAULT_OUTBOX_PENDING_MAX = 100  # transient in-flight backlog ceiling
DEFAULT_MIN_EXECUTIONS = 1  # at least one real fill across the window
DEFAULT_NAV_MAX_AGE_DAYS = 2  # daily_nav is written per UTC day; 2 days of grace
DEFAULT_FEEDBACK_SERVICE = "feedback_loop_engine"

# Outbox rows still awaiting delivery (not yet published, not dead-lettered).
_OUTBOX_BACKLOG_STATUSES = ("pending", "failed", "in_progress")


@dataclass(frozen=True)
class SoakThresholds:
    """Tunable bounds for the soak acceptance checks."""

    heartbeat_max_age_s: int = DEFAULT_HEARTBEAT_MAX_AGE_S
    market_data_max_age_s: int = DEFAULT_MARKET_DATA_MAX_AGE_S
    signal_max_age_s: int = DEFAULT_SIGNAL_MAX_AGE_S
    outbox_pending_max: int = DEFAULT_OUTBOX_PENDING_MAX
    min_executions: int = DEFAULT_MIN_EXECUTIONS
    nav_max_age_days: int = DEFAULT_NAV_MAX_AGE_DAYS
    feedback_service: str = DEFAULT_FEEDBACK_SERVICE


@dataclass(frozen=True)
class SoakCheck:
    """One acceptance signal's verdict."""

    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class CanonicalFillEvidence:
    """Database evidence for canonical OMS fill certification."""

    positive_fills: int
    certified_fills: int
    provenance_failures: int
    incomplete_economics: int
    missing_trade_ids: int
    duplicate_broker_fill_keys: int


@dataclass(frozen=True)
class SoakReport:
    """Aggregate verdict — the soak passes only when every check passes."""

    checks: list[SoakCheck]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {"passed": self.passed, "checks": [c.to_dict() for c in self.checks]}


def _age_seconds(ts: datetime | None, now: datetime) -> float | None:
    """Seconds between ``ts`` and ``now``; naive timestamps are read as UTC.

    The ``prices`` table stores naive timestamps while ``service_heartbeats`` is
    tz-aware, so both paths flow through here for a consistent comparison.
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds()


def _naive_utc(ts: datetime) -> datetime:
    """Return UTC without tzinfo for the canonical ``executions.fill_ts`` column."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


def collect_canonical_fill_evidence(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> CanonicalFillEvidence:
    """Summarize positive fills and their immutable relational provenance.

    A certifiable fill is an ``executions`` row joined through its canonical
    ``orders`` and ``order_intents`` records. The order and intent must agree on
    the owned broker account, the account must agree on user/broker/environment,
    and the originating signal must agree on strategy and instrument. A
    non-empty ``trade_id`` makes ``(order_id, trade_id)`` the stable identity
    of one venue-reported fill. The actual fill timestamp, fee amount/currency,
    and venue are mandatory; order-status snapshots and fee-only adjustments
    are not certification evidence.
    """
    window_filters: list[ColumnElement[bool]] = []
    if since is not None:
        window_filters.append(Execution.fill_ts >= _naive_utc(since))
    if until is not None:
        window_filters.append(Execution.fill_ts <= _naive_utc(until))

    positive = and_(Execution.qty > 0, Execution.price > 0)
    has_trade_id = and_(
        Execution.trade_id.is_not(None),
        func.length(func.trim(Execution.trade_id)) > 0,
    )
    complete_economics = and_(
        Execution.fill_ts.is_not(None),
        Execution.fee_amount.is_not(None),
        Execution.fee_ccy.is_not(None),
        func.length(func.trim(Execution.fee_ccy)) > 0,
        Execution.venue.is_not(None),
        func.length(func.trim(Execution.venue)) > 0,
    )
    provenance_valid = and_(
        Order.order_id.is_not(None),
        OrderIntent.intent_id.is_not(None),
        LinkedBrokerAccount.account_id.is_not(None),
        Strategy.strategy_id.is_not(None),
        CanonicalSignal.signal_id.is_not(None),
        Order.account_id == OrderIntent.account_id,
        LinkedBrokerAccount.account_id == Order.account_id,
        LinkedBrokerAccount.user_id == OrderIntent.user_id,
        LinkedBrokerAccount.broker_id == Order.broker_id,
        LinkedBrokerAccount.environment == OrderIntent.broker_environment,
        Strategy.strategy_id == OrderIntent.strategy_id,
        CanonicalSignal.strategy_id == OrderIntent.strategy_id,
        CanonicalSignal.instr_id == Execution.instr_id,
    )
    joined = (
        select(Execution.exec_id)
        .select_from(Execution)
        .outerjoin(Order, Order.order_id == Execution.order_id)
        .outerjoin(OrderIntent, OrderIntent.intent_id == Order.intent_id)
        .outerjoin(
            LinkedBrokerAccount,
            LinkedBrokerAccount.account_id == Order.account_id,
        )
        .outerjoin(Strategy, Strategy.strategy_id == OrderIntent.strategy_id)
        .outerjoin(
            CanonicalSignal,
            CanonicalSignal.signal_id == OrderIntent.canonical_signal_id,
        )
    )

    positive_fills = int(
        session.execute(
            select(func.count()).select_from(joined.where(positive, *window_filters).subquery())
        ).scalar_one()
    )
    attributed_fills = int(
        session.execute(
            select(func.count()).select_from(
                joined.where(positive, provenance_valid, *window_filters).subquery()
            )
        ).scalar_one()
    )
    certified_fills = int(
        session.execute(
            select(func.count()).select_from(
                joined.where(
                    positive,
                    provenance_valid,
                    has_trade_id,
                    complete_economics,
                    *window_filters,
                ).subquery()
            )
        ).scalar_one()
    )
    missing_trade_ids = int(
        session.execute(
            select(func.count()).select_from(
                joined.where(
                    positive,
                    or_(
                        Execution.trade_id.is_(None),
                        func.length(func.trim(Execution.trade_id)) == 0,
                    ),
                    *window_filters,
                ).subquery()
            )
        ).scalar_one()
    )
    incomplete_economics = int(
        session.execute(
            select(func.count()).select_from(
                joined.where(
                    positive,
                    or_(
                        Execution.fill_ts.is_(None),
                        Execution.fee_amount.is_(None),
                        Execution.fee_ccy.is_(None),
                        func.length(func.trim(Execution.fee_ccy)) == 0,
                        Execution.venue.is_(None),
                        func.length(func.trim(Execution.venue)) == 0,
                    ),
                    *window_filters,
                ).subquery()
            )
        ).scalar_one()
    )
    duplicate_keys = (
        select(Execution.order_id, Execution.trade_id)
        .where(positive, has_trade_id, *window_filters)
        .group_by(Execution.order_id, Execution.trade_id)
        .having(func.count() > 1)
        .subquery()
    )
    duplicate_broker_fill_keys = int(
        session.execute(select(func.count()).select_from(duplicate_keys)).scalar_one()
    )
    return CanonicalFillEvidence(
        positive_fills=positive_fills,
        certified_fills=certified_fills,
        provenance_failures=positive_fills - attributed_fills,
        incomplete_economics=incomplete_economics,
        missing_trade_ids=missing_trade_ids,
        duplicate_broker_fill_keys=duplicate_broker_fill_keys,
    )


def _check_feedback_liveness(session: Session, now: datetime, th: SoakThresholds) -> SoakCheck:
    row = session.get(ServiceHeartbeat, th.feedback_service)
    age = _age_seconds(row.last_success_at if row else None, now)
    if age is None:
        return SoakCheck(
            "feedback_liveness", False, f"no heartbeat for service '{th.feedback_service}'"
        )
    return SoakCheck(
        "feedback_liveness",
        age <= th.heartbeat_max_age_s,
        f"feedback heartbeat age {age:.0f}s (max {th.heartbeat_max_age_s}s)",
    )


def _check_market_data_freshness(session: Session, now: datetime, th: SoakThresholds) -> SoakCheck:
    latest = session.execute(select(func.max(InstrumentPrice.ts))).scalar_one_or_none()
    age = _age_seconds(latest, now)
    if age is None:
        return SoakCheck("market_data_freshness", False, "no rows in prices")
    return SoakCheck(
        "market_data_freshness",
        age <= th.market_data_max_age_s,
        f"latest price age {age:.0f}s (max {th.market_data_max_age_s}s)",
    )


def _check_signal_activity(session: Session, now: datetime, th: SoakThresholds) -> SoakCheck:
    # Stall detection: market data can be fresh while signal emission has stopped
    # (a crashed strategy worker). The newest canonical_signals row must be recent.
    latest = session.execute(select(func.max(CanonicalSignal.ts))).scalar_one_or_none()
    age = _age_seconds(latest, now)
    if age is None:
        return SoakCheck("signal_activity", False, "no canonical_signals emitted")
    return SoakCheck(
        "signal_activity",
        age <= th.signal_max_age_s,
        f"latest signal age {age:.0f}s (max {th.signal_max_age_s}s)",
    )


def _check_positions_consistency(session: Session) -> SoakCheck:
    # Spot is long-only: a negative-quantity position is state corruption (a bad
    # fill/close or a leaked short). Relax this bound once non-spot modes ship.
    neg = int(
        session.execute(
            select(func.count()).select_from(Position).where(Position.qty < 0)
        ).scalar_one()
    )
    return SoakCheck(
        "positions_consistency",
        neg == 0,
        f"{neg} positions with negative qty (spot is long-only; must be 0)",
    )


def _check_outbox_backlog(session: Session, th: SoakThresholds) -> SoakCheck:
    rows = session.execute(
        select(OutboxEvent.status, func.count()).group_by(OutboxEvent.status)
    ).all()
    counts = {status: int(n) for status, n in rows}
    backlog = sum(counts.get(s, 0) for s in _OUTBOX_BACKLOG_STATUSES)
    dead = counts.get("dead_letter", 0)
    return SoakCheck(
        "outbox_backlog",
        backlog <= th.outbox_pending_max and dead == 0,
        f"backlog {backlog} (max {th.outbox_pending_max}), dead_letter {dead} (must be 0)",
    )


def _check_execution_fills(session: Session, th: SoakThresholds) -> SoakCheck:
    evidence = collect_canonical_fill_evidence(session)
    rows = session.execute(
        select(ExecutionLog.status, func.count()).group_by(ExecutionLog.status)
    ).all()
    counts = {status: int(n) for status, n in rows}
    return SoakCheck(
        "execution_fills",
        evidence.certified_fills >= th.min_executions
        and evidence.provenance_failures == 0
        and evidence.incomplete_economics == 0
        and evidence.missing_trade_ids == 0,
        (
            f"{evidence.certified_fills}/{evidence.positive_fills} positive canonical fills "
            "certified "
            f"(min {th.min_executions}); {evidence.provenance_failures} provenance failures, "
            f"{evidence.incomplete_economics} incomplete fill economics, "
            f"{evidence.missing_trade_ids} missing venue trade IDs; diagnostics: "
            f"{counts.get('executed', 0)} execution_logs executed, "
            f"{counts.get('no_op', 0)} no_op"
        ),
    )


def _check_duplicate_submissions(session: Session) -> SoakCheck:
    evidence = collect_canonical_fill_evidence(session)
    return SoakCheck(
        "duplicate_submissions",
        evidence.duplicate_broker_fill_keys == 0 and evidence.missing_trade_ids == 0,
        (
            f"{evidence.duplicate_broker_fill_keys} duplicate canonical "
            "(order_id, trade_id) venue trade keys, "
            f"{evidence.missing_trade_ids} positive fills without trade_id (must both be 0)"
        ),
    )


def _check_nav_recorded(session: Session, now: datetime, th: SoakThresholds) -> SoakCheck:
    # H9: the RiskGuard daily-loss / drawdown caps re-baseline off daily_nav. If
    # NAV is never persisted, the caps are silently inert — so a soak that has run
    # must show a recent, owned daily_nav row for every account with a canonical
    # fill. A row for one account must never certify another account owned by the
    # same user.
    filled_accounts = session.execute(
        select(LinkedBrokerAccount.account_id, LinkedBrokerAccount.user_id)
        .select_from(LinkedBrokerAccount)
        .join(Order, Order.account_id == LinkedBrokerAccount.account_id)
        .join(OrderIntent, OrderIntent.intent_id == Order.intent_id)
        .join(Execution, Execution.order_id == Order.order_id)
        .where(
            Execution.qty > 0,
            OrderIntent.account_id == LinkedBrokerAccount.account_id,
            OrderIntent.user_id == LinkedBrokerAccount.user_id,
        )
        .distinct()
    ).all()
    if not filled_accounts:
        return SoakCheck(
            "nav_recorded",
            False,
            "no account-attributed fills — no account NAV baseline can be certified",
        )

    missing_accounts: list[int] = []
    stale_accounts: list[tuple[int, int]] = []
    for account_id, user_id in filled_accounts:
        latest = session.execute(
            select(func.max(DailyNav.date)).where(
                DailyNav.account_id == int(account_id),
                DailyNav.user_id == str(user_id),
            )
        ).scalar_one_or_none()
        if latest is None:
            missing_accounts.append(int(account_id))
            continue
        age_days = (now.date() - latest).days
        if age_days > th.nav_max_age_days:
            stale_accounts.append((int(account_id), age_days))

    passed = not missing_accounts and not stale_accounts
    stale_detail = ", ".join(f"{account_id}:{age_days}d" for account_id, age_days in stale_accounts)
    return SoakCheck(
        "nav_recorded",
        passed,
        (
            f"{len(filled_accounts)} filled account(s) checked; "
            f"missing={missing_accounts or 'none'}; "
            f"stale={stale_detail or 'none'} "
            f"(max {th.nav_max_age_days}d)"
        ),
    )


def _check_alert_sink(alerts_deliverable: bool) -> SoakCheck:
    return SoakCheck(
        "alert_sink",
        alerts_deliverable,
        "alerts are deliverable (sink configured + alerting enabled)"
        if alerts_deliverable
        else "alerts will not reach a human (no ALERT_* sink, or alerting disabled)",
    )


def check_soak_acceptance(
    session: Session,
    *,
    now: datetime,
    alerts_deliverable: bool,
    thresholds: SoakThresholds | None = None,
) -> SoakReport:
    """Run every soak acceptance check and return the aggregate verdict.

    ``alerts_deliverable`` is computed by the caller from the environment — a sink
    is configured (``lib_common.alerting.build_sinks_from_env``) AND alerting is
    enabled (``EXECUTION_ALERTS_ENABLED``); a configured-but-disabled sink delivers
    nothing. Passing it in keeps this a pure function of the session + parameters
    (and thus directly testable).
    """
    th = thresholds or SoakThresholds()
    return SoakReport(
        checks=[
            _check_feedback_liveness(session, now, th),
            _check_market_data_freshness(session, now, th),
            _check_signal_activity(session, now, th),
            _check_outbox_backlog(session, th),
            _check_execution_fills(session, th),
            _check_duplicate_submissions(session),
            _check_positions_consistency(session),
            _check_nav_recorded(session, now, th),
            _check_alert_sink(alerts_deliverable),
        ]
    )
