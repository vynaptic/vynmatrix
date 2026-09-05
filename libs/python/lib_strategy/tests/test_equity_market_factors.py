"""Contract tests for exact-session US-equity market-factor arithmetic."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest

from lib_common.hashing import canonical_json_hash
from lib_strategy.equity_market_factors import (
    DailyEquityMarketObservation,
    EquityMarketFactorInput,
    EquityMarketFactorPolicy,
    EquityMarketInputError,
    PointInTimeEquitySecurity,
    StructuralBreadthExclusion,
    calculate_equity_market_factors,
    conservative_split_coordinate_notional,
    validate_split_price_contract,
)
from lib_strategy.panels import OfficialSessionCutoff, SessionAuthority

_COST_CONTEXT = canonical_json_hash(
    {
        "schema": "reference-order-cost-policy-v1",
        "reference_order_notional_usd": "1000000",
        "impact_model": "licensed-point-in-time-estimate",
    }
)
_ADJUSTMENT_POLICY = "in-house-total-return-and-split-adjusted-ohlc-v1"


def _policy() -> EquityMarketFactorPolicy:
    return EquityMarketFactorPolicy(
        round_trip_commission_bps=1.0,
        cost_context_sha256=_COST_CONTEXT,
        required_adjustment_policy=_ADJUSTMENT_POLICY,
    )


def test_split_coordinate_notional_uses_explicit_integer_haircut() -> None:
    assert conservative_split_coordinate_notional(100.0, 1_000.0) == 99_900.0
    assert conservative_split_coordinate_notional(100.0, 1.0) == 0.0


def test_split_price_contract_rejects_mismatched_factor() -> None:
    with pytest.raises(EquityMarketInputError, match="cumulative split-factor contract"):
        validate_split_price_contract(
            raw_close=100.0,
            split_adjusted_close=49.0,
            split_adjustment_factor=0.5,
        )


def _sessions(count: int = 274) -> tuple[OfficialSessionCutoff, ...]:
    start = date(2023, 1, 2)
    result: list[OfficialSessionCutoff] = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            opens_at = datetime.combine(current, time(14, 30), tzinfo=UTC)
            closes_at = datetime.combine(current, time(21), tzinfo=UTC)
            result.append(
                OfficialSessionCutoff(
                    mic="XNYS",
                    session_date=current,
                    opens_at=opens_at,
                    closes_at=closes_at,
                    authority=SessionAuthority.OFFICIAL_EXCHANGE,
                    source_identity="fixture:official-session-contract",
                    content_sha256=canonical_json_hash(
                        {
                            "session": current.isoformat(),
                            "opens_at": opens_at.isoformat(),
                            "closes_at": closes_at.isoformat(),
                        }
                    ),
                )
            )
        current += timedelta(days=1)
    return tuple(result)


def _security(
    instrument_id: int,
    symbol: str,
    *,
    benchmark: bool = False,
) -> PointInTimeEquitySecurity:
    observation_id = canonical_json_hash(
        {"kind": "security_identity", "instrument_id": instrument_id}
    )
    return PointInTimeEquitySecurity(
        instrument_id=instrument_id,
        security_id=f"security:{symbol}",
        issuer_id=f"issuer:{symbol}",
        symbol=symbol,
        sector="Benchmark" if benchmark else "Information Technology",
        industry="Broad Market" if benchmark else f"Industry {instrument_id}",
        quote_currency="USD",
        tradable=not benchmark,
        observation_id=observation_id,
        observation_sha256=canonical_json_hash({"observation_id": observation_id, "revision": 1}),
    )


def _history(
    security: PointInTimeEquitySecurity,
    sessions: tuple[OfficialSessionCutoff, ...],
    *,
    daily_return: float,
    costs: bool = True,
    bad_gap_index: int | None = None,
    action_clear: bool = True,
    alternating: bool = False,
) -> tuple[DailyEquityMarketObservation, ...]:
    result: list[DailyEquityMarketObservation] = []
    total_return_close = 100.0
    split_close = 100.0
    for index, session in enumerate(sessions):
        session_return = daily_return
        if alternating:
            session_return = daily_return if index % 2 == 0 else -daily_return / 2.0
        prior_split_close = split_close
        total_return_close *= 1.0 + session_return
        split_close *= 1.0 + session_return
        split_open = prior_split_close * (0.80 if index == bad_gap_index else 1.0)
        observation_id = canonical_json_hash(
            {
                "kind": "daily_market",
                "instrument_id": security.instrument_id,
                "session": session.session_date.isoformat(),
                "revision": 1,
            }
        )
        result.append(
            DailyEquityMarketObservation(
                instrument_id=security.instrument_id,
                symbol=security.symbol,
                session_date=session.session_date,
                observed_at=session.closes_at,
                available_at=session.closes_at,
                observation_id=observation_id,
                observation_sha256=canonical_json_hash(
                    {"observation_id": observation_id, "content_revision": 1}
                ),
                provider="eodhd",
                timeframe="1d",
                entitlement_scope="historical_validation_only",
                entitlement_owner_user_id=None,
                total_return_close=total_return_close,
                split_adjusted_open=split_open,
                split_adjusted_close=split_close,
                split_adjusted_volume=1_000_000.0,
                split_adjustment_factor=1.0,
                raw_close=split_close,
                round_trip_spread_bps=10.0 if costs else None,
                one_way_nonspread_cost_bps=2.0 if costs else None,
                cost_context_sha256=_COST_CONTEXT if costs else None,
                corporate_action_clear=(action_clear or index != len(sessions) - 1),
            )
        )
    return tuple(result)


def _input(*, costs: bool = True) -> EquityMarketFactorInput:
    sessions = _sessions()
    rising = _security(1, "AAA")
    falling = _security(2, "BBB")
    benchmark = _security(3, "SPY", benchmark=True)
    prices = (
        *_history(
            rising,
            sessions,
            daily_return=0.001,
            costs=costs,
            bad_gap_index=len(sessions) - 10,
            action_clear=False,
        ),
        *_history(falling, sessions, daily_return=-0.0005, costs=costs),
        *_history(
            benchmark,
            sessions,
            daily_return=0.001,
            costs=costs,
            alternating=True,
        ),
    )
    return EquityMarketFactorInput(
        effective_session=sessions[-1].session_date,
        cutoff=sessions[-1].closes_at,
        official_sessions=sessions,
        members=(rising, falling),
        benchmark=benchmark,
        prices=prices,
    )


def _structural_exclusion(
    security: PointInTimeEquitySecurity,
    sessions: tuple[OfficialSessionCutoff, ...],
    *,
    missing_sessions: int = 1,
) -> StructuralBreadthExclusion:
    observed_sessions = sessions[missing_sessions:]
    observations = _history(
        security,
        observed_sessions,
        daily_return=0.001,
    )
    evidence_sha256 = canonical_json_hash(
        {"provider": "licensed-reference-provider", "symbol": security.symbol}
    )
    identity_binding = canonical_json_hash(
        {
            "evidence_sha256": evidence_sha256,
            "security_id": security.security_id,
        }
    )
    return StructuralBreadthExclusion(
        security=security,
        reason_code="listing_history_warmup",
        listing_date=observed_sessions[0].session_date,
        listing_session=observed_sessions[0].session_date,
        observed_history_sessions=len(observations),
        required_history_sessions=len(sessions),
        missing_session_dates=tuple(item.session_date for item in sessions[:missing_sessions]),
        observed_session_dates=tuple(item.session_date for item in observed_sessions),
        source_observation_ids=tuple(item.observation_id for item in observations),
        source_observation_sha256s=tuple(item.observation_sha256 for item in observations),
        membership_interval_id=canonical_json_hash({"membership": security.security_id}),
        evidence_id=canonical_json_hash({"identity_binding": identity_binding, "revision": 1}),
        evidence_provider="licensed-reference-provider",
        evidence_provider_symbol=f"{security.symbol}.US",
        evidence_artifact_role="security_reference_response",
        evidence_source_ref=f"/securities/{security.symbol}",
        evidence_retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        evidence_sha256=evidence_sha256,
        identity_binding=identity_binding,
    )


def _input_with_structural_exclusions(
    exclusion_count: int,
) -> EquityMarketFactorInput:
    sessions = _sessions()
    total_members = 100
    observed_count = total_members - exclusion_count
    members = tuple(_security(index, f"S{index:03d}") for index in range(1, observed_count + 1))
    excluded_securities = tuple(
        _security(index, f"S{index:03d}") for index in range(observed_count + 1, total_members + 1)
    )
    benchmark = _security(1_000, "SPY", benchmark=True)
    prices = tuple(
        observation
        for security in members
        for observation in _history(security, sessions, daily_return=0.001)
    ) + _history(benchmark, sessions, daily_return=0.001, alternating=True)
    return EquityMarketFactorInput(
        effective_session=sessions[-1].session_date,
        cutoff=sessions[-1].closes_at,
        official_sessions=sessions,
        members=members,
        benchmark=benchmark,
        prices=prices,
        structural_breadth_exclusions=tuple(
            _structural_exclusion(security, sessions) for security in excluded_securities
        ),
    )


def test_exact_registered_momentum_cost_risk_and_regime_arithmetic() -> None:
    panel = _input()
    snapshot = calculate_equity_market_factors(panel, _policy())
    by_symbol = {item.security.symbol: item for item in snapshot.instruments}

    rising = by_symbol["AAA"]
    assert rising.momentum_6_1 == pytest.approx((1.001**126) - 1.0)
    assert rising.momentum_12_1 == pytest.approx((1.001**252) - 1.0)
    assert rising.price_momentum == pytest.approx(((1.001**126) - 1.0 + (1.001**252) - 1.0) / 2.0)
    assert rising.absolute_momentum == rising.price_momentum
    assert rising.expected_round_trip_cost_bps == pytest.approx(15.0)
    assert rising.worst_gap_return == pytest.approx(-0.20)
    assert rising.downside_volatility == pytest.approx(0.0)
    assert rising.corporate_action_clear is False
    assert len(rising.source_observation_ids) == _policy().required_history_sessions
    assert snapshot.regime.benchmark_trend_score == 1.0
    assert snapshot.regime.breadth_score == pytest.approx(0.5)
    assert snapshot.regime.realized_volatility > 0.0


def test_missing_explicit_costs_remain_missing_and_are_not_imputed() -> None:
    snapshot = calculate_equity_market_factors(_input(costs=False), _policy())
    assert all(item.expected_round_trip_cost_bps is None for item in snapshot.instruments)


def test_calculation_is_order_invariant_and_content_addressed() -> None:
    panel = _input()
    expected = calculate_equity_market_factors(panel, _policy())
    reordered = replace(
        panel,
        official_sessions=tuple(reversed(panel.official_sessions)),
        members=tuple(reversed(panel.members)),
        prices=tuple(reversed(panel.prices)),
    )
    actual = calculate_equity_market_factors(reordered, _policy())
    assert actual == expected
    assert actual.content_sha256 == expected.content_sha256


def test_listing_warmup_uses_full_denominator_and_conservative_bounds() -> None:
    panel = _input_with_structural_exclusions(1)
    snapshot = calculate_equity_market_factors(panel, _policy())
    regime = snapshot.regime

    assert len(snapshot.instruments) == 99
    assert regime.breadth_positive_members == 99
    assert regime.breadth_observed_members == 99
    assert regime.breadth_structural_excluded_members == 1
    assert regime.breadth_total_members == 100
    assert regime.breadth_score == pytest.approx(0.99)
    assert regime.breadth_upper_bound == pytest.approx(1.0)
    assert regime.breadth_uncertainty == pytest.approx(0.01)
    assert regime.breadth_coverage_ratio == pytest.approx(0.99)
    assert regime.structural_breadth_exclusions[0].observed_history_sessions == 273


def test_structural_warmup_is_bounded_and_post_listing_gaps_fail_closed() -> None:
    with pytest.raises(EquityMarketInputError, match="fraction exceeds"):
        calculate_equity_market_factors(_input_with_structural_exclusions(2), _policy())

    sessions = _sessions()
    exclusion = _structural_exclusion(_security(101, "NEW"), sessions)
    with pytest.raises(EquityMarketInputError, match="post-listing gap"):
        replace(
            exclusion,
            missing_session_dates=(
                *exclusion.missing_session_dates,
                exclusion.observed_session_dates[10],
            ),
            observed_history_sessions=exclusion.observed_history_sessions - 1,
            observed_session_dates=(
                *exclusion.observed_session_dates[:10],
                *exclusion.observed_session_dates[11:],
            ),
            source_observation_ids=exclusion.source_observation_ids[:-1],
            source_observation_sha256s=exclusion.source_observation_sha256s[:-1],
        )


def test_274_session_member_cannot_be_relabelled_as_structural_warmup() -> None:
    sessions = _sessions()
    with pytest.raises(EquityMarketInputError, match="complete history"):
        _structural_exclusion(
            _security(101, "FULL"),
            sessions,
            missing_sessions=0,
        )


def test_missing_exact_session_and_future_availability_fail_closed() -> None:
    panel = _input()
    first_member = panel.members[0]
    missing = replace(
        panel,
        prices=tuple(
            item
            for item in panel.prices
            if not (
                item.instrument_id == first_member.instrument_id
                and item.session_date == panel.official_sessions[10].session_date
            )
        ),
    )
    with pytest.raises(EquityMarketInputError, match="missing official sessions"):
        calculate_equity_market_factors(missing, _policy())

    latest = next(
        item
        for item in panel.prices
        if item.instrument_id == first_member.instrument_id
        and item.session_date == panel.effective_session
    )
    future = replace(latest, available_at=panel.cutoff + timedelta(seconds=1))
    future_panel = replace(
        panel,
        prices=tuple(future if item is latest else item for item in panel.prices),
    )
    with pytest.raises(EquityMarketInputError, match="unavailable at the decision cutoff"):
        calculate_equity_market_factors(future_panel, _policy())


def test_cost_context_and_nonfinite_values_are_rejected() -> None:
    panel = _input()
    observation = panel.prices[-1]
    wrong_context = replace(
        observation,
        cost_context_sha256=canonical_json_hash({"wrong": "reference order"}),
    )
    divergent = replace(
        panel,
        prices=tuple(wrong_context if item is observation else item for item in panel.prices),
    )
    with pytest.raises(EquityMarketInputError, match="reference-order policy"):
        calculate_equity_market_factors(divergent, _policy())

    with pytest.raises(EquityMarketInputError, match="total_return_close must be finite"):
        replace(observation, total_return_close=math.nan)
