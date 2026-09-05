"""Populate account-scoped mode rankings from realised execution economics.

Only filled execution-metric snapshots carrying exact FIFO close-to-entry
lineage are eligible. Signal evaluation supplies the opening signal's instrument
and horizon identity; it does not supply the return. The return is each matched
lot's net realised P&L divided by its entry capital, preserving broker fees,
observed FX conversion, and mode-specific payoff instead of relabelling a
signal-direction price move as execution P&L.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from lib_application.db.models import (
    ExecutionMetric,
    ModePerformance,
    SignalPerformance,
)
from lib_common.logging import get_logger
from lib_strategy.scoring.mode_horizon import normalize_mode_horizon

logger = get_logger(__name__)

#: Above this magnitude a Sharpe/Sortino is numerical noise from degenerate
#: dispersion, not a statistic (and it overflows the Numeric(10,4) column).
_RATIO_DEFINED_BOUND = 1_000.0

_MIN_FOR_DEVIATION = 2


class ModePerformanceIntegrityError(RuntimeError):
    """An execution metric contains malformed realised-trade attribution."""


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < _MIN_FOR_DEVIATION:
        return 0.0
    m = _mean(xs)
    return float((sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5)


def _bounded_ratio(mean_value: float, dispersion: float) -> float | None:
    """Return mean/dispersion, or None when the dispersion is degenerate."""
    if dispersion <= 0.0:
        return None
    ratio = mean_value / dispersion
    return ratio if abs(ratio) < _RATIO_DEFINED_BOUND else None


def _dec(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"))


@dataclass(frozen=True)
class _Metrics:
    total_return: float
    sharpe: float | None
    sortino: float | None
    win_rate: float
    avg_win: float | None
    avg_loss: float | None
    max_drawdown: float
    sample_size: int


@dataclass(frozen=True)
class _RealizedContribution:
    entry_exec_id: int
    exit_exec_id: int
    entry_canonical_signal_id: int
    exit_canonical_signal_id: int
    realized_pnl: Decimal
    deployed_capital: Decimal
    account_currency: str


def _positive_int(value: Any, *, field: str, metric_id: str) -> int:
    msg = f"Execution metric {metric_id} has invalid {field}"
    if isinstance(value, bool):
        raise ModePerformanceIntegrityError(msg)
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ModePerformanceIntegrityError(msg) from exc
    if normalized <= 0:
        raise ModePerformanceIntegrityError(msg)
    return normalized


def _decimal(value: Any, *, field: str, metric_id: str) -> Decimal:
    msg = f"Execution metric {metric_id} has invalid {field}"
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ModePerformanceIntegrityError(msg) from exc
    if not normalized.is_finite():
        raise ModePerformanceIntegrityError(msg)
    return normalized


def _realized_contributions(metric: ExecutionMetric) -> list[_RealizedContribution]:
    metric_id = str(metric.metric_id)
    metadata = metric.metadata_json or {}
    if not isinstance(metadata, Mapping):
        msg = f"Execution metric {metric_id} metadata is not an object"
        raise ModePerformanceIntegrityError(msg)
    raw = metadata.get("realized_pnl_contributions")
    if raw is None:
        # Metrics written before exact FIFO lineage was introduced are not safe
        # mode-ranking inputs. They remain valid monitoring snapshots.
        return []
    if not isinstance(raw, list):
        msg = f"Execution metric {metric_id} realized_pnl_contributions is not a list"
        raise ModePerformanceIntegrityError(msg)

    contributions: list[_RealizedContribution] = []
    for item in raw:
        if not isinstance(item, Mapping):
            msg = f"Execution metric {metric_id} has a malformed realized contribution"
            raise ModePerformanceIntegrityError(msg)
        realized_pnl = _decimal(
            item.get("realized_pnl"),
            field="realized_pnl",
            metric_id=metric_id,
        )
        deployed_capital = _decimal(
            item.get("deployed_capital"),
            field="deployed_capital",
            metric_id=metric_id,
        )
        if deployed_capital <= 0:
            msg = f"Execution metric {metric_id} has non-positive deployed_capital"
            raise ModePerformanceIntegrityError(msg)
        account_currency = str(item.get("account_currency") or "").strip().upper()
        if not account_currency or not account_currency.isalnum():
            msg = f"Execution metric {metric_id} has invalid account_currency"
            raise ModePerformanceIntegrityError(msg)
        contributions.append(
            _RealizedContribution(
                entry_exec_id=_positive_int(
                    item.get("entry_exec_id"),
                    field="entry_exec_id",
                    metric_id=metric_id,
                ),
                exit_exec_id=_positive_int(
                    item.get("exit_exec_id"),
                    field="exit_exec_id",
                    metric_id=metric_id,
                ),
                entry_canonical_signal_id=_positive_int(
                    item.get("entry_canonical_signal_id"),
                    field="entry_canonical_signal_id",
                    metric_id=metric_id,
                ),
                exit_canonical_signal_id=_positive_int(
                    item.get("exit_canonical_signal_id"),
                    field="exit_canonical_signal_id",
                    metric_id=metric_id,
                ),
                realized_pnl=realized_pnl,
                deployed_capital=deployed_capital,
                account_currency=account_currency,
            )
        )
    return contributions


def _metrics(returns: list[float]) -> _Metrics:
    """Per-trade-return metrics for one (instrument, mode, horizon) cohort.

    Sharpe/Sortino are un-annualised (mean/deviation) — a constant factor across
    modes, so it does not affect the relative ranking the scorer needs.
    """
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in returns:
        equity *= 1.0 + r
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    sd = _std(returns)
    downside = [r for r in returns if r < 0]
    dd = (sum(r**2 for r in downside) / len(returns)) ** 0.5 if downside and returns else 0.0
    # A risk-adjusted ratio is statistically undefined when the sample's
    # dispersion is degenerate (e.g. two nearly identical fills one bar
    # apart): sd approaches 0 and the ratio explodes into the billions,
    # overflowing Numeric(10, 4). Persist NULL for undefined, never a
    # numerically meaningless value.
    sharpe = _bounded_ratio(_mean(returns), sd)
    sortino = _bounded_ratio(_mean(returns), dd)
    return _Metrics(
        total_return=equity - 1.0,
        sharpe=sharpe,
        sortino=sortino,
        win_rate=len(wins) / len(returns) if returns else 0.0,
        avg_win=_mean(wins) if wins else None,
        avg_loss=_mean(losses) if losses else None,
        max_drawdown=max_dd,
        sample_size=len(returns),
    )


class ModePerformanceWriter:
    """Aggregate executed signal outcomes into ``mode_performance`` rows."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def update(self, *, lookback_days: int = 90, now: datetime | None = None) -> int:
        """Recompute ModePerformance over the trailing ``lookback_days``.

        Returns the number of (account, strategy, instrument, mode, horizon)
        rows upserted.
        """
        ref = now or datetime.now(tz=UTC)
        cutoff = ref - timedelta(days=max(1, lookback_days))

        with Session(self._engine) as session:
            metrics = session.scalars(
                select(ExecutionMetric)
                .where(
                    ExecutionMetric.signal_id.is_not(None),
                    ExecutionMetric.orders_filled > 0,
                    ExecutionMetric.created_at >= cutoff,
                )
                .order_by(
                    ExecutionMetric.account_id,
                    ExecutionMetric.strategy_id,
                    ExecutionMetric.symbol,
                    ExecutionMetric.execution_mode,
                    ExecutionMetric.created_at,
                    ExecutionMetric.metric_id,
                )
            ).all()

            attributed_metrics = [(metric, _realized_contributions(metric)) for metric in metrics]
            entry_signal_ids = {
                contribution.entry_canonical_signal_id
                for _metric, contributions in attributed_metrics
                for contribution in contributions
            }
            performance_by_signal: dict[int, list[SignalPerformance]] = {}
            if entry_signal_ids:
                performance_rows = session.scalars(
                    select(SignalPerformance)
                    .where(
                        SignalPerformance.signal_id.in_(entry_signal_ids),
                        SignalPerformance.did_execute.is_(True),
                    )
                    .order_by(
                        SignalPerformance.signal_id,
                        SignalPerformance.evaluation_horizon,
                        SignalPerformance.perf_id,
                    )
                ).all()
                for performance in performance_rows:
                    performance_by_signal.setdefault(int(performance.signal_id), []).append(
                        performance
                    )

            # One opening signal is deliberately evaluated at multiple raw
            # horizons. A realised FIFO match contributes once to every distinct
            # mode-performance bucket represented by those evaluations, but not
            # twice when (for example) both 15min and 1h normalize to intraday.
            seen_contribution_buckets: set[tuple[str, int, int, int, str]] = set()
            # key -> (returns, timestamps)
            groups: dict[
                tuple[int, str, int, str, str],
                tuple[list[float], list[datetime]],
            ] = {}
            for metric, contributions in attributed_metrics:
                for contribution in contributions:
                    for performance in performance_by_signal.get(
                        contribution.entry_canonical_signal_id, []
                    ):
                        if str(performance.strategy_id) != str(metric.strategy_id):
                            msg = "Realized contribution escaped its strategy boundary"
                            raise ModePerformanceIntegrityError(msg)
                        instr_id = int(performance.instr_id)
                        bucket = normalize_mode_horizon(performance.evaluation_horizon)
                        contribution_key = (
                            str(metric.metric_id),
                            contribution.entry_exec_id,
                            contribution.exit_exec_id,
                            instr_id,
                            bucket,
                        )
                        if contribution_key in seen_contribution_buckets:
                            continue
                        seen_contribution_buckets.add(contribution_key)
                        trade_return = contribution.realized_pnl / contribution.deployed_capital
                        key = (
                            int(metric.account_id),
                            str(metric.strategy_id),
                            instr_id,
                            str(metric.execution_mode),
                            bucket,
                        )
                        rets, tss = groups.setdefault(key, ([], []))
                        rets.append(float(trade_return))
                        tss.append(metric.created_at)

            for (
                account_id,
                strategy_id,
                instr_id,
                mode,
                bucket,
            ), (returns, tss) in groups.items():
                self._upsert(
                    session,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    instr_id=instr_id,
                    mode=mode,
                    horizon=bucket,
                    metrics=_metrics(returns),
                    period_start=min(tss),
                    period_end=max(tss),
                )
            session.commit()
            return len(groups)

    def _upsert(
        self,
        session: Session,
        *,
        account_id: int,
        strategy_id: str,
        instr_id: int,
        mode: str,
        horizon: str,
        metrics: _Metrics,
        period_start: datetime,
        period_end: datetime,
    ) -> None:
        values = {
            "account_id": account_id,
            "strategy_id": strategy_id,
            "instr_id": instr_id,
            "execution_mode": mode,
            "horizon": horizon,
            "period_start": period_start,
            "period_end": period_end,
            "total_return": _dec(metrics.total_return),
            "sharpe_ratio": _dec(metrics.sharpe) if metrics.sharpe is not None else None,
            "sortino_ratio": _dec(metrics.sortino) if metrics.sortino is not None else None,
            "win_rate": _dec(metrics.win_rate),
            "avg_win": _dec(metrics.avg_win) if metrics.avg_win is not None else None,
            "avg_loss": _dec(metrics.avg_loss) if metrics.avg_loss is not None else None,
            "max_drawdown": _dec(metrics.max_drawdown),
            "sample_size": metrics.sample_size,
            "updated_at": datetime.now(tz=UTC),
        }
        conflict_columns = [
            ModePerformance.account_id,
            ModePerformance.strategy_id,
            ModePerformance.instr_id,
            ModePerformance.execution_mode,
            ModePerformance.horizon,
        ]
        updated_values = {
            key: value
            for key, value in values.items()
            if key not in {"account_id", "strategy_id", "instr_id", "execution_mode", "horizon"}
        }
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            session.execute(
                postgresql_insert(ModePerformance)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=conflict_columns,
                    index_where=ModePerformance.instr_id.is_not(None),
                    set_=updated_values,
                )
            )
        elif dialect_name == "sqlite":
            session.execute(
                sqlite_insert(ModePerformance)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=conflict_columns,
                    index_where=ModePerformance.instr_id.is_not(None),
                    set_=updated_values,
                )
            )
        else:
            msg = f"Unsupported mode-performance persistence dialect: {dialect_name}"
            raise RuntimeError(msg)
