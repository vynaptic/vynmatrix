from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from feedback_loop_engine.engine import FeedbackLoopEngine
from feedback_loop_engine.main import get_feedback_evaluation_horizons
from feedback_loop_engine.models import EvaluationHorizon
from feedback_loop_engine.price_provider import (
    PriceObservationOrigin,
    SqlOHLCPriceProvider,
)
from lib_application.db.models import (
    Base,
    CanonicalSignal,
    EquityFactorSnapshot,
    EquityObservation,
    EquityObservationValue,
    EquityRankSnapshot,
    EquityRankSnapshotRow,
    EquitySourceLineage,
    Instrument,
    InstrumentPrice,
    ModelRebalance,
    ModelRebalanceLeg,
    SignalPerformance,
    Strategy,
    StrategyVersion,
    User,
)
from lib_common.hashing import canonical_json_hash
from lib_data.dataset import UnsupportedSessionTimingError
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)


def _session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed_equity_feedback_evidence(
    session_local,
    *,
    rank_disposition: str = "selected",
):
    if rank_disposition not in {"selected", "ineligible_exit", "absent_universe_exit"}:
        raise ValueError("unsupported equity feedback rank disposition")
    owner = "personal-owner"
    strategy_id = "us_quality_compounder_v1"
    instrument_id = 501
    signal_ts = datetime(2024, 1, 31, 21, tzinfo=UTC)
    exit_ts = signal_ts + timedelta(days=1)
    entitlement_scope = "personal-research"
    authority = ProviderAuthorityPolicy(
        policy_version="feedback-personal-eodhd-v1",
        data_use_scope=DataUseScope.PAPER_FORWARD,
        rules=(
            ProviderAuthorityRule(
                provider="eodhd",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=(entitlement_scope,),
                entitlement_owner_user_id=owner,
            ),
        ),
    )
    lineage_id = "1" * 64
    entry_id = "2" * 64
    entry_sha = "3" * 64
    exit_id = "4" * 64
    exit_sha = "5" * 64
    factor_id = "6" * 64
    rank_id = "7" * 64
    rebalance_id = "f" * 64
    external_signal_id = f"feedback-equity-{rank_disposition}"
    is_selected = rank_disposition == "selected"
    row_present = rank_disposition != "absent_universe_exit"
    rank_complete = rank_disposition != "ineligible_exit"
    canonical_action = "long" if is_selected else "flat"
    snapshot_action = "long" if is_selected else "close"
    phase = "entry" if is_selected else "exit"
    exclusion_reason = (
        None
        if is_selected
        else "factor_evidence_incomplete"
        if rank_disposition == "ineligible_exit"
        else "left_effective_universe"
    )
    current_factor_id = factor_id if rank_complete and row_present else None
    metadata = {
        "price_store": "equity_observations",
        "price_source": "eodhd",
        "price_timeframe": "1d",
        "price_entitlement_scope": entitlement_scope,
        "price_entitlement_owner_user_id": owner,
        "price_observation_id": entry_id,
        "price_observation_sha256": entry_sha,
        "feedback_price_field": "total_return_close",
        "rank_snapshot_sha256": rank_id,
        "provider_authority_sha256": authority.digest,
        "data_use_scope": DataUseScope.PAPER_FORWARD.value,
        "strategy_rebalance_id": "0" * 64,
        "model_rebalance_sequence": 0,
        "model_rebalance_phase": phase,
        "factor_snapshot_id": factor_id,
        "current_rank_row_present": row_present,
        "current_rank_factor_snapshot_id": current_factor_id,
        "membership_change_reason": exclusion_reason,
    }

    def signal_snapshot(
        *,
        action: str,
        external_id: str,
        timestamp: datetime,
        signal_metadata: dict[str, object],
    ) -> dict[str, object]:
        return {
            "signal_id": f"domain-{external_id}",
            "strategy_id": strategy_id,
            "strategy_type": "indicator",
            "symbol": "AAPL",
            "action": action,
            "confidence": 0.9 if action == "long" else 0.0,
            "timestamp": timestamp.isoformat(),
            "horizon": "1d",
            "expected_return": 0.02 if action == "long" else None,
            "predicted_risk": 0.03 if action == "long" else None,
            "horizon_days": 1.0,
            "entry_price": 100.0,
            "stop_loss": None,
            "take_profit": None,
            "size_hint": 0.1 if action == "long" else None,
            "asset_class": "equity",
            "sector": "Technology",
            "industry": "Hardware",
            "index": "S&P 500",
            "instrument_id": str(instrument_id),
            "strategy_version": "1.0.0",
            "run_id": None,
            "source": "paper",
            "external_signal_id": external_id,
            "expires_at": None,
            "metadata": signal_metadata,
        }

    current_snapshot = signal_snapshot(
        action=snapshot_action,
        external_id=external_signal_id,
        timestamp=signal_ts,
        signal_metadata=metadata,
    )
    with session_local() as session:
        session.add_all(
            [
                User(
                    user_id=owner,
                    email="personal-owner@example.test",
                    base_ccy="USD",
                ),
                Strategy(
                    strategy_id=strategy_id,
                    strategy_name="USQualityCompounder",
                    asset_class="equity",
                ),
                Instrument(
                    instr_id=instrument_id,
                    asset_class="equity",
                    canonical="AAPL",
                    exchange="NASDAQ",
                    settlement_currency="USD",
                ),
            ]
        )
        session.flush()
        session.add(
            StrategyVersion(
                strat_ver_id=1,
                strategy_id=strategy_id,
                semver="1.0.0",
                param_schema={},
                default_params={},
            )
        )
        session.add(
            EquitySourceLineage(
                lineage_id=lineage_id,
                provider="eodhd",
                product="EOD Historical Data All World",
                endpoint="content-addressed-historical-snapshot-manifest",
                dataset_version="2024-02-02",
                tool_version="feedback-test-v1",
                source_identity="eodhd-personal-snapshot",
                source_revision="2024-02-02",
                retrieved_at=signal_ts - timedelta(days=1),
                timestamp_semantics={
                    "event_at": "official session close",
                    "available_at": "first actionable timestamp",
                },
                adjustment_policy="split-and-dividend-aware-v1",
                entitlement_scope=entitlement_scope,
                entitlement_owner_user_id=owner,
                missing_data_policy="fail-closed",
                content_sha256="8" * 64,
            )
        )
        session.add_all(
            [
                EquityObservation(
                    observation_id=entry_id,
                    lineage_id=lineage_id,
                    instr_id=instrument_id,
                    observation_kind="price",
                    source_record_identity="AAPL:2024-01-31",
                    event_at=signal_ts,
                    available_at=signal_ts,
                    revision=1,
                    disposition="observed",
                    content_sha256=entry_sha,
                ),
                EquityObservation(
                    observation_id=exit_id,
                    lineage_id=lineage_id,
                    instr_id=instrument_id,
                    observation_kind="price",
                    source_record_identity="AAPL:2024-02-01",
                    event_at=exit_ts,
                    available_at=exit_ts,
                    revision=1,
                    disposition="observed",
                    content_sha256=exit_sha,
                ),
                EquityObservationValue(
                    value_id="9" * 64,
                    observation_id=entry_id,
                    field_name="total_return_close",
                    ordinal=0,
                    value_type="decimal",
                    decimal_value=Decimal("100"),
                    unit="USD",
                ),
                EquityObservationValue(
                    value_id="a" * 64,
                    observation_id=exit_id,
                    field_name="total_return_close",
                    ordinal=0,
                    value_type="decimal",
                    decimal_value=Decimal("110"),
                    unit="USD",
                ),
                EquityFactorSnapshot(
                    factor_snapshot_id=factor_id,
                    strategy_id=strategy_id,
                    strat_ver_id=1,
                    instr_id=instrument_id,
                    effective_session=date(2024, 1, 31),
                    cutoff_at=signal_ts,
                    calculation_version="fixture-v1",
                    configuration_digest="b" * 64,
                    source_contract_registry_sha256="c" * 64,
                    peer_taxonomy_version="fixture-v1",
                    peer_group="sector:technology",
                    completeness_status=("complete" if rank_complete else "ineligible"),
                    expected_factor_count=1,
                    available_factor_count=1 if rank_complete else 0,
                    content_sha256=factor_id,
                ),
                EquityRankSnapshot(
                    rank_snapshot_id=rank_id,
                    strategy_id=strategy_id,
                    strat_ver_id=1,
                    effective_session=date(2024, 1, 31),
                    cutoff_at=signal_ts,
                    configuration_digest="b" * 64,
                    panel_revision_digest="d" * 64,
                    factor_content_digest="e" * 64,
                    data_use_scope=DataUseScope.PAPER_FORWARD.value,
                    provider_authority_digest=authority.digest,
                    provider_authority_policy=authority.to_payload(),
                    peer_taxonomy_version="fixture-v1",
                    completeness_status="complete",
                    expected_instrument_count=1 if row_present else 0,
                    included_instrument_count=1 if rank_complete and row_present else 0,
                    excluded_instrument_count=0 if rank_complete or not row_present else 1,
                    content_sha256=rank_id,
                ),
                CanonicalSignal(
                    signal_id=901,
                    strategy_id=strategy_id,
                    strat_ver_id=1,
                    instr_id=instrument_id,
                    action=canonical_action,
                    direction=canonical_action,
                    confidence=Decimal("0.9") if is_selected else Decimal("0"),
                    entry_price=Decimal("100"),
                    expected_return=Decimal("0.02") if is_selected else None,
                    predicted_risk=Decimal("0.03") if is_selected else None,
                    horizon_seconds=86_400,
                    signal_meta=metadata,
                    source_runner="indicator",
                    ts=signal_ts,
                    external_signal_id=external_signal_id,
                ),
                ModelRebalance(
                    rebalance_id=rebalance_id,
                    strategy_id=strategy_id,
                    strat_ver_id=1,
                    rank_snapshot_id=rank_id,
                    effective_session=date(2024, 1, 31),
                    decision_cutoff=signal_ts,
                    execute_not_before=signal_ts + timedelta(hours=17, minutes=30),
                    execution_session_sha256="1" * 64,
                    data_use_scope=DataUseScope.PAPER_FORWARD.value,
                    provider_authority_sha256=authority.digest,
                    configuration_sha256="b" * 64,
                    input_snapshot_sha256="2" * 64,
                    expected_leg_count=1,
                    intentional_cash_slots=0,
                    content_sha256="3" * 64,
                ),
            ]
        )
        if row_present:
            session.add(
                EquityRankSnapshotRow(
                    rank_snapshot_id=rank_id,
                    instr_id=instrument_id,
                    factor_snapshot_id=factor_id if rank_complete else None,
                    eligible=rank_complete,
                    strategy_eligible=is_selected,
                    row_ordinal=0,
                    rank_position=Decimal("1") if rank_complete else None,
                    composite_score=Decimal("0.8") if rank_complete else None,
                    target_allocation_hint=Decimal("0.1") if is_selected else None,
                    decision="selected" if is_selected else "exit",
                    incumbent=not is_selected,
                    exclusion_reason=exclusion_reason,
                )
            )
        prior_model_id: str | None = None
        prior_leg_id: str | None = None
        if rank_disposition == "absent_universe_exit":
            prior_rank_id = "0" * 64
            prior_model_id = "a" * 64
            prior_leg_id = "prior-selected-leg"
            prior_external_id = "feedback-equity-prior-selected"
            prior_ts = signal_ts - timedelta(days=33)
            prior_metadata = dict(metadata)
            prior_metadata.update(
                {
                    "rank_snapshot_sha256": prior_rank_id,
                    "model_rebalance_phase": "entry",
                    "current_rank_row_present": True,
                    "current_rank_factor_snapshot_id": factor_id,
                    "membership_change_reason": None,
                }
            )
            prior_snapshot = signal_snapshot(
                action="long",
                external_id=prior_external_id,
                timestamp=prior_ts,
                signal_metadata=prior_metadata,
            )
            session.add_all(
                [
                    EquityRankSnapshot(
                        rank_snapshot_id=prior_rank_id,
                        strategy_id=strategy_id,
                        strat_ver_id=1,
                        effective_session=date(2023, 12, 29),
                        cutoff_at=prior_ts,
                        configuration_digest="b" * 64,
                        panel_revision_digest="4" * 64,
                        factor_content_digest="5" * 64,
                        data_use_scope=DataUseScope.PAPER_FORWARD.value,
                        provider_authority_digest=authority.digest,
                        provider_authority_policy=authority.to_payload(),
                        peer_taxonomy_version="fixture-v1",
                        completeness_status="complete",
                        expected_instrument_count=1,
                        included_instrument_count=1,
                        excluded_instrument_count=0,
                        content_sha256=prior_rank_id,
                    ),
                    EquityRankSnapshotRow(
                        rank_snapshot_id=prior_rank_id,
                        instr_id=instrument_id,
                        factor_snapshot_id=factor_id,
                        eligible=True,
                        strategy_eligible=True,
                        row_ordinal=0,
                        rank_position=Decimal("1"),
                        composite_score=Decimal("0.8"),
                        target_allocation_hint=Decimal("0.1"),
                        decision="selected",
                        incumbent=False,
                        exclusion_reason=None,
                    ),
                    CanonicalSignal(
                        signal_id=900,
                        strategy_id=strategy_id,
                        strat_ver_id=1,
                        instr_id=instrument_id,
                        action="long",
                        direction="long",
                        confidence=Decimal("0.9"),
                        entry_price=Decimal("100"),
                        expected_return=Decimal("0.02"),
                        predicted_risk=Decimal("0.03"),
                        horizon_seconds=86_400,
                        signal_meta=prior_metadata,
                        source_runner="indicator",
                        ts=prior_ts,
                        external_signal_id=prior_external_id,
                    ),
                    ModelRebalance(
                        rebalance_id=prior_model_id,
                        strategy_id=strategy_id,
                        strat_ver_id=1,
                        rank_snapshot_id=prior_rank_id,
                        effective_session=date(2023, 12, 29),
                        decision_cutoff=prior_ts,
                        execute_not_before=prior_ts + timedelta(hours=17, minutes=30),
                        execution_session_sha256="6" * 64,
                        data_use_scope=DataUseScope.PAPER_FORWARD.value,
                        provider_authority_sha256=authority.digest,
                        configuration_sha256="b" * 64,
                        input_snapshot_sha256="7" * 64,
                        expected_leg_count=1,
                        intentional_cash_slots=0,
                        content_sha256="8" * 64,
                    ),
                    ModelRebalanceLeg(
                        rebalance_id=prior_model_id,
                        sequence=0,
                        leg_id=prior_leg_id,
                        leg_sha256="9" * 64,
                        signal_snapshot=prior_snapshot,
                        signal_snapshot_sha256=canonical_json_hash(prior_snapshot),
                        rank_snapshot_id=prior_rank_id,
                        factor_snapshot_id=factor_id,
                        current_rank_row_present=True,
                        current_rank_instr_id=instrument_id,
                        current_rank_factor_snapshot_id=factor_id,
                        prior_model_rebalance_id=None,
                        prior_model_leg_id=None,
                        membership_change_reason=None,
                        instr_id=instrument_id,
                        external_signal_id=prior_external_id,
                        phase="entry",
                        action="long",
                        rank_position=Decimal("1"),
                        allocation_hint=Decimal("0.1"),
                    ),
                ]
            )
        session.add(
            ModelRebalanceLeg(
                rebalance_id=rebalance_id,
                sequence=0,
                leg_id=external_signal_id,
                leg_sha256="c" * 64,
                signal_snapshot=current_snapshot,
                signal_snapshot_sha256=canonical_json_hash(current_snapshot),
                rank_snapshot_id=rank_id,
                factor_snapshot_id=factor_id,
                current_rank_row_present=row_present,
                current_rank_instr_id=instrument_id if row_present else None,
                current_rank_factor_snapshot_id=current_factor_id,
                prior_model_rebalance_id=prior_model_id,
                prior_model_leg_id=prior_leg_id,
                membership_change_reason=exclusion_reason,
                instr_id=instrument_id,
                external_signal_id=external_signal_id,
                phase=phase,
                action=canonical_action,
                rank_position=Decimal("1") if rank_complete and row_present else None,
                allocation_hint=Decimal("0.1") if is_selected else Decimal("0"),
            )
        )
        session.commit()
    return instrument_id, signal_ts, metadata


def test_price_provider_ignores_stale_prices() -> None:
    session_local = _session_factory()
    target_ts = datetime(2026, 3, 8, 12, 0)
    with session_local() as session:
        session.add(
            InstrumentPrice(
                price_id=1,
                instr_id=11,
                ts=target_ts - timedelta(days=10),
                timeframe="1m",
                close=44000.0,
                source="coinbase_live",
            )
        )
        session.commit()

    provider = SqlOHLCPriceProvider(session_local, max_staleness=timedelta(days=5))

    assert (
        provider.get_entry_observation(
            11,
            target_ts,
            price_source="coinbase_live",
            price_timeframe="1m",
        )
        is None
    )


def test_price_provider_prefers_recent_price_within_staleness_window() -> None:
    session_local = _session_factory()
    target_ts = datetime(2026, 3, 8, 12, 0)
    with session_local() as session:
        session.add_all(
            [
                InstrumentPrice(
                    price_id=1,
                    instr_id=11,
                    ts=target_ts - timedelta(days=2),
                    timeframe="1m",
                    close=45000.0,
                    source="coinbase_live",
                ),
                InstrumentPrice(
                    price_id=2,
                    instr_id=11,
                    ts=target_ts - timedelta(hours=1),
                    timeframe="1m",
                    close=45500.0,
                    source="coinbase_live",
                ),
            ]
        )
        session.commit()

    provider = SqlOHLCPriceProvider(session_local, max_staleness=timedelta(days=5))

    observation = provider.get_entry_observation(
        11,
        target_ts,
        price_source="coinbase_live",
        price_timeframe="1m",
    )
    assert observation is not None
    assert observation.price == 45500.0


def test_price_provider_rejects_different_source_instead_of_substituting() -> None:
    session_local = _session_factory()
    target_ts = datetime(2026, 3, 8, 12, 0)
    with session_local() as session:
        session.add(
            InstrumentPrice(
                price_id=1,
                instr_id=11,
                ts=target_ts - timedelta(minutes=1),
                timeframe="1m",
                close=45000.0,
                source="deribit",
            )
        )
        session.commit()

    provider = SqlOHLCPriceProvider(session_local)

    assert (
        provider.get_entry_observation(
            11,
            target_ts,
            price_source="coinbase_live",
            price_timeframe="1m",
        )
        is None
    )


def test_price_provider_rejects_different_timeframe_instead_of_substituting() -> None:
    session_local = _session_factory()
    target_ts = datetime(2026, 3, 8, 12, 0)
    with session_local() as session:
        session.add(
            InstrumentPrice(
                price_id=1,
                instr_id=11,
                ts=target_ts - timedelta(minutes=5),
                timeframe="5m",
                close=45000.0,
                source="coinbase_live",
            )
        )
        session.commit()

    provider = SqlOHLCPriceProvider(session_local)

    assert (
        provider.get_entry_observation(
            11,
            target_ts,
            price_source="coinbase_live",
            price_timeframe="1m",
        )
        is None
    )


@pytest.mark.parametrize(
    ("price_source", "price_timeframe"),
    [(None, "1m"), ("coinbase_live", None), (None, None)],
)
def test_price_provider_requires_complete_signal_provenance(
    price_source: str | None,
    price_timeframe: str | None,
) -> None:
    session_local = _session_factory()
    target_ts = datetime(2026, 3, 8, 12, 0)
    with session_local() as session:
        session.add(_price(1, target_ts - timedelta(minutes=1), 45000.0))
        session.commit()

    provider = SqlOHLCPriceProvider(session_local)

    assert (
        provider.get_entry_observation(
            11,
            target_ts,
            price_source=price_source,
            price_timeframe=price_timeframe,
        )
        is None
    )


def test_equity_feedback_uses_exact_owner_authorized_total_return_observations() -> None:
    session_local = _session_factory()
    instrument_id, signal_ts, metadata = _seed_equity_feedback_evidence(session_local)
    provider = SqlOHLCPriceProvider(session_local)

    entry = provider.get_equity_entry_observation(
        instrument_id,
        signal_ts,
        metadata=metadata,
        canonical_signal_id=901,
    )
    exit_observation = provider.get_equity_exit_observation(
        instrument_id,
        signal_ts,
        "1d",
        metadata=metadata,
        canonical_signal_id=901,
    )

    assert entry is not None
    assert exit_observation is not None
    assert entry.price == 100.0
    assert exit_observation.price == 110.0
    assert entry.origin is PriceObservationOrigin.EQUITY_OBSERVATION
    assert entry.observation_id == metadata["price_observation_id"]
    assert entry.observation_sha256 == metadata["price_observation_sha256"]
    assert exit_observation.origin is PriceObservationOrigin.EQUITY_OBSERVATION
    assert exit_observation.to_metadata()["observation_id"] == "4" * 64


def test_equity_feedback_fails_closed_on_entitlement_owner_drift() -> None:
    session_local = _session_factory()
    instrument_id, signal_ts, metadata = _seed_equity_feedback_evidence(session_local)
    tampered = dict(metadata)
    tampered["price_entitlement_owner_user_id"] = "another-owner"
    provider = SqlOHLCPriceProvider(session_local)

    assert (
        provider.get_equity_entry_observation(
            instrument_id,
            signal_ts,
            metadata=tampered,
            canonical_signal_id=901,
        )
        is None
    )


@pytest.mark.parametrize("rank_disposition", ["ineligible_exit", "absent_universe_exit"])
def test_equity_feedback_authorizes_selected_exit_without_eligible_current_rank(
    rank_disposition: str,
) -> None:
    session_local = _session_factory()
    instrument_id, signal_ts, metadata = _seed_equity_feedback_evidence(
        session_local,
        rank_disposition=rank_disposition,
    )
    provider = SqlOHLCPriceProvider(session_local)

    entry = provider.get_equity_entry_observation(
        instrument_id,
        signal_ts,
        metadata=metadata,
        canonical_signal_id=901,
    )
    exit_observation = provider.get_equity_exit_observation(
        instrument_id,
        signal_ts,
        "1d",
        metadata=metadata,
        canonical_signal_id=901,
    )

    assert entry is not None
    assert exit_observation is not None
    assert entry.price == 100.0
    assert exit_observation.price == 110.0


def test_equity_feedback_rejects_absent_rank_without_prior_target_lineage() -> None:
    session_local = _session_factory()
    instrument_id, signal_ts, metadata = _seed_equity_feedback_evidence(
        session_local,
        rank_disposition="absent_universe_exit",
    )
    with session_local() as session:
        current_leg = session.scalar(
            select(ModelRebalanceLeg).where(ModelRebalanceLeg.rebalance_id == "f" * 64)
        )
        assert current_leg is not None
        session.execute(
            ModelRebalanceLeg.__table__.update()
            .where(ModelRebalanceLeg.rebalance_id == "f" * 64)
            .values(prior_model_leg_id="missing-prior-leg")
        )
        session.commit()
    provider = SqlOHLCPriceProvider(session_local)

    assert (
        provider.get_equity_entry_observation(
            instrument_id,
            signal_ts,
            metadata=metadata,
            canonical_signal_id=901,
        )
        is None
    )


def test_feedback_cycle_persists_equity_observation_provenance() -> None:
    session_local = _session_factory()
    instrument_id, signal_ts, metadata = _seed_equity_feedback_evidence(session_local)
    engine = session_local.kw["bind"]
    feedback = FeedbackLoopEngine(
        engine=engine,
        price_provider=SqlOHLCPriceProvider(session_local),
    )

    result = feedback.run_evaluation_cycle(horizon=EvaluationHorizon.D1, limit=10)

    assert result["signals_evaluated"] == 1
    with session_local() as session:
        performance = session.scalar(
            select(SignalPerformance).where(SignalPerformance.signal_id == 901)
        )
    assert performance is not None
    assert float(performance.entry_price) == 100.0
    assert float(performance.exit_price) == 110.0
    assert performance.meta["entry_price_provenance"]["origin"] == "equity_observation"
    assert performance.meta["entry_price_provenance"]["observation_id"] == "2" * 64
    assert performance.meta["exit_price_provenance"]["observation_id"] == "4" * 64


def test_entry_lookup_uses_only_completed_bars() -> None:
    session_local = _session_factory()
    signal_ts = datetime(2026, 3, 8, 12, 0)
    with session_local() as session:
        session.add_all(
            [
                _price(1, signal_ts - timedelta(minutes=1), 45000.0),
                # ``prices.ts`` is the candle start. This row is not complete at
                # the signal timestamp and must never be used as its entry price.
                _price(2, signal_ts, 99999.0),
            ]
        )
        session.commit()

    provider = SqlOHLCPriceProvider(session_local)
    observation = provider.get_entry_observation(
        11,
        signal_ts,
        price_source="coinbase_live",
        price_timeframe="1m",
    )

    assert observation is not None
    assert observation.price_id == 1
    assert observation.price == 45000.0
    assert observation.bar_open_ts == (signal_ts - timedelta(minutes=1)).replace(tzinfo=UTC)
    assert observation.bar_close_ts == signal_ts.replace(tzinfo=UTC)
    assert observation.source == "coinbase_live"
    assert observation.timeframe == "1m"
    assert observation.origin is PriceObservationOrigin.PRICES_TABLE


def _price(price_id: int, ts: datetime, close: float) -> InstrumentPrice:
    return InstrumentPrice(
        price_id=price_id,
        instr_id=11,
        ts=ts,
        timeframe="1m",
        close=close,
        source="coinbase_live",
    )


def test_exit_price_resolves_near_horizon_bar() -> None:
    # FB-1: a bar in the second half of the horizon window is the exit.
    session_local = _session_factory()
    as_of = datetime(2026, 3, 8, 12, 0)  # target = as_of + 1d
    with session_local() as session:
        session.add(_price(1, as_of + timedelta(hours=23), 46000.0))
        session.commit()

    provider = SqlOHLCPriceProvider(session_local, max_staleness=timedelta(days=5))
    observation = provider.get_exit_observation(
        11, as_of, "1d", price_source="coinbase_live", price_timeframe="1m"
    )
    assert observation is not None
    assert observation.price == 46000.0


def test_h1_exit_uses_bar_closing_at_exact_target_without_one_bar_lookahead() -> None:
    session_local = _session_factory()
    signal_ts = datetime(2026, 3, 8, 12, 0)
    target_ts = signal_ts + timedelta(hours=1)
    with session_local() as session:
        session.add_all(
            [
                InstrumentPrice(
                    price_id=1,
                    instr_id=11,
                    ts=signal_ts,
                    timeframe="1h",
                    close=46000.0,
                    source="coinbase_live",
                ),
                # Opens at the exact target but closes one hour later. Selecting
                # this row would introduce one full bar of look-ahead.
                InstrumentPrice(
                    price_id=2,
                    instr_id=11,
                    ts=target_ts,
                    timeframe="1h",
                    close=99999.0,
                    source="coinbase_live",
                ),
            ]
        )
        session.commit()

    provider = SqlOHLCPriceProvider(session_local)
    observation = provider.get_exit_observation(
        11,
        signal_ts,
        "1h",
        price_source="coinbase_live",
        price_timeframe="1h",
    )

    assert observation is not None
    assert observation.price_id == 1
    assert observation.price == 46000.0
    assert observation.bar_close_ts == target_ts.replace(tzinfo=UTC)


def test_session_daily_price_requires_authoritative_close_metadata() -> None:
    session_local = _session_factory()
    bar_open = datetime(2026, 3, 6, 14, 30)
    with session_local() as session:
        session.add(
            Instrument(
                instr_id=12,
                asset_class="equity",
                canonical="SPY",
                exchange="NYSE",
                settlement_currency="USD",
            )
        )
        session.add(
            InstrumentPrice(
                price_id=1,
                instr_id=12,
                ts=bar_open,
                timeframe="1d",
                close=575.0,
                source="equity_provider",
            )
        )
        session.commit()

    provider = SqlOHLCPriceProvider(session_local)
    with pytest.raises(UnsupportedSessionTimingError, match="authoritative bar open/close"):
        provider.get_entry_observation(
            12,
            bar_open + timedelta(days=1),
            price_source="equity_provider",
            price_timeframe="1d",
        )


def test_exit_price_rejects_near_entry_bar_far_from_horizon() -> None:
    # FB-1: with only a near-entry bar (outside [as_of+12h, target]), the exit
    # must be None — not the stale near-entry price that yields a false ~0% move.
    session_local = _session_factory()
    as_of = datetime(2026, 3, 8, 12, 0)
    with session_local() as session:
        session.add(_price(1, as_of + timedelta(hours=1), 45000.0))  # near entry
        session.commit()

    provider = SqlOHLCPriceProvider(session_local, max_staleness=timedelta(days=5))
    observation = provider.get_exit_observation(
        11, as_of, "1d", price_source="coinbase_live", price_timeframe="1m"
    )
    assert observation is None


def test_feedback_evaluation_horizons_defaults_to_all(monkeypatch) -> None:
    monkeypatch.delenv("FEEDBACK_EVALUATION_HORIZONS", raising=False)

    assert get_feedback_evaluation_horizons() == list(EvaluationHorizon)


def test_feedback_evaluation_horizons_parses_valid_values(monkeypatch) -> None:
    monkeypatch.setenv("FEEDBACK_EVALUATION_HORIZONS", "1h,1d,1w")

    assert get_feedback_evaluation_horizons() == [
        EvaluationHorizon.H1,
        EvaluationHorizon.D1,
        EvaluationHorizon.W1,
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "1h,invalid,1w",
        "1h,,1w",
        "1h,1h",
    ],
)
def test_feedback_evaluation_horizons_rejects_malformed_config(
    monkeypatch,
    raw: str,
) -> None:
    monkeypatch.setenv("FEEDBACK_EVALUATION_HORIZONS", raw)

    with pytest.raises(ValueError, match="FEEDBACK_EVALUATION_HORIZONS"):
        get_feedback_evaluation_horizons()
