"""Pipeline soak reconciliation report (e2e-verification utility).

Companion to ``soak_acceptance`` (the programmatic go-live gate): this builds
a per-stage activity + reconciliation report for a local 24h paper soak so a
human (or the CLI) can confirm signals flowed end to end and the DB is internally
consistent — signals → scores → decisions → executions → outbox → positions →
P&L → feedback — and flag the invariant violations that matter (dup scores, dup
venue trade identities, dead-lettered events, no canonical fills, missing
executions).

Robustness: every stage runs in isolation, so a single failing query (e.g. a
column missing on a schema behind head) degrades that one stage to UNKNOWN with a
clear error instead of aborting the whole report. A schema preflight reports
whether the DB is at head.

Pure function of (session, window) so it is directly unit-testable;
``scripts/verify_pipeline_soak.py`` is the thin CLI wrapper.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, func, inspect, select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    AssetScore,
    CanonicalSignal,
    ExecutionDecisionLog,
    ExecutionLog,
    ExecutionMetric,
    OutboxEvent,
    PendingOrder,
    ServiceHeartbeat,
    SignalPerformance,
)
from lib_application.services.soak_acceptance import collect_canonical_fill_evidence

DEFAULT_HEARTBEAT_MAX_AGE_S = 2 * 3600
_ACTIONABLE_ACTIONS = ("long", "short")
_ORPHAN_SAMPLE_LIMIT = 20
# Sentinel columns that prove the DB is migrated to head (alembic, not create_all).
_SCHEMA_SENTINELS: tuple[tuple[str, str], ...] = (
    ("asset_scores", "external_signal_id"),
    ("execution_decision_logs", "idempotency_key"),
    ("pending_orders", "idempotency_key"),
    ("order_intents", "canonical_signal_id"),
    ("executions", "trade_id"),
)


@dataclass
class SoakSection:
    """One report section: a named breakdown + optional invariant verdict."""

    name: str
    rows: Mapping[str, int | float | str]
    ok: bool | None = None  # None = informational; True/False = invariant verdict
    detail: str = ""
    error: str | None = None  # set when the stage failed to run (UNKNOWN, degraded)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "rows": self.rows,
            "ok": self.ok,
            "detail": self.detail,
            "error": self.error,
        }


@dataclass
class SoakReport:
    """Full reconciliation report. ``passed`` = all invariant sections held."""

    since: datetime
    until: datetime
    sections: list[SoakSection] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(s.ok for s in self.sections if s.ok is not None)

    @property
    def degraded(self) -> bool:
        """True if any stage failed to run (the report is incomplete)."""
        return any(s.error is not None for s in self.sections)

    def to_dict(self) -> dict[str, object]:
        return {
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "passed": self.passed,
            "degraded": self.degraded,
            "sections": [s.to_dict() for s in self.sections],
        }


def _naive(ts: datetime) -> datetime:
    """canonical_signals/asset_scores.ts are tz-naive; compare windows in kind."""
    return ts.replace(tzinfo=None)


def _coerce_utc(ts: datetime, ref: datetime) -> datetime:
    """Align a possibly-naive heartbeat ts to the reference's tz for subtraction."""
    if ts.tzinfo is None and ref.tzinfo is not None:
        return ts.replace(tzinfo=ref.tzinfo)
    if ts.tzinfo is not None and ref.tzinfo is None:
        return ts.replace(tzinfo=None)
    return ts


def _counts(session: Session, column: Any, *filters: Any) -> dict[str, int]:
    rows = session.execute(select(column, func.count()).where(*filters).group_by(column)).all()
    return {str(k): int(n) for k, n in rows}


def _dup_keys(session: Session, model: Any, *filters: Any) -> int:
    subq = (
        select(model.idempotency_key)
        .where(model.idempotency_key.is_not(None), *filters)
        .group_by(model.idempotency_key)
        .having(func.count() > 1)
        .subquery()
    )
    return int(session.execute(select(func.count()).select_from(subq)).scalar_one())


def _run_stage(
    report: SoakReport, name: str, builder: Callable[[], SoakSection | list[SoakSection]]
) -> None:
    """Run one stage in isolation: a failure degrades it to UNKNOWN, not a crash.

    A stage that raises (e.g. a column missing on a schema behind head) appends an
    informational section with ``error`` set, so the report stays complete and the
    caller can see ``report.degraded`` rather than getting a traceback.
    """
    try:
        section = builder()
    except Exception as exc:
        report.sections.append(
            SoakSection(
                name,
                {},
                ok=None,
                detail="stage failed to run — schema behind head? run `alembic upgrade head`",
                error=f"{type(exc).__name__}: {exc}",
            )
        )
    else:
        if isinstance(section, list):
            report.sections.extend(section)
        else:
            report.sections.append(section)


def build_soak_report(
    session: Session,
    *,
    now: datetime,
    since: datetime,
    require_feedback: bool = True,
    heartbeat_max_age_s: int = DEFAULT_HEARTBEAT_MAX_AGE_S,
) -> SoakReport:
    """Build the per-stage soak reconciliation report for the window [since, now].

    Args:
        require_feedback: when False, a missing/stale feedback heartbeat is
            informational (UNKNOWN) instead of a hard failure — for soaks run
            without the feedback-loop-engine service.
    """
    report = SoakReport(since=since, until=now)

    # Guard: an empty/negative window is a usage error, not a passing soak.
    if now <= since:
        report.sections.append(
            SoakSection(
                "window",
                {"since": since.isoformat(), "until": now.isoformat()},
                ok=False,
                detail="empty/negative window — nothing to reconcile",
            )
        )
        return report

    naive_since = _naive(since)

    _run_stage(report, "schema", lambda: _schema_section(session))
    _run_stage(report, "signals", lambda: _signals_section(session, naive_since))
    _run_stage(report, "scoring", lambda: _scoring_section(session, naive_since))
    _run_stage(report, "decisions+executions", lambda: _execution_sections(session, since, now))
    _run_stage(report, "outbox", lambda: _outbox_section(session, since))
    _run_stage(report, "dedup", lambda: _dedup_section(session, since, now))
    _run_stage(report, "reconciliation", lambda: _reconciliation_section(session, naive_since))
    _run_stage(report, "pnl", lambda: _pnl_sections(session, since))
    _run_stage(report, "feedback", lambda: _feedback_section(session, naive_since))
    _run_stage(
        report,
        "feedback_liveness",
        lambda: _feedback_liveness_section(session, now, require_feedback, heartbeat_max_age_s),
    )
    return report


def _schema_section(session: Session) -> SoakSection:
    """Report whether the DB is migrated to head (sentinel columns present)."""
    insp = inspect(session.get_bind())
    table_names = set(insp.get_table_names())
    rows: dict[str, int | float | str] = {}
    missing: list[str] = []
    for table, column in _SCHEMA_SENTINELS:
        present = table in table_names and any(
            candidate["name"] == column for candidate in insp.get_columns(table)
        )
        label = f"{table}.{column}"
        rows[label] = "present" if present else "MISSING"
        if not present:
            missing.append(label)
    detail = (
        "schema at head"
        if not missing
        else "schema BEHIND head — run `alembic upgrade head` before trusting the soak"
    )
    return SoakSection("schema", rows, ok=not missing, detail=detail)


def _signals_section(session: Session, naive_since: datetime) -> SoakSection:
    rows = session.execute(
        select(CanonicalSignal.strategy_id, CanonicalSignal.action, func.count())
        .where(CanonicalSignal.ts >= naive_since)
        .group_by(CanonicalSignal.strategy_id, CanonicalSignal.action)
    ).all()
    signals = {f"{sid}/{act}": int(n) for sid, act, n in rows}
    return SoakSection(
        "signals",
        signals,
        ok=sum(signals.values()) > 0,
        detail="canonical_signals by strategy/action; expect entries + exits",
    )


def _scoring_section(session: Session, naive_since: datetime) -> SoakSection:
    score_count = int(
        session.execute(
            select(func.count()).select_from(AssetScore).where(AssetScore.ts >= naive_since)
        ).scalar_one()
    )
    dup_subq = (
        select(AssetScore.external_signal_id)
        .where(AssetScore.external_signal_id.is_not(None))
        .group_by(AssetScore.external_signal_id)
        .having(func.count() > 1)
        .subquery()
    )
    dup_scores = int(session.execute(select(func.count()).select_from(dup_subq)).scalar_one())
    return SoakSection(
        "scoring",
        {"asset_scores": score_count, "duplicate_external_signal_id": dup_scores},
        ok=dup_scores == 0,
        detail="SC-6: a re-delivered signal must UPDATE its score row, not duplicate",
    )


def _execution_sections(
    session: Session,
    since: datetime,
    until: datetime,
) -> list[SoakSection]:
    decisions = _counts(session, ExecutionDecisionLog.status, ExecutionDecisionLog.ts >= since)
    exec_status = _counts(session, ExecutionLog.status, ExecutionLog.created_at >= since)
    evidence = collect_canonical_fill_evidence(session, since=since, until=until)
    return [
        SoakSection("decisions", decisions, detail="execution_decision_logs.status"),
        SoakSection(
            "execution_logs",
            exec_status,
            detail="diagnostic outcomes only; canonical executions certify fills",
        ),
        SoakSection(
            "executions",
            {
                "positive_fills": evidence.positive_fills,
                "certified_fills": evidence.certified_fills,
                "provenance_failures": evidence.provenance_failures,
                "incomplete_economics": evidence.incomplete_economics,
                "missing_trade_ids": evidence.missing_trade_ids,
            },
            ok=(
                evidence.certified_fills > 0
                and evidence.provenance_failures == 0
                and evidence.incomplete_economics == 0
                and evidence.missing_trade_ids == 0
            ),
            detail=(
                "positive canonical OMS fills with matching account/signal/strategy "
                "lineage, complete economics, and stable venue trade identity"
            ),
        ),
    ]


def _outbox_section(session: Session, since: datetime) -> SoakSection:
    # Window-scoped: a recurring soak verdict must not be poisoned by stale
    # historical dead-letters from a previous run.
    outbox = _counts(session, OutboxEvent.status, OutboxEvent.created_at >= since)
    dead = outbox.get("dead_letter", 0)
    return SoakSection(
        "outbox",
        outbox,
        ok=dead == 0,
        detail="dead_letter MUST be 0 in-window; pending should drain",
    )


def _dedup_section(session: Session, since: datetime, until: datetime) -> SoakSection:
    # Decision and pending-order keys remain diagnostics. The certification
    # invariant is the canonical venue trade identity for this window.
    dup_decisions = _dup_keys(session, ExecutionDecisionLog, ExecutionDecisionLog.ts >= since)
    dup_orders = _dup_keys(session, PendingOrder, PendingOrder.created_at >= since)
    evidence = collect_canonical_fill_evidence(session, since=since, until=until)
    return SoakSection(
        "dedup",
        {
            "duplicate_broker_fill_keys": evidence.duplicate_broker_fill_keys,
            "positive_fills_without_trade_id": evidence.missing_trade_ids,
            "diagnostic_duplicate_decision_keys": dup_decisions,
            "diagnostic_duplicate_pending_order_keys": dup_orders,
        },
        ok=evidence.duplicate_broker_fill_keys == 0 and evidence.missing_trade_ids == 0,
        detail=(
            "canonical (order_id, trade_id) venue trade keys unique in-window; "
            "dispatch-state keys are diagnostics"
        ),
    )


def _reconciliation_section(session: Session, naive_since: datetime) -> SoakSection:
    """Actionable (long/short) signals that were NEVER handled.

    The core "did every actionable signal get acted on?" check — a per-signal flow
    property that per-stage counts cannot see. A signal is an orphan only if it has
    NEITHER an execution_logs row (executed / no_op / blocked) NOR an
    execution_decision_logs row (a rejected/skipped policy decision IS a terminal
    "handled" outcome, not a missing execution). flat/hold are excluded.
    """
    # The decision stores the domain Signal UUID, while ``run_id`` is the
    # canonical cross-container link to the persisted signal.
    has_decision = exists().where(
        and_(
            CanonicalSignal.run_id.is_not(None),
            ExecutionDecisionLog.run_id == CanonicalSignal.run_id,
        )
    )
    orphan_filter = (
        CanonicalSignal.ts >= naive_since,
        CanonicalSignal.action.in_(_ACTIONABLE_ACTIONS),
        ExecutionLog.log_id.is_(None),
        ~has_decision,
    )
    orphan_total = int(
        session.execute(
            select(func.count())
            .select_from(CanonicalSignal)
            .outerjoin(ExecutionLog, ExecutionLog.canonical_signal_id == CanonicalSignal.signal_id)
            .where(*orphan_filter)
        ).scalar_one()
    )
    sample = (
        session.execute(
            select(CanonicalSignal.signal_id)
            .outerjoin(ExecutionLog, ExecutionLog.canonical_signal_id == CanonicalSignal.signal_id)
            .where(*orphan_filter)
            .limit(_ORPHAN_SAMPLE_LIMIT)
        )
        .scalars()
        .all()
    )
    rows: dict[str, int | float | str] = {"orphan_actionable_signals": orphan_total}
    if sample:
        rows["orphan_signal_ids_sample"] = ",".join(str(s) for s in sample)
    return SoakSection(
        "reconciliation",
        rows,
        ok=orphan_total == 0,
        detail="actionable signals with NO execution AND NO decision (never handled)",
    )


def _pnl_sections(session: Session, since: datetime) -> list[SoakSection]:
    fills = _counts(session, PendingOrder.status, PendingOrder.created_at >= since)

    # realized_pnl is a per-partition CUMULATIVE running total (one snapshot row
    # per execution), so SUMMing snapshots N-counts it. Take the LATEST row per
    # (user, strategy, symbol, mode) partition, then sum per (user, strategy, mode).
    # Exclude 'blocked' rows (policy-blocked executions that never filled but still
    # carry the partition's cumulative P&L).
    rn = (
        func.row_number()
        .over(
            partition_by=[
                ExecutionMetric.user_id,
                ExecutionMetric.strategy_id,
                ExecutionMetric.symbol,
                ExecutionMetric.execution_mode,
            ],
            order_by=ExecutionMetric.created_at.desc(),
        )
        .label("rn")
    )
    latest = (
        select(
            ExecutionMetric.user_id.label("user_id"),
            ExecutionMetric.strategy_id.label("strategy_id"),
            ExecutionMetric.execution_mode.label("execution_mode"),
            ExecutionMetric.realized_pnl.label("realized_pnl"),
            rn,
        )
        .where(ExecutionMetric.created_at >= since, ExecutionMetric.execution_mode != "blocked")
        .subquery()
    )
    realized = session.execute(
        select(
            latest.c.user_id,
            latest.c.strategy_id,
            latest.c.execution_mode,
            func.sum(latest.c.realized_pnl),
        )
        .where(latest.c.rn == 1)
        .group_by(latest.c.user_id, latest.c.strategy_id, latest.c.execution_mode)
    ).all()
    # orders_filled IS per-execution (not cumulative) — sum it over the window.
    filled = session.execute(
        select(
            ExecutionMetric.user_id,
            ExecutionMetric.strategy_id,
            ExecutionMetric.execution_mode,
            func.sum(ExecutionMetric.orders_filled),
        )
        .where(ExecutionMetric.created_at >= since, ExecutionMetric.execution_mode != "blocked")
        .group_by(
            ExecutionMetric.user_id, ExecutionMetric.strategy_id, ExecutionMetric.execution_mode
        )
    ).all()
    pnl_rows: dict[str, int | float | str] = {}
    for user, strat, mode, realized_total in realized:
        pnl_rows[f"{user}/{strat}/{mode}/realized_pnl"] = float(realized_total or 0)
    for user, strat, mode, total_filled in filled:
        pnl_rows[f"{user}/{strat}/{mode}/orders_filled"] = int(total_filled or 0)
    return [
        SoakSection("pending_orders", fills, detail="market BUY/SELL -> filled"),
        SoakSection(
            "realized_pnl",
            pnl_rows,
            detail="latest cumulative realized P&L per (user,strategy,mode); blocked excluded",
        ),
    ]


def _feedback_section(session: Session, naive_since: datetime) -> SoakSection:
    # signal_performance.signal_ts is tz-naive (like canonical_signals/asset_scores).
    rows = session.execute(
        select(SignalPerformance.evaluation_horizon, SignalPerformance.is_correct, func.count())
        .where(
            SignalPerformance.signal_ts >= naive_since, SignalPerformance.is_correct.is_not(None)
        )
        .group_by(SignalPerformance.evaluation_horizon, SignalPerformance.is_correct)
    ).all()
    feedback = {f"{hz}/correct={ic}": int(n) for hz, ic, n in rows}
    return SoakSection(
        "feedback",
        feedback,
        detail="signal_performance evaluated (forward-return directional accuracy, by horizon)",
    )


def _feedback_liveness_section(
    session: Session, now: datetime, require_feedback: bool, heartbeat_max_age_s: int
) -> SoakSection:
    hb = session.get(ServiceHeartbeat, "feedback_loop_engine")
    hb_age = None if hb is None else (now - _coerce_utc(hb.last_success_at, now)).total_seconds()
    fresh = hb_age is not None and hb_age <= heartbeat_max_age_s
    # When feedback is intentionally not running, a missing/stale heartbeat is
    # informational (UNKNOWN) rather than a hard soak failure.
    ok: bool | None = fresh if require_feedback else (True if fresh else None)
    detail = (
        "service_heartbeats[feedback_loop_engine] must be fresh"
        if require_feedback
        else "feedback heartbeat (optional: require_feedback=False)"
    )
    return SoakSection(
        "feedback_liveness",
        {"heartbeat_age_s": "missing" if hb_age is None else round(hb_age)},
        ok=ok,
        detail=detail,
    )


def render_markdown(report: SoakReport) -> str:
    """Render the report as a compact markdown digest for the run findings."""
    overall = "PASS" if report.passed else "FAIL"
    if report.degraded:
        overall += " (DEGRADED — some stages could not run)"
    lines = [
        f"# Pipeline soak report — {report.since.isoformat()} → {report.until.isoformat()}",
        f"**Overall invariants: {overall}**",
        "",
    ]
    for section in report.sections:
        if section.error is not None:
            verdict = "  ⚠ UNKNOWN"
        elif section.ok is None:
            verdict = ""
        else:
            verdict = "  ✓" if section.ok else "  ✗ FAIL"
        lines.append(f"## {section.name}{verdict}")
        if section.detail:
            lines.append(f"_{section.detail}_")
        if section.error:
            lines.append(f"> error: {section.error}")
        for key, value in section.rows.items():
            lines.append(f"- {key}: {value}")
        lines.append("")
    return "\n".join(lines)
