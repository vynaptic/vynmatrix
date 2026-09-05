"""Resolve account-owned equity baselines for pre-trade risk controls.

Daily-loss policy uses the prior persisted ``daily_nav`` close. Drawdown policy
uses a durable account-wide peak source: an exact execution metric or, for an
exact connected paper account, its configured initial equity. The latter lets
a new paper account establish its first portfolio position without inventing an
execution metric or resetting the peak to a later broker balance.

The current equity observation remains a separate authority. Portfolio entries
may rely on the paper-account bootstrap only when the balance came directly
from the broker adapter and its user, account, currency, and observation time
match the owned account. Live accounts without durable history, profile-cache
snapshots, mismatches, and database failures remain fail closed.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from lib_application.db.models import DailyNav, ExecutionMetric, LinkedBrokerAccount

from .metrics.fx_rates import normalize_currency

AccountEquityObservationSource = Literal["broker", "profile_cache", "unattributed"]
RiskPeakSource = Literal["execution_metric", "paper_account_initial_equity"]


class RiskBaselineUnavailableError(RuntimeError):
    """A risk baseline cannot be attributed to the requested owned account."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _positive_equity(value: float, *, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        msg = f"Risk baseline {field_name} must be finite and positive"
        raise RiskBaselineUnavailableError(msg) from exc
    if not math.isfinite(result) or result <= 0:
        msg = f"Risk baseline {field_name} must be finite and positive"
        raise RiskBaselineUnavailableError(msg)
    return result


@dataclass(frozen=True, slots=True)
class AccountEquityObservation:
    """One immutable account-equity observation supplied by execution state."""

    user_id: str
    account_id: int
    broker_reported_account_id: str
    currency: str
    equity: float
    observed_at: datetime
    source: AccountEquityObservationSource

    def __post_init__(self) -> None:
        normalized_user = str(self.user_id or "").strip()
        broker_reported_account_id = str(self.broker_reported_account_id or "").strip()
        if not normalized_user:
            msg = "Account equity observation requires user_id"
            raise RiskBaselineUnavailableError(msg)
        if not broker_reported_account_id:
            msg = "Account equity observation requires broker-reported account identity"
            raise RiskBaselineUnavailableError(msg)
        _validate_account_id(self.account_id)
        try:
            currency = normalize_currency(self.currency)
        except ValueError as exc:
            msg = "Account equity observation requires a valid currency"
            raise RiskBaselineUnavailableError(msg) from exc
        equity = _positive_equity(self.equity, field_name="current_equity")
        source = str(self.source or "").strip()
        if source not in {"broker", "profile_cache", "unattributed"}:
            msg = f"Account equity observation has unsupported source {source!r}"
            raise RiskBaselineUnavailableError(msg)
        object.__setattr__(self, "user_id", normalized_user)
        object.__setattr__(
            self,
            "broker_reported_account_id",
            broker_reported_account_id,
        )
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "equity", equity)
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        object.__setattr__(self, "source", source)

    def to_audit_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "broker_account_id": self.account_id,
            "broker_reported_account_id": self.broker_reported_account_id,
            "currency": self.currency,
            "equity": self.equity,
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class RiskPeakProvenance:
    """Exact durable source that established an account drawdown peak."""

    user_id: str
    account_id: int
    currency: str
    source: RiskPeakSource
    source_id: str
    equity: float
    observed_at: datetime

    def __post_init__(self) -> None:
        normalized_user = str(self.user_id or "").strip()
        source_id = str(self.source_id or "").strip()
        if not normalized_user or not source_id:
            msg = "Risk peak provenance requires user and source identity"
            raise RiskBaselineUnavailableError(msg)
        _validate_account_id(self.account_id)
        try:
            currency = normalize_currency(self.currency)
        except ValueError as exc:
            msg = "Risk peak provenance requires a valid currency"
            raise RiskBaselineUnavailableError(msg) from exc
        source = str(self.source or "").strip()
        if source not in {"execution_metric", "paper_account_initial_equity"}:
            msg = f"Risk peak provenance has unsupported source {source!r}"
            raise RiskBaselineUnavailableError(msg)
        equity = _positive_equity(self.equity, field_name="peak_equity")
        object.__setattr__(self, "user_id", normalized_user)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "equity", equity)
        object.__setattr__(self, "observed_at", _utc(self.observed_at))

    def to_audit_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "broker_account_id": self.account_id,
            "currency": self.currency,
            "source": self.source,
            "source_id": self.source_id,
            "equity": self.equity,
            "observed_at": self.observed_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class RiskBaseline:
    """Account-scoped values with source-agnostic durable peak provenance."""

    day_start_equity: float
    peak_equity: float
    peak_provenance: RiskPeakProvenance | None = None
    current_equity_observation: AccountEquityObservation | None = None

    def __post_init__(self) -> None:
        day_start = _positive_equity(
            self.day_start_equity,
            field_name="day_start_equity",
        )
        peak = _positive_equity(self.peak_equity, field_name="peak_equity")
        provenance = self.peak_provenance
        observation = self.current_equity_observation
        if provenance is not None and provenance.equity != peak:
            msg = "Risk peak value does not match its durable provenance"
            raise RiskBaselineUnavailableError(msg)
        if (
            provenance is not None
            and observation is not None
            and (
                provenance.user_id != observation.user_id
                or provenance.account_id != observation.account_id
                or provenance.currency != observation.currency
            )
        ):
            msg = "Risk peak and current equity provenance cross an account boundary"
            raise RiskBaselineUnavailableError(msg)
        object.__setattr__(self, "day_start_equity", day_start)
        object.__setattr__(self, "peak_equity", peak)

    @property
    def has_persisted_account_peak(self) -> bool:
        """Return whether an exact durable account source established the peak."""
        return self.peak_provenance is not None

    def to_audit_dict(self) -> dict[str, object]:
        return {
            "day_start_equity": self.day_start_equity,
            "peak_equity": self.peak_equity,
            "has_persisted_account_peak": self.has_persisted_account_peak,
            "peak_provenance": (
                self.peak_provenance.to_audit_dict() if self.peak_provenance else None
            ),
            "current_equity_observation": (
                self.current_equity_observation.to_audit_dict()
                if self.current_equity_observation
                else None
            ),
        }


def _validate_account_id(account_id: int) -> None:
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
        msg = "Risk baseline requires a positive broker account_id"
        raise RiskBaselineUnavailableError(msg)


def _validate_observation(
    observation: AccountEquityObservation | None,
    *,
    account: LinkedBrokerAccount,
    account_currency: str,
    current_equity: float,
    evaluated_at: datetime,
) -> None:
    if observation is None:
        return
    account_id = int(account.account_id)
    if observation.user_id != str(account.user_id) or observation.account_id != account_id:
        msg = "Current account equity observation does not match the owned account"
        raise RiskBaselineUnavailableError(msg)
    external_ref = str(account.external_ref or "").strip()
    expected_reported_id = external_ref or str(account_id)
    if observation.broker_reported_account_id != expected_reported_id:
        msg = "Current broker-reported account identity does not match the owned account"
        raise RiskBaselineUnavailableError(msg)
    if observation.currency != account_currency:
        msg = (
            f"Current equity currency for broker account {account_id} does not "
            f"match its persisted base currency {account_currency}"
        )
        raise RiskBaselineUnavailableError(msg)
    if observation.equity != current_equity:
        msg = "Current account equity changed between state validation and risk evaluation"
        raise RiskBaselineUnavailableError(msg)
    if observation.observed_at > evaluated_at:
        msg = "Current account equity observation cannot be future-dated"
        raise RiskBaselineUnavailableError(msg)


def _metric_peak(
    session: Any,
    *,
    user_id: str,
    account_id: int,
    account_currency: str,
) -> tuple[float, RiskPeakProvenance] | None:
    metric = (
        session.query(ExecutionMetric)
        .filter(
            ExecutionMetric.user_id == user_id,
            ExecutionMetric.account_id == account_id,
            ExecutionMetric.peak_equity.is_not(None),
            ExecutionMetric.peak_equity > 0,
        )
        .order_by(
            ExecutionMetric.peak_equity.desc(),
            ExecutionMetric.created_at.asc(),
            ExecutionMetric.metric_id.asc(),
        )
        .first()
    )
    if metric is None or metric.peak_equity is None:
        return None
    value = _positive_equity(float(metric.peak_equity), field_name="peak_equity")
    return (
        value,
        RiskPeakProvenance(
            user_id=user_id,
            account_id=account_id,
            currency=account_currency,
            source="execution_metric",
            source_id=str(metric.metric_id),
            equity=value,
            observed_at=metric.created_at,
        ),
    )


def _paper_initial_peak(
    account: LinkedBrokerAccount,
    *,
    observation: AccountEquityObservation | None,
    account_currency: str,
) -> tuple[float, RiskPeakProvenance] | None:
    if (
        account.environment != "paper"
        or account.status != "connected"
        or account.paper_initial_equity is None
        or observation is None
        or observation.source != "broker"
    ):
        return None
    value = _positive_equity(
        float(account.paper_initial_equity),
        field_name="paper_initial_equity",
    )
    return (
        value,
        RiskPeakProvenance(
            user_id=str(account.user_id),
            account_id=int(account.account_id),
            currency=account_currency,
            source="paper_account_initial_equity",
            source_id=f"linked-broker-account:{int(account.account_id)}",
            equity=value,
            observed_at=account.created_at,
        ),
    )


def resolve_risk_baseline(
    session_factory: Callable[[], AbstractContextManager[Any]] | None,
    *,
    user_id: str,
    account_id: int,
    current_equity: float,
    equity_observation: AccountEquityObservation | None = None,
    now: datetime | None = None,
) -> RiskBaseline:
    """Return account-owned daily and drawdown baselines.

    Ordinary isolated callers without persistence retain the historical
    current-equity bootstrap, but it carries no durable provenance and cannot
    authorize a portfolio entry. A connected paper account can use its exact
    configured initial equity only when ``equity_observation`` is broker-sourced
    and matches the persisted owner/account/currency boundary.
    """
    _validate_account_id(account_id)
    normalized_equity = _positive_equity(current_equity, field_name="current_equity")
    evaluated_at = _utc(now or datetime.now(tz=UTC))
    day_start_equity = normalized_equity
    if session_factory is None:
        return RiskBaseline(
            day_start_equity=day_start_equity,
            peak_equity=normalized_equity,
            current_equity_observation=equity_observation,
        )

    today = evaluated_at.date()
    with session_factory() as session:
        account = (
            session.query(LinkedBrokerAccount)
            .filter(
                LinkedBrokerAccount.account_id == account_id,
                LinkedBrokerAccount.user_id == str(user_id),
            )
            .one_or_none()
        )
        if account is None:
            msg = f"Broker account {account_id} is unavailable for user {user_id}"
            raise RiskBaselineUnavailableError(msg)
        try:
            account_ccy = normalize_currency(account.base_ccy)
        except ValueError as exc:
            msg = f"Broker account {account_id} has no valid base currency"
            raise RiskBaselineUnavailableError(msg) from exc
        _validate_observation(
            equity_observation,
            account=account,
            account_currency=account_ccy,
            current_equity=normalized_equity,
            evaluated_at=evaluated_at,
        )

        prior_nav = (
            session.query(DailyNav)
            .filter(
                DailyNav.user_id == user_id,
                DailyNav.account_id == account_id,
                DailyNav.date < today,
            )
            .order_by(DailyNav.date.desc())
            .first()
        )
        if prior_nav is not None and prior_nav.nav_value is not None:
            try:
                nav_ccy = normalize_currency(prior_nav.nav_ccy)
            except ValueError as exc:
                msg = f"Daily NAV for broker account {account_id} has no valid currency"
                raise RiskBaselineUnavailableError(msg) from exc
            if nav_ccy != account_ccy:
                msg = (
                    f"Daily NAV currency for broker account {account_id} does not "
                    f"match its persisted base currency {account_ccy}"
                )
                raise RiskBaselineUnavailableError(msg)
            day_start_equity = _positive_equity(
                float(prior_nav.nav_value),
                field_name="day_start_equity",
            )

        durable_candidates = [
            candidate
            for candidate in (
                _metric_peak(
                    session,
                    user_id=str(user_id),
                    account_id=account_id,
                    account_currency=account_ccy,
                ),
                _paper_initial_peak(
                    account,
                    observation=equity_observation,
                    account_currency=account_ccy,
                ),
            )
            if candidate is not None
        ]

    if durable_candidates:
        peak_equity, peak_provenance = max(
            durable_candidates,
            key=lambda item: (item[0], item[1].source, item[1].source_id),
        )
    else:
        peak_equity = normalized_equity
        peak_provenance = None
    return RiskBaseline(
        day_start_equity=day_start_equity,
        peak_equity=peak_equity,
        peak_provenance=peak_provenance,
        current_equity_observation=equity_observation,
    )
