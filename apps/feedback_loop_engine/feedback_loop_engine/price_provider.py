"""Price provider abstraction for feedback evaluation.

Supports source- and timeframe-aware lookups so feedback can prefer
the same data source the strategy used when emitting a signal.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import NoReturn

from sqlalchemy import desc, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lib_application.db.models import (
    CanonicalSignal,
    EquityObservation,
    EquityObservationValue,
    EquityRankSnapshot,
    EquityRankSnapshotRow,
    EquitySourceLineage,
    Instrument,
    InstrumentPrice,
    ModelRebalance,
    ModelRebalanceLeg,
)
from lib_application.services.equity_lineage import (
    EquityObservationAuthorityError,
    validate_equity_observation_authority,
)
from lib_common.hashing import canonical_json_hash
from lib_common.logging import get_logger
from lib_data.dataset import (
    UnsupportedSessionTimingError,
    fixed_duration_interval,
    timeframe_interval,
)
from lib_strategy.data_authority import DataUseScope, ProviderAuthorityPolicy
from lib_strategy.signals.utils import ensure_utc

from .models import EvaluationHorizon

logger = get_logger(__name__)

# Type alias for session factory
SessionFactory = Callable[[], AbstractContextManager[Session]]

DEFAULT_MAX_STALENESS = timedelta(days=5)
_DIGEST_LENGTH = 64


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)


class PriceObservationOrigin(str, Enum):
    """Auditable origin of a price used by the feedback evaluator."""

    PRICES_TABLE = "prices_table"
    CANONICAL_SIGNAL = "canonical_signal"
    EQUITY_OBSERVATION = "equity_observation"


@dataclass(frozen=True)
class PriceObservation:
    """A price plus the completed-bar provenance supporting it."""

    price_id: int | None
    price: float
    bar_open_ts: datetime | None
    bar_close_ts: datetime | None
    source: str | None
    timeframe: str | None
    origin: PriceObservationOrigin
    observation_id: str | None = None
    observation_sha256: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.price) or self.price <= 0.0:
            msg = "Price observation requires a finite positive price"
            raise ValueError(msg)
        if (self.bar_open_ts is None) != (self.bar_close_ts is None):
            msg = "Price observation bar timestamps must both be set or both be absent"
            raise ValueError(msg)
        if (
            self.bar_open_ts is not None
            and self.bar_close_ts is not None
            and ensure_utc(self.bar_close_ts) < ensure_utc(self.bar_open_ts)
        ):
            msg = "Price observation close timestamp cannot precede its open timestamp"
            raise ValueError(msg)

    def to_metadata(self) -> dict[str, int | float | str | None]:
        """Serialize provenance into ``SignalPerformance.meta``."""

        metadata: dict[str, int | float | str | None] = {
            "price_id": self.price_id,
            "price": self.price,
            "bar_open_ts": self.bar_open_ts.isoformat() if self.bar_open_ts else None,
            "bar_close_ts": self.bar_close_ts.isoformat() if self.bar_close_ts else None,
            "source": self.source,
            "timeframe": self.timeframe,
            "origin": self.origin.value,
        }
        if self.observation_id is not None:
            metadata["observation_id"] = self.observation_id
        if self.observation_sha256 is not None:
            metadata["observation_sha256"] = self.observation_sha256
        return metadata


@dataclass(frozen=True, slots=True)
class EquityPriceEvidenceContext:
    """Exact owner-authorized observation stream selected by a rank snapshot."""

    source: str
    timeframe: str
    entitlement_scope: str
    entitlement_owner_user_id: str | None
    entry_observation_id: str
    entry_observation_sha256: str
    rank_snapshot_id: str
    provider_authority_sha256: str
    data_use_scope: DataUseScope

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, object]) -> EquityPriceEvidenceContext:
        """Parse the complete fail-closed strategy metadata contract."""

        if metadata.get("price_store") != "equity_observations":
            _invalid("equity feedback requires price_store=equity_observations")
        if metadata.get("feedback_price_field") != "total_return_close":
            _invalid("equity feedback requires total_return_close semantics")
        source = _metadata_text(metadata, "price_source")
        if source != source.lower():
            _invalid("equity feedback price_source must be canonical lowercase")
        timeframe = _metadata_text(metadata, "price_timeframe")
        if timeframe != "1d":
            _invalid("equity feedback supports only registered daily observations")
        owner_raw = metadata.get("price_entitlement_owner_user_id")
        owner = (
            None
            if owner_raw is None
            else _metadata_text(metadata, "price_entitlement_owner_user_id")
        )
        try:
            scope = DataUseScope(_metadata_text(metadata, "data_use_scope"))
        except ValueError as exc:
            message = "equity feedback data_use_scope is invalid"
            raise ValueError(message) from exc
        return cls(
            source=source,
            timeframe=timeframe,
            entitlement_scope=_metadata_text(metadata, "price_entitlement_scope"),
            entitlement_owner_user_id=owner,
            entry_observation_id=_metadata_digest(metadata, "price_observation_id"),
            entry_observation_sha256=_metadata_digest(metadata, "price_observation_sha256"),
            rank_snapshot_id=_metadata_digest(metadata, "rank_snapshot_sha256"),
            provider_authority_sha256=_metadata_digest(metadata, "provider_authority_sha256"),
            data_use_scope=scope,
        )


def _metadata_text(metadata: Mapping[str, object], field_name: str) -> str:
    value = metadata.get(field_name)
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"equity feedback {field_name} must be canonical text")
    return value


def _metadata_digest(metadata: Mapping[str, object], field_name: str) -> str:
    value = _metadata_text(metadata, field_name)
    if len(value) != _DIGEST_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        _invalid(f"equity feedback {field_name} must be a lowercase SHA-256 digest")
    return value


def _as_database_timestamp(value: datetime) -> datetime:
    """Convert an instant to the repository's UTC-naive ``DateTime`` convention."""

    return ensure_utc(value).replace(tzinfo=None)


def evaluation_target(as_of: datetime, horizon: str) -> datetime:
    """Return the exact UTC signal-plus-horizon evaluation target."""

    # Fail closed: an unknown horizon raises ValueError instead of silently
    # evaluating against a default 1-day window.
    return ensure_utc(as_of) + EvaluationHorizon(horizon).duration


class PriceProvider:
    """Abstract base class for price lookup."""

    def get_entry_observation(
        self,
        instr_id: int,
        as_of: datetime,
        *,
        price_source: str | None = None,
        price_timeframe: str | None = None,
    ) -> PriceObservation | None:
        """Return the completed-bar observation at or before signal time."""

        raise NotImplementedError

    def get_exit_observation(
        self,
        instr_id: int,
        as_of: datetime,
        horizon: str,
        *,
        price_source: str | None = None,
        price_timeframe: str | None = None,
    ) -> PriceObservation | None:
        """Return the completed-bar observation near the evaluation horizon."""

        raise NotImplementedError

    def get_equity_entry_observation(
        self,
        instr_id: int,
        as_of: datetime,
        *,
        metadata: Mapping[str, object],
        canonical_signal_id: int | None = None,
    ) -> PriceObservation | None:
        """Return an exact immutable equity entry observation when supported."""

        del instr_id, as_of, metadata, canonical_signal_id
        return None

    def get_equity_exit_observation(
        self,
        instr_id: int,
        as_of: datetime,
        horizon: str,
        *,
        metadata: Mapping[str, object],
        canonical_signal_id: int | None = None,
    ) -> PriceObservation | None:
        """Return a near-horizon observation from the authorized equity stream."""

        del instr_id, as_of, horizon, metadata, canonical_signal_id
        return None


class SqlOHLCPriceProvider(PriceProvider):
    """Price provider pinned to the signal's exact source and timeframe.

    Feedback evidence must measure the same market-data stream that produced
    the signal. Missing provenance or a missing exact observation leaves the
    signal pending; another vendor or bar duration is never substituted.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        max_staleness: timedelta = DEFAULT_MAX_STALENESS,
    ) -> None:
        self._session_factory = session_factory
        self._max_staleness = max_staleness

    def _lookup_observation(
        self,
        instr_id: int,
        target_ts: datetime,
        *,
        source: str,
        timeframe: str,
        oldest_ts: datetime | None = None,
    ) -> PriceObservation | None:
        try:
            with self._session_factory() as s:
                target_utc = ensure_utc(target_ts)
                lower_bound_utc = (
                    ensure_utc(oldest_ts)
                    if oldest_ts is not None
                    else target_utc - self._max_staleness
                )
                asset_class = None
                if timeframe_interval(timeframe) >= timedelta(days=1):
                    asset_class = s.execute(
                        select(Instrument.asset_class).where(Instrument.instr_id == instr_id)
                    ).scalar_one_or_none()
                interval = fixed_duration_interval(
                    timeframe,
                    asset_class=asset_class,
                )
                stmt = (
                    select(InstrumentPrice)
                    .where(
                        InstrumentPrice.instr_id == instr_id,
                        InstrumentPrice.source == source,
                        InstrumentPrice.timeframe == timeframe,
                        InstrumentPrice.ts <= _as_database_timestamp(target_utc - interval),
                        InstrumentPrice.ts >= _as_database_timestamp(lower_bound_utc - interval),
                    )
                    .order_by(desc(InstrumentPrice.ts))
                    .limit(1)
                )
                rows = s.execute(stmt).scalars().all()
                observations: list[PriceObservation] = []
                for row in rows:
                    try:
                        interval = fixed_duration_interval(
                            row.timeframe,
                            asset_class=asset_class,
                        )
                    except UnsupportedSessionTimingError:
                        raise
                    except ValueError:
                        logger.warning(
                            "Skipping price_id=%s with unsupported timeframe=%s",
                            row.price_id,
                            row.timeframe,
                        )
                        continue
                    bar_open_ts = ensure_utc(row.ts)
                    bar_close_ts = bar_open_ts + interval
                    if not lower_bound_utc <= bar_close_ts <= target_utc:
                        continue
                    observations.append(
                        PriceObservation(
                            price_id=int(row.price_id),
                            price=float(row.close),
                            bar_open_ts=bar_open_ts,
                            bar_close_ts=bar_close_ts,
                            source=row.source,
                            timeframe=row.timeframe,
                            origin=PriceObservationOrigin.PRICES_TABLE,
                        )
                    )
                if not observations:
                    return None
                return max(
                    observations,
                    key=lambda item: (
                        item.bar_close_ts or datetime.min.replace(tzinfo=UTC),
                        item.bar_open_ts or datetime.min.replace(tzinfo=UTC),
                        item.price_id or 0,
                    ),
                )
        except UnsupportedSessionTimingError:
            raise
        except (EquityObservationAuthorityError, SQLAlchemyError, TypeError, ValueError):
            logger.warning(
                "Price lookup failed for instr_id=%s source=%s tf=%s",
                instr_id,
                source,
                timeframe,
                exc_info=True,
            )
            return None

    def _lookup_exact(
        self,
        instr_id: int,
        target_ts: datetime,
        price_source: str | None,
        price_timeframe: str | None,
        oldest_ts: datetime | None = None,
    ) -> PriceObservation | None:
        """Return only evidence matching complete signal provenance."""
        source = (price_source or "").strip()
        timeframe = (price_timeframe or "").strip()
        if not source or not timeframe:
            logger.warning(
                "Price lookup requires exact signal provenance for instr_id=%s",
                instr_id,
            )
            return None
        return self._lookup_observation(
            instr_id,
            target_ts,
            source=source,
            timeframe=timeframe,
            oldest_ts=oldest_ts,
        )

    def get_entry_observation(
        self,
        instr_id: int,
        as_of: datetime,
        *,
        price_source: str | None = None,
        price_timeframe: str | None = None,
    ) -> PriceObservation | None:
        return self._lookup_exact(instr_id, as_of, price_source, price_timeframe)

    def get_exit_observation(
        self,
        instr_id: int,
        as_of: datetime,
        horizon: str,
        *,
        price_source: str | None = None,
        price_timeframe: str | None = None,
    ) -> PriceObservation | None:
        delta = EvaluationHorizon(horizon).duration
        target_ts = evaluation_target(as_of, horizon)
        # Bound the exit to the second half of the horizon window
        # [as_of + delta/2, target]. This keeps the exit NEAR the horizon and
        # strictly AFTER entry — without it, sparse data lets the latest price
        # within max_staleness (5d) resolve to a near-entry bar, yielding a false
        # ~0% move / FLAT verdict that corrupts the consecutive-wrong tracker
        # (FB-1). No near-horizon price -> None -> the signal stays pending.
        oldest_ts = target_ts - delta / 2
        return self._lookup_exact(
            instr_id, target_ts, price_source, price_timeframe, oldest_ts=oldest_ts
        )

    def get_equity_entry_observation(
        self,
        instr_id: int,
        as_of: datetime,
        *,
        metadata: Mapping[str, object],
        canonical_signal_id: int | None = None,
    ) -> PriceObservation | None:
        """Load the signal's exact total-return-close observation and lineage."""

        try:
            context = EquityPriceEvidenceContext.from_metadata(metadata)
            cutoff = ensure_utc(as_of)
            with self._session_factory() as session:
                policy = self._equity_authority_policy(
                    session,
                    instr_id=instr_id,
                    context=context,
                    metadata=metadata,
                    canonical_signal_id=canonical_signal_id,
                )
                observation, lineage = validate_equity_observation_authority(
                    session,
                    observation_id=context.entry_observation_id,
                    expected_kind="price",
                    cutoff=cutoff,
                    provider_authority_policy=policy,
                    expected_instrument_id=instr_id,
                )
                if (
                    str(observation.content_sha256) != context.entry_observation_sha256
                    or ensure_utc(observation.event_at) > cutoff
                ):
                    _invalid("equity entry observation differs from signal lineage")
                self._require_equity_lineage(lineage, context=context)
                return self._equity_price_observation(
                    session,
                    observation=observation,
                    context=context,
                )
        except (EquityObservationAuthorityError, SQLAlchemyError, TypeError, ValueError):
            logger.warning(
                "Equity entry price lookup failed for instr_id=%s",
                instr_id,
                exc_info=True,
            )
            return None

    def get_equity_exit_observation(
        self,
        instr_id: int,
        as_of: datetime,
        horizon: str,
        *,
        metadata: Mapping[str, object],
        canonical_signal_id: int | None = None,
    ) -> PriceObservation | None:
        """Load a near-horizon total-return close from the same authority."""

        try:
            context = EquityPriceEvidenceContext.from_metadata(metadata)
            delta = EvaluationHorizon(horizon).duration
            target = evaluation_target(as_of, horizon)
            oldest = target - delta / 2
            with self._session_factory() as session:
                policy = self._equity_authority_policy(
                    session,
                    instr_id=instr_id,
                    context=context,
                    metadata=metadata,
                    canonical_signal_id=canonical_signal_id,
                )
                candidate_ids = session.scalars(
                    select(EquityObservation.observation_id)
                    .join(
                        EquitySourceLineage,
                        EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
                    )
                    .where(
                        EquityObservation.instr_id == instr_id,
                        EquityObservation.observation_kind == "price",
                        EquityObservation.disposition == "observed",
                        EquityObservation.event_at >= _as_database_timestamp(oldest),
                        EquityObservation.event_at <= _as_database_timestamp(target),
                        EquityObservation.available_at.is_not(None),
                        EquityObservation.available_at <= _as_database_timestamp(target),
                        EquitySourceLineage.provider == context.source,
                        EquitySourceLineage.entitlement_scope == context.entitlement_scope,
                        EquitySourceLineage.entitlement_owner_user_id.is_(None)
                        if context.entitlement_owner_user_id is None
                        else EquitySourceLineage.entitlement_owner_user_id
                        == context.entitlement_owner_user_id,
                    )
                    .order_by(
                        EquityObservation.event_at.desc(),
                        EquityObservation.revision.desc(),
                    )
                ).all()
                for candidate_id in candidate_ids:
                    try:
                        observation, lineage = validate_equity_observation_authority(
                            session,
                            observation_id=str(candidate_id),
                            expected_kind="price",
                            cutoff=target,
                            provider_authority_policy=policy,
                            expected_instrument_id=instr_id,
                        )
                        self._require_equity_lineage(lineage, context=context)
                        return self._equity_price_observation(
                            session,
                            observation=observation,
                            context=context,
                        )
                    except ValueError:
                        continue
                return None
        except Exception:
            logger.warning(
                "Equity exit price lookup failed for instr_id=%s",
                instr_id,
                exc_info=True,
            )
            return None

    @staticmethod
    def _equity_authority_policy(
        session: Session,
        *,
        instr_id: int,
        context: EquityPriceEvidenceContext,
        metadata: Mapping[str, object],
        canonical_signal_id: int | None,
    ) -> ProviderAuthorityPolicy:
        snapshot = session.get(EquityRankSnapshot, context.rank_snapshot_id)
        if (
            snapshot is None
            or str(snapshot.content_sha256) != context.rank_snapshot_id
            or str(snapshot.provider_authority_digest) != context.provider_authority_sha256
            or str(snapshot.data_use_scope) != context.data_use_scope.value
            or str(snapshot.completeness_status) != "complete"
        ):
            _invalid("equity feedback rank authority is missing or incompatible")
        SqlOHLCPriceProvider._require_equity_model_leg(
            session,
            instr_id=instr_id,
            snapshot=snapshot,
            context=context,
            metadata=metadata,
            canonical_signal_id=canonical_signal_id,
        )
        raw_policy = snapshot.provider_authority_policy
        if not isinstance(raw_policy, Mapping):
            _invalid("equity feedback provider authority policy is invalid")
        policy = ProviderAuthorityPolicy.from_payload(raw_policy)
        if (
            policy.digest != context.provider_authority_sha256
            or policy.data_use_scope is not context.data_use_scope
        ):
            _invalid("equity feedback provider authority digest differs")
        policy.require_authorized(
            provider=context.source,
            entitlement_scope=context.entitlement_scope,
            entitlement_owner_user_id=context.entitlement_owner_user_id,
        )
        return policy

    @staticmethod
    def _require_equity_model_leg(
        session: Session,
        *,
        instr_id: int,
        snapshot: EquityRankSnapshot,
        context: EquityPriceEvidenceContext,
        metadata: Mapping[str, object],
        canonical_signal_id: int | None,
    ) -> None:
        """Prove the signal is one immutable selected model decision.

        Rank eligibility is alpha state, not data authority. Mandatory exits
        can be rank-ineligible or absent from the current effective universe;
        their authority comes from the persisted model leg and, for a universe
        departure, its database-backed prior-target lineage.
        """

        if (
            isinstance(canonical_signal_id, bool)
            or not isinstance(canonical_signal_id, int)
            or canonical_signal_id <= 0
        ):
            _invalid("equity feedback requires a canonical signal identity")
        canonical = session.get(CanonicalSignal, canonical_signal_id)
        if canonical is None or int(canonical.instr_id) != instr_id:
            _invalid("equity feedback canonical signal is missing or incompatible")
        raw_canonical_metadata = canonical.signal_meta
        if not isinstance(raw_canonical_metadata, Mapping) or dict(raw_canonical_metadata) != dict(
            metadata
        ):
            _invalid("equity feedback metadata differs from the canonical signal")

        legs = session.scalars(
            select(ModelRebalanceLeg).where(
                ModelRebalanceLeg.external_signal_id == str(canonical.external_signal_id),
                ModelRebalanceLeg.instr_id == instr_id,
                ModelRebalanceLeg.rank_snapshot_id == context.rank_snapshot_id,
            )
        ).all()
        if len(legs) != 1:
            _invalid("equity feedback requires one exact persisted model leg")
        leg = legs[0]
        model = session.get(ModelRebalance, str(leg.rebalance_id))
        if (
            model is None
            or str(model.rank_snapshot_id) != context.rank_snapshot_id
            or str(model.strategy_id) != str(canonical.strategy_id)
            or int(model.strat_ver_id) != int(canonical.strat_ver_id or 0)
            or str(model.provider_authority_sha256) != context.provider_authority_sha256
            or str(model.data_use_scope) != context.data_use_scope.value
            or ensure_utc(model.decision_cutoff) != ensure_utc(canonical.ts)
            or str(snapshot.strategy_id) != str(canonical.strategy_id)
            or int(snapshot.strat_ver_id) != int(canonical.strat_ver_id or 0)
        ):
            _invalid("equity feedback model lineage is missing or incompatible")

        signal_snapshot = leg.signal_snapshot
        if (
            not isinstance(signal_snapshot, Mapping)
            or canonical_json_hash(dict(signal_snapshot)) != str(leg.signal_snapshot_sha256)
            or signal_snapshot.get("external_signal_id") != str(canonical.external_signal_id)
            or str(signal_snapshot.get("instrument_id")) != str(instr_id)
            or signal_snapshot.get("metadata") != dict(metadata)
            or str(leg.action) != str(canonical.action)
        ):
            _invalid("equity feedback model signal snapshot is incompatible")

        rank_row = session.get(
            EquityRankSnapshotRow,
            {"rank_snapshot_id": context.rank_snapshot_id, "instr_id": instr_id},
        )
        row_present = rank_row is not None
        if bool(leg.current_rank_row_present) is not row_present:
            _invalid("equity feedback model leg rank-row disposition differs")
        if rank_row is not None:
            if int(leg.current_rank_instr_id or 0) != instr_id or (
                str(leg.current_rank_factor_snapshot_id or "") or None
            ) != (str(rank_row.factor_snapshot_id or "") or None):
                _invalid("equity feedback model leg rank-row lineage differs")
            if (not bool(rank_row.eligible) or not bool(rank_row.strategy_eligible)) and (
                str(leg.phase) != "exit" or str(leg.action) != "flat"
            ):
                _invalid("rank-ineligible feedback authority requires a selected exit")
            return

        if (
            str(leg.phase) != "exit"
            or str(leg.action) != "flat"
            or str(leg.membership_change_reason) != "left_effective_universe"
            or leg.prior_model_rebalance_id is None
            or leg.prior_model_leg_id is None
        ):
            _invalid("absent-rank feedback authority requires a proven universe exit")
        prior_legs = session.scalars(
            select(ModelRebalanceLeg).where(
                ModelRebalanceLeg.rebalance_id == str(leg.prior_model_rebalance_id),
                ModelRebalanceLeg.leg_id == str(leg.prior_model_leg_id),
                ModelRebalanceLeg.instr_id == instr_id,
                ModelRebalanceLeg.factor_snapshot_id == str(leg.factor_snapshot_id),
            )
        ).all()
        if len(prior_legs) != 1 or (
            str(prior_legs[0].phase) not in {"entry", "hold", "reduce"}
            or str(prior_legs[0].action) not in {"long", "hold"}
        ):
            _invalid("universe-exit feedback authority lacks prior target lineage")

    @staticmethod
    def _require_equity_lineage(
        lineage: EquitySourceLineage,
        *,
        context: EquityPriceEvidenceContext,
    ) -> None:
        owner = (
            str(lineage.entitlement_owner_user_id)
            if lineage.entitlement_owner_user_id is not None
            else None
        )
        if (
            str(lineage.provider) != context.source
            or str(lineage.entitlement_scope) != context.entitlement_scope
            or owner != context.entitlement_owner_user_id
        ):
            _invalid("equity feedback lineage differs from signal authority")

    @staticmethod
    def _equity_price_observation(
        session: Session,
        *,
        observation: EquityObservation,
        context: EquityPriceEvidenceContext,
    ) -> PriceObservation:
        values = session.scalars(
            select(EquityObservationValue).where(
                EquityObservationValue.observation_id == str(observation.observation_id),
                EquityObservationValue.field_name == "total_return_close",
                EquityObservationValue.ordinal == 0,
            )
        ).all()
        if len(values) != 1 or values[0].value_type != "decimal" or values[0].decimal_value is None:
            _invalid("equity feedback total_return_close is absent or ambiguous")
        event_at = ensure_utc(observation.event_at)
        return PriceObservation(
            price_id=None,
            price=float(values[0].decimal_value),
            bar_open_ts=event_at,
            bar_close_ts=event_at,
            source=context.source,
            timeframe=context.timeframe,
            origin=PriceObservationOrigin.EQUITY_OBSERVATION,
            observation_id=str(observation.observation_id),
            observation_sha256=str(observation.content_sha256),
        )


class NullPriceProvider(PriceProvider):
    """Always returns None; use when no price store is available."""

    def get_entry_observation(
        self,
        _instr_id: int,
        _as_of: datetime,
        *,
        price_source: str | None = None,
        price_timeframe: str | None = None,
    ) -> PriceObservation | None:
        del price_source, price_timeframe
        return None

    def get_exit_observation(
        self,
        _instr_id: int,
        _as_of: datetime,
        _horizon: str,
        *,
        price_source: str | None = None,
        price_timeframe: str | None = None,
    ) -> PriceObservation | None:
        del price_source, price_timeframe
        return None
