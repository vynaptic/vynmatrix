"""One-shot prospective producer for the US Quality Compounder panel."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import Instrument, MarketCalendar, MarketSession, StrategyVersion
from lib_infrastructure.market_data.eodhd_client import EODHDClient
from lib_strategy.equity_market_factors import EquityMarketFactorPolicy
from lib_strategy.equity_quality_compounder import QUALITY_COMPOUNDER_STRATEGY_VERSION
from lib_strategy.equity_transaction_costs import DailyBarCostModelPolicy

from .quality_compounder_eodhd import (
    AcquiredQualityCompounderUniverse,
    acquire_quality_compounder_benchmark_identity,
    acquire_quality_compounder_identities,
    acquire_quality_compounder_membership,
)
from .quality_compounder_factor_materializer import QualityCompounderDatabaseFactorResolver
from .quality_compounder_market import (
    QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY,
    AcquiredQualityCompounderMarketSeries,
    acquire_quality_compounder_market_series,
    persist_quality_compounder_market_series,
)
from .quality_compounder_panel import quality_compounder_provider_authority_policy
from .quality_compounder_quarterly import QualityCompounderQuarterlyWindow
from .quality_compounder_registration import (
    build_quality_compounder_materialization_panel,
    persist_quality_compounder_panel_revision,
)
from .quality_compounder_sec import (
    QualityCompounderIssuerSecGraph,
    acquire_quality_compounder_sec_graphs,
    persist_quality_compounder_sec_graphs,
)
from .quality_compounder_universe import (
    QualityCompounderSecurityIdentity,
    QualityCompounderUniverseComponent,
    persist_quality_compounder_benchmark_identity,
    persist_quality_compounder_universe,
    require_quality_compounder_catalogue,
)
from .sec_edgar import SecEdgarClient

_STRATEGY_ID = "us_quality_compounder_v1"
_STRATEGY_VERSION = QUALITY_COMPOUNDER_STRATEGY_VERSION
_BENCHMARK_SYMBOL = "SPY"


class QualityCompounderProducerError(RuntimeError):
    """A complete prospective panel cannot be produced before the next open."""


def _invalid(message: str) -> NoReturn:
    raise QualityCompounderProducerError(message)


SessionFactory = Callable[[], Session]


class QualityCompounderPanelProducer:
    """Acquire outside transactions, then persist and register one exact panel."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        eodhd_client: EODHDClient,
        sec_client: SecEdgarClient,
        entitlement_owner_user_id: str,
        market_policy: EquityMarketFactorPolicy,
        cost_policy: DailyBarCostModelPolicy,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(session_factory):
            _invalid("quality-compounder producer requires a session factory")
        owner = str(entitlement_owner_user_id).strip()
        if not owner or owner != entitlement_owner_user_id:
            _invalid("quality-compounder producer requires one canonical entitlement owner")
        if not isinstance(market_policy, EquityMarketFactorPolicy) or not isinstance(
            cost_policy, DailyBarCostModelPolicy
        ):
            _invalid("quality-compounder producer requires frozen market and cost policies")
        if market_policy.cost_context_sha256 != cost_policy.configuration_sha256:
            _invalid("market-factor and daily-cost policies use different cost identities")
        if market_policy.required_adjustment_policy != (
            QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY
        ):
            _invalid("market-factor policy uses another price-adjustment contract")
        self._session_factory = session_factory
        self._eodhd = eodhd_client
        self._sec = sec_client
        self._owner = owner
        self._authority = quality_compounder_provider_authority_policy(owner)
        self._market_policy = market_policy
        self._cost_policy = cost_policy
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    def produce(
        self,
        *,
        window: QualityCompounderQuarterlyWindow,
        started_at: datetime,
        complete_before: datetime,
    ) -> None:
        """Create one durable panel revision without holding a DB transaction over HTTP."""

        started = _utc(started_at, field_name="producer start")
        deadline = _utc(complete_before, field_name="producer deadline")
        if (
            deadline != window.execution_opens_at
            or not window.decision_closes_at <= started < deadline
        ):
            _invalid("quality-compounder production window is inconsistent")
        official_sessions = self._official_sessions(window)
        membership = acquire_quality_compounder_membership(
            client=self._eodhd,
            decision_session=window.decision_closes_at.date(),
            decision_close=window.decision_closes_at,
            complete_before=deadline,
        )
        instrument_ids = self._catalogue_preflight(
            window=window,
            components=membership.components,
        )
        universe = acquire_quality_compounder_identities(
            client=self._eodhd,
            membership=membership,
            complete_before=deadline,
        )
        benchmark = acquire_quality_compounder_benchmark_identity(
            client=self._eodhd,
            decision_close=window.decision_closes_at,
            complete_before=deadline,
        )
        market_series = tuple(
            acquire_quality_compounder_market_series(
                client=self._eodhd,
                identity=identity,
                instrument_id=instrument_ids[symbol],
                official_sessions=official_sessions,
                decision_session=membership.decision_session,
                required_history_sessions=self._market_policy.required_history_sessions,
                cost_policy=self._cost_policy,
                entitlement_owner_user_id=self._owner,
            )
            for symbol, identity in universe.identities.items()
        )
        benchmark_series = acquire_quality_compounder_market_series(
            client=self._eodhd,
            identity=benchmark,
            instrument_id=instrument_ids[_BENCHMARK_SYMBOL],
            official_sessions=official_sessions,
            decision_session=membership.decision_session,
            required_history_sessions=self._market_policy.required_history_sessions,
            cost_policy=self._cost_policy,
            entitlement_owner_user_id=self._owner,
        )
        sec_cutoff = self._now_before(deadline, field_name="SEC acquisition cutoff")
        sec_graphs = acquire_quality_compounder_sec_graphs(
            self._sec,
            identities=tuple(universe.identities.values()),
            cutoff=sec_cutoff,
        )
        cutoff = self._now_before(deadline, field_name="panel cutoff")
        self._require_acquisition_cutoff(
            cutoff=cutoff,
            decision_close=window.decision_closes_at,
            universe_acquired_at=universe.acquired_at,
            market_series=(*market_series, benchmark_series),
            sec_graphs=sec_graphs,
        )
        self._persist_and_register(
            window=window,
            cutoff=cutoff,
            universe=universe,
            benchmark=benchmark,
            market_series=(*market_series, benchmark_series),
            sec_graphs=sec_graphs,
        )

    def _official_sessions(
        self,
        window: QualityCompounderQuarterlyWindow,
    ) -> tuple[tuple[datetime, datetime], ...]:
        with self._session_factory() as session:
            calendar = session.get(MarketCalendar, window.calendar_id)
            rows = tuple(
                session.scalars(
                    select(MarketSession)
                    .where(MarketSession.calendar_id == window.calendar_id)
                    .order_by(MarketSession.opens_at)
                )
            )
            if (
                calendar is None
                or str(calendar.code) != "XNYS"
                or str(calendar.source_kind) != "exchange"
                or calendar.observation_id is None
                or not rows
            ):
                _invalid("producer requires one immutable shared XNYS calendar")
            decision = next(
                (item for item in rows if int(item.session_id) == window.decision_session_id),
                None,
            )
            execution = next(
                (item for item in rows if int(item.session_id) == window.execution_session_id),
                None,
            )
            if (
                decision is None
                or execution is None
                or _utc(decision.closes_at, field_name="decision close")
                != window.decision_closes_at
                or _utc(execution.opens_at, field_name="execution open")
                != window.execution_opens_at
            ):
                _invalid("producer window differs from persisted XNYS sessions")
            return tuple(
                (
                    _utc(item.opens_at, field_name="official open"),
                    _utc(item.closes_at, field_name="official close"),
                )
                for item in rows
            )

    def _catalogue_preflight(
        self,
        *,
        window: QualityCompounderQuarterlyWindow,
        components: Sequence[QualityCompounderUniverseComponent],
    ) -> Mapping[str, int]:
        with self._session_factory() as session:
            member_ids = require_quality_compounder_catalogue(session, components)
            benchmark = session.scalar(
                select(Instrument).where(
                    Instrument.asset_class == "etf",
                    Instrument.canonical == _BENCHMARK_SYMBOL,
                )
            )
            if (
                benchmark is None
                or not bool(benchmark.is_tradable)
                or str(benchmark.settlement_currency) != "USD"
                or str(benchmark.market_session_policy) != "scheduled"
                or benchmark.market_calendar_id != window.calendar_id
            ):
                _invalid("SPY lacks exact shared-calendar ETF catalogue authority")
            return {**member_ids, _BENCHMARK_SYMBOL: int(benchmark.instr_id)}

    def _persist_and_register(
        self,
        *,
        window: QualityCompounderQuarterlyWindow,
        cutoff: datetime,
        universe: AcquiredQualityCompounderUniverse,
        benchmark: QualityCompounderSecurityIdentity,
        market_series: Sequence[AcquiredQualityCompounderMarketSeries],
        sec_graphs: Sequence[QualityCompounderIssuerSecGraph],
    ) -> None:
        with self._session_factory() as session, session.begin():
            persist_quality_compounder_universe(
                session,
                components=universe.membership.components,
                identities=universe.identities,
                current_evidence=universe.membership.current_evidence,
                historical_evidence=universe.membership.historical_evidence,
                ticker_history_evidence=universe.membership.ticker_history_evidence,
                decision_session=universe.membership.decision_session,
                decision_close=window.decision_closes_at,
                cutoff=cutoff,
                entitlement_owner_user_id=self._owner,
            )
            persist_quality_compounder_benchmark_identity(
                session,
                identity=benchmark,
                decision_session=window.decision_closes_at.date(),
                decision_close=window.decision_closes_at,
                cutoff=cutoff,
                entitlement_owner_user_id=self._owner,
            )
            for acquired_market in market_series:
                persist_quality_compounder_market_series(session, acquired_market)
            persist_quality_compounder_sec_graphs(session, sec_graphs)
            version = session.scalar(
                select(StrategyVersion).where(
                    StrategyVersion.strategy_id == _STRATEGY_ID,
                    StrategyVersion.semver == _STRATEGY_VERSION,
                )
            )
            if version is None or str(version.status) != "active":
                _invalid("quality-compounder strategy version is unavailable or inactive")
            source_panel = build_quality_compounder_materialization_panel(
                session,
                window=window,
                cutoff=cutoff,
                provider_authority_policy=self._authority,
            )
            materialized = QualityCompounderDatabaseFactorResolver(
                session,
                provider_authority_policy=self._authority,
            ).materialize(
                panel=source_panel,
                strategy_version=_STRATEGY_VERSION,
                strategy_version_id=int(version.strat_ver_id),
                market_policy=self._market_policy,
            )
            registration_time = self._now_before(
                window.execution_opens_at,
                field_name="panel registration clock",
            )
            persist_quality_compounder_panel_revision(
                session,
                window=window,
                cutoff=cutoff,
                now=registration_time,
                market=materialized.market_snapshot,
                market_policy=self._market_policy,
                fundamentals=materialized.fundamental_snapshot,
                market_cap_by_symbol=materialized.market_cap_by_symbol,
                provider_authority_policy=self._authority,
                entitlement_owner_user_id=self._owner,
            )
            self._now_before(
                window.execution_opens_at,
                field_name="panel commit clock",
            )

    def _now_before(self, deadline: datetime, *, field_name: str) -> datetime:
        current = _utc(self._clock(), field_name=field_name)
        if current >= deadline:
            _invalid(f"{field_name} reached the execution-session open")
        return current

    @staticmethod
    def _require_acquisition_cutoff(
        *,
        cutoff: datetime,
        decision_close: datetime,
        universe_acquired_at: datetime,
        market_series: Sequence[AcquiredQualityCompounderMarketSeries],
        sec_graphs: Sequence[QualityCompounderIssuerSecGraph],
    ) -> None:
        retrievals = [universe_acquired_at]
        retrievals.extend(item.available_at for item in market_series)
        for graph in sec_graphs:
            for fact in graph.accepted_facts:
                retrievals.extend((fact.fact.source.retrieved_at, fact.filing_source.retrieved_at))
                if fact.historical_sic_source is not None:
                    retrievals.append(fact.historical_sic_source.retrieved_at)
        close = _utc(decision_close, field_name="decision close")
        if not retrievals or any(
            not close <= _utc(item, field_name="retrieval") <= cutoff for item in retrievals
        ):
            _invalid("source graph retrieval falls outside the admitted panel cutoff")


def _utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "QualityCompounderPanelProducer",
    "QualityCompounderProducerError",
]
