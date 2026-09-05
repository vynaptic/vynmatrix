"""
Drawdown tracking service.

Computes and tracks drawdown metrics:
- Current drawdown percentage
- Maximum drawdown (peak-to-trough)
- Drawdown duration (time in drawdown)
- Recovery tracking
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from .fx_rates import normalize_currency


class DrawdownLedgerUnavailableError(RuntimeError):
    """Account-scoped drawdown history cannot be read or written safely."""


@dataclass
class DrawdownSnapshot:
    """Point-in-time drawdown state."""

    user_id: str
    account_id: int
    strategy_id: str | None
    timestamp: datetime

    # Equity values
    current_equity: Decimal
    peak_equity: Decimal
    trough_equity: Decimal

    # Drawdown metrics
    current_drawdown_pct: float
    max_drawdown_pct: float
    max_drawdown_amount: Decimal

    # Duration metrics
    in_drawdown: bool
    drawdown_start: datetime | None
    current_drawdown_duration_hours: float
    max_drawdown_duration_hours: float

    # Recovery metrics
    recovery_needed_pct: float
    is_recovering: bool


@dataclass
class EquityPoint:
    """Single equity data point.

    ``DailyNav`` stores account-level NAV only (no cash/positions split), so an
    equity point carries just the timestamp and total equity.
    """

    timestamp: datetime
    equity: Decimal


class DrawdownService:
    """
    Service for tracking and computing drawdown metrics.

    Tracks:
    - Rolling peak equity
    - Current drawdown from peak
    - Maximum historical drawdown
    - Time spent in drawdown
    - Recovery progress

    Usage:
        service = DrawdownService(session_factory=get_session)

        # Get current drawdown state
        snapshot = await service.get_drawdown_snapshot(
            user_id="user_001",
            account_id=101,
            current_equity=Decimal("105000"),
        )

        # Update tracking
        await service.update_drawdown(
            user_id="user_001",
            account_id=101,
            equity=Decimal("105000"),
            nav_ccy="EUR",
        )
    """

    # Threshold for considering "in drawdown" (0.5%)
    DRAWDOWN_THRESHOLD = 0.005

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Any]] | None = None,
    ) -> None:
        """
        Initialize drawdown service.

        Args:
            session_factory: Factory for database sessions
        """
        self._session_factory = session_factory

    async def get_drawdown_snapshot(
        self,
        user_id: str,
        current_equity: Decimal,
        *,
        account_id: int,
        strategy_id: str | None = None,
    ) -> DrawdownSnapshot:
        """
        Get current drawdown state for a user.

        Args:
            user_id: User identifier
            account_id: Owned linked broker account
            current_equity: Current total equity
            strategy_id: Optional strategy filter

        Returns:
            DrawdownSnapshot with current drawdown metrics
        """
        # Fetch historical equity data
        history = await self._get_equity_history(user_id, account_id=account_id)

        if not history:
            # No history - no drawdown
            now = datetime.now(tz=UTC)
            return DrawdownSnapshot(
                user_id=user_id,
                account_id=account_id,
                strategy_id=strategy_id,
                timestamp=now,
                current_equity=current_equity,
                peak_equity=current_equity,
                trough_equity=current_equity,
                current_drawdown_pct=0.0,
                max_drawdown_pct=0.0,
                max_drawdown_amount=Decimal("0"),
                in_drawdown=False,
                drawdown_start=None,
                current_drawdown_duration_hours=0.0,
                max_drawdown_duration_hours=0.0,
                recovery_needed_pct=0.0,
                is_recovering=False,
            )

        return self._compute_drawdown_snapshot(
            user_id=user_id,
            account_id=account_id,
            strategy_id=strategy_id,
            current_equity=current_equity,
            history=history,
        )

    def _compute_drawdown_snapshot(
        self,
        user_id: str,
        account_id: int,
        strategy_id: str | None,
        current_equity: Decimal,
        history: list[EquityPoint],
    ) -> DrawdownSnapshot:
        """Compute drawdown metrics from equity history."""
        now = datetime.now(tz=UTC)

        # Calculate peak from history + current
        all_equities = [p.equity for p in history] + [current_equity]
        peak_equity = max(all_equities)

        # Calculate trough (minimum after peak)
        # Find the MOST RECENT occurrence of peak equity to avoid overstating duration
        peak_time = None
        sorted_history = sorted(history, key=lambda p: p.timestamp, reverse=True)
        for p in sorted_history:
            if p.equity == peak_equity:
                peak_time = p.timestamp
                break

        trough_equity = current_equity
        if peak_time:
            post_peak = [p.equity for p in history if p.timestamp >= peak_time]
            if post_peak:
                trough_equity = min([*post_peak, current_equity])

        # Current drawdown
        current_drawdown_pct = 0.0
        if peak_equity > 0:
            current_drawdown_pct = float((peak_equity - current_equity) / peak_equity)

        # Maximum drawdown calculation
        max_drawdown_pct, max_drawdown_amount = self._calculate_max_drawdown(history)

        # Check if current drawdown exceeds historical max
        if current_drawdown_pct > max_drawdown_pct:
            max_drawdown_pct = current_drawdown_pct
            max_drawdown_amount = peak_equity - current_equity

        # Drawdown state
        in_drawdown = current_drawdown_pct > self.DRAWDOWN_THRESHOLD

        # Find drawdown start
        drawdown_start = None
        current_duration = 0.0
        if in_drawdown:
            drawdown_start = self._find_drawdown_start(history, peak_equity)
            if drawdown_start:
                current_duration = (now - drawdown_start).total_seconds() / 3600

        # Max drawdown duration from history
        max_duration = self._calculate_max_drawdown_duration(history)
        max_duration = max(max_duration, current_duration)

        # Recovery metrics
        recovery_needed_pct = 0.0
        is_recovering = False
        if in_drawdown and trough_equity < current_equity:
            is_recovering = True
            if peak_equity > current_equity:
                recovery_needed_pct = float((peak_equity - current_equity) / current_equity * 100)

        return DrawdownSnapshot(
            user_id=user_id,
            account_id=account_id,
            strategy_id=strategy_id,
            timestamp=now,
            current_equity=current_equity,
            peak_equity=peak_equity,
            trough_equity=trough_equity,
            current_drawdown_pct=current_drawdown_pct,
            max_drawdown_pct=max_drawdown_pct,
            max_drawdown_amount=max_drawdown_amount,
            in_drawdown=in_drawdown,
            drawdown_start=drawdown_start,
            current_drawdown_duration_hours=current_duration,
            max_drawdown_duration_hours=max_duration,
            recovery_needed_pct=recovery_needed_pct,
            is_recovering=is_recovering,
        )

    def _calculate_max_drawdown(self, history: list[EquityPoint]) -> tuple[float, Decimal]:
        """Calculate maximum drawdown from equity history."""
        if not history:
            return 0.0, Decimal("0")

        max_dd_pct = 0.0
        max_dd_amount = Decimal("0")
        running_peak = Decimal("0")

        for point in sorted(history, key=lambda p: p.timestamp):
            running_peak = max(running_peak, point.equity)

            if running_peak > 0:
                dd_pct = float((running_peak - point.equity) / running_peak)
                dd_amount = running_peak - point.equity

                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct
                    max_dd_amount = dd_amount

        return max_dd_pct, max_dd_amount

    def _find_drawdown_start(
        self,
        history: list[EquityPoint],
        peak_equity: Decimal,
    ) -> datetime | None:
        """Find when the current drawdown started."""
        sorted_history = sorted(history, key=lambda p: p.timestamp, reverse=True)

        for point in sorted_history:
            if point.equity >= peak_equity:
                return point.timestamp

        # If no peak found, drawdown started from beginning
        return sorted_history[-1].timestamp if sorted_history else None

    def _calculate_max_drawdown_duration(self, history: list[EquityPoint]) -> float:
        """Calculate maximum time spent in drawdown (hours)."""
        if not history:
            return 0.0

        sorted_history = sorted(history, key=lambda p: p.timestamp)
        max_duration = 0.0
        current_duration = 0.0
        running_peak = Decimal("0")
        drawdown_start: datetime | None = None

        for point in sorted_history:
            if point.equity >= running_peak:
                # New peak - reset drawdown tracking
                if drawdown_start:
                    duration = (point.timestamp - drawdown_start).total_seconds() / 3600
                    max_duration = max(max_duration, duration)
                    drawdown_start = None
                running_peak = point.equity
            elif running_peak > 0:
                dd_pct = float((running_peak - point.equity) / running_peak)
                if dd_pct > self.DRAWDOWN_THRESHOLD and drawdown_start is None:
                    drawdown_start = point.timestamp

        # Handle ongoing drawdown
        if drawdown_start and sorted_history:
            current_duration = (
                sorted_history[-1].timestamp - drawdown_start
            ).total_seconds() / 3600
            max_duration = max(max_duration, current_duration)

        return max_duration

    async def update_drawdown(
        self,
        user_id: str,
        equity: Decimal,
        *,
        account_id: int,
        nav_ccy: str,
        strategy_id: str | None = None,
    ) -> None:
        """
        Record today's NAV snapshot and update drawdown tracking.

        Args:
            user_id: User identifier
            account_id: Owned linked broker account
            equity: Total equity value (today's NAV)
            nav_ccy: Explicit currency of ``equity``; must match the account
            strategy_id: Optional strategy filter (used for the snapshot only;
                ``DailyNav`` is account-level and has no per-strategy column).
        """
        if self._session_factory is None:
            return

        try:
            from lib_application.db.models import DailyNav  # noqa: PLC0415

            snapshot = await self.get_drawdown_snapshot(
                user_id=user_id,
                current_equity=equity,
                account_id=account_id,
                strategy_id=strategy_id,
            )
            drawdown = Decimal(str(snapshot.current_drawdown_pct))

            with self._session_factory() as session:
                account_ccy = self._resolve_scoped_account_currency(
                    session,
                    user_id=user_id,
                    account_id=account_id,
                    require_connected=True,
                )
                self._validate_nav_currency(
                    nav_ccy,
                    account_currency=account_ccy,
                    account_id=account_id,
                )

                today = datetime.now(tz=UTC).date()
                existing = (
                    session.query(DailyNav)
                    .filter(
                        DailyNav.user_id == user_id,
                        DailyNav.account_id == account_id,
                        DailyNav.date == today,
                    )
                    .first()
                )

                if existing:
                    existing.nav_value = equity
                    existing.nav_ccy = account_ccy
                    existing.drawdown = drawdown
                else:
                    session.add(
                        DailyNav(
                            user_id=user_id,
                            account_id=account_id,
                            date=today,
                            nav_ccy=account_ccy,
                            nav_value=equity,
                            drawdown=drawdown,
                        )
                    )

                session.commit()

        except DrawdownLedgerUnavailableError:
            raise
        except (ImportError, SQLAlchemyError, OSError, TypeError, ValueError) as exc:
            msg = f"Unable to update drawdown for user={user_id} account={account_id}"
            raise DrawdownLedgerUnavailableError(msg) from exc

    async def _get_equity_history(
        self,
        user_id: str,
        *,
        account_id: int,
        days: int = 365,
    ) -> list[EquityPoint]:
        """Fetch equity history for exactly one owned broker account."""
        if self._session_factory is None:
            return []

        try:
            from lib_application.db.models import DailyNav  # noqa: PLC0415

            cutoff = datetime.now(tz=UTC) - timedelta(days=days)

            with self._session_factory() as session:
                account_ccy = self._resolve_scoped_account_currency(
                    session,
                    user_id=user_id,
                    account_id=account_id,
                )
                rows = (
                    session.query(DailyNav)
                    .filter(
                        DailyNav.user_id == user_id,
                        DailyNav.account_id == account_id,
                        DailyNav.date >= cutoff.date(),
                    )
                    .order_by(DailyNav.date)
                    .all()
                )
                for row in rows:
                    self._validate_nav_currency(
                        row.nav_ccy,
                        account_currency=account_ccy,
                        account_id=account_id,
                    )

                return [
                    EquityPoint(
                        timestamp=datetime.combine(
                            row.date,
                            datetime.min.time(),
                            tzinfo=UTC,
                        ),
                        equity=row.nav_value,
                    )
                    for row in rows
                ]

        except DrawdownLedgerUnavailableError:
            raise
        except (ImportError, SQLAlchemyError, OSError, TypeError, ValueError) as exc:
            msg = f"Unable to read drawdown history for user={user_id} account={account_id}"
            raise DrawdownLedgerUnavailableError(msg) from exc

    async def get_drawdown_report(
        self,
        user_id: str,
        *,
        account_id: int,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate a comprehensive drawdown report.

        Args:
            user_id: User identifier
            account_id: Owned linked broker account
            strategy_id: Optional strategy filter

        Returns:
            Dictionary with drawdown analysis
        """
        history = await self._get_equity_history(user_id, account_id=account_id)

        if not history:
            return {
                "user_id": user_id,
                "account_id": account_id,
                "strategy_id": strategy_id,
                "has_data": False,
                "message": "No equity history available",
            }

        current_equity = history[-1].equity if history else Decimal("0")
        snapshot = await self.get_drawdown_snapshot(
            user_id=user_id,
            current_equity=current_equity,
            account_id=account_id,
            strategy_id=strategy_id,
        )

        # Calculate drawdown periods
        drawdown_periods = self._identify_drawdown_periods(history)

        return {
            "user_id": user_id,
            "account_id": account_id,
            "strategy_id": strategy_id,
            "has_data": True,
            "current_state": {
                "equity": float(snapshot.current_equity),
                "peak_equity": float(snapshot.peak_equity),
                "trough_equity": float(snapshot.trough_equity),
                "in_drawdown": snapshot.in_drawdown,
                "current_drawdown_pct": round(snapshot.current_drawdown_pct * 100, 2),
                "drawdown_duration_hours": round(snapshot.current_drawdown_duration_hours, 1),
                "is_recovering": snapshot.is_recovering,
                "recovery_needed_pct": round(snapshot.recovery_needed_pct, 2),
            },
            "historical": {
                "max_drawdown_pct": round(snapshot.max_drawdown_pct * 100, 2),
                "max_drawdown_amount": float(snapshot.max_drawdown_amount),
                "max_drawdown_duration_hours": round(snapshot.max_drawdown_duration_hours, 1),
                "drawdown_periods": len(drawdown_periods),
            },
            "drawdown_periods": [
                {
                    "start": p["start"].isoformat(),
                    "end": p["end"].isoformat() if p["end"] else None,
                    "peak_equity": float(p["peak"]),
                    "trough_equity": float(p["trough"]),
                    "drawdown_pct": round(p["drawdown_pct"] * 100, 2),
                    "duration_hours": round(p["duration_hours"], 1),
                    "recovered": p["recovered"],
                }
                for p in drawdown_periods[-10:]  # Last 10 periods
            ],
            "generated_at": datetime.now(tz=UTC).isoformat(),
        }

    @staticmethod
    def _validate_nav_currency(
        nav_ccy: str,
        *,
        account_currency: str,
        account_id: int,
    ) -> None:
        try:
            supplied_ccy = normalize_currency(nav_ccy)
        except ValueError as exc:
            msg = "Drawdown NAV requires an explicit valid nav_ccy"
            raise DrawdownLedgerUnavailableError(msg) from exc
        if supplied_ccy != account_currency:
            msg = (
                f"Drawdown NAV currency {supplied_ccy} does not match broker "
                f"account {account_id} base currency {account_currency}"
            )
            raise DrawdownLedgerUnavailableError(msg)

    @staticmethod
    def _resolve_scoped_account_currency(
        session: Any,
        *,
        user_id: str,
        account_id: int,
        require_connected: bool = False,
    ) -> str:
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            msg = "Drawdown tracking requires a positive broker account_id"
            raise DrawdownLedgerUnavailableError(msg)

        from lib_application.db.models import LinkedBrokerAccount  # noqa: PLC0415

        query = session.query(LinkedBrokerAccount).filter(
            LinkedBrokerAccount.account_id == account_id,
            LinkedBrokerAccount.user_id == str(user_id),
        )
        if require_connected:
            query = query.filter(LinkedBrokerAccount.status == "connected")
        account = query.one_or_none()
        if account is None:
            state = "connected " if require_connected else ""
            msg = (
                f"{state.capitalize()}broker account {account_id} is unavailable for user {user_id}"
            )
            raise DrawdownLedgerUnavailableError(msg)
        try:
            return str(normalize_currency(account.base_ccy))
        except ValueError as exc:
            msg = f"Broker account {account_id} has no valid base currency"
            raise DrawdownLedgerUnavailableError(msg) from exc

    def _identify_drawdown_periods(self, history: list[EquityPoint]) -> list[dict[str, Any]]:
        """Identify distinct drawdown periods from equity history."""
        if not history:
            return []

        periods: list[dict[str, Any]] = []
        sorted_history = sorted(history, key=lambda p: p.timestamp)

        running_peak = Decimal("0")
        current_period: dict[str, Any] | None = None

        for point in sorted_history:
            if point.equity >= running_peak:
                # New peak
                if current_period and current_period["trough"] < running_peak:
                    # Close current period as recovered
                    current_period["end"] = point.timestamp
                    current_period["recovered"] = True
                    duration = (
                        current_period["end"] - current_period["start"]
                    ).total_seconds() / 3600
                    current_period["duration_hours"] = duration
                    periods.append(current_period)
                    current_period = None

                running_peak = point.equity
            else:
                # In drawdown
                dd_pct = float((running_peak - point.equity) / running_peak)

                if dd_pct > self.DRAWDOWN_THRESHOLD:
                    if current_period is None:
                        # Start new period
                        current_period = {
                            "start": point.timestamp,
                            "end": None,
                            "peak": running_peak,
                            "trough": point.equity,
                            "drawdown_pct": dd_pct,
                            "duration_hours": 0.0,
                            "recovered": False,
                        }
                    # Update trough if deeper
                    elif point.equity < current_period["trough"]:
                        current_period["trough"] = point.equity
                        current_period["drawdown_pct"] = dd_pct

        # Handle ongoing drawdown
        if current_period:
            current_period["end"] = sorted_history[-1].timestamp
            duration = (current_period["end"] - current_period["start"]).total_seconds() / 3600
            current_period["duration_hours"] = duration
            periods.append(current_period)

        return periods
