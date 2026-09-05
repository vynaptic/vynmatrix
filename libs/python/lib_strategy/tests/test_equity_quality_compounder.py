"""Tests for the concentrated US quality-compounder selection policy."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from lib_strategy.cross_sectional import (
    CrossSectionalEntity,
    CrossSectionalRanker,
    FactorObservation,
    FactorSpec,
)
from lib_strategy.equity_quality_compounder import (
    MarketCapBucket,
    QualityCompounderGroupLevel,
    QualityCompounderGroupMember,
    QualityCompounderHolding,
    QualityCompounderPolicy,
    QualityCompounderSecurity,
    calculate_quality_compounder_group_scores,
    quality_compounder_configuration_sha256,
    quality_compounder_entries_allowed,
    quality_compounder_market_cap_bucket,
    quality_compounder_market_ineligibility_reason,
    select_quality_compounders,
)

_CUTOFF = datetime(2026, 6, 30, 22, 0, tzinfo=UTC)


def _snapshot(
    values: tuple[float, ...],
    *,
    policy: QualityCompounderPolicy,
):
    entities = tuple(
        CrossSectionalEntity(
            entity_id=f"security-{index:02d}",
            symbol=f"S{index:02d}",
            peer_groups=("industry:all", "sector:all"),
        )
        for index in range(len(values))
    )
    observations = tuple(
        FactorObservation(
            entity_id=entity.entity_id,
            factor_name=spec.name,
            raw_value=values[index],
            source_observation_ids=(f"source:{entity.entity_id}:{spec.name}",),
        )
        for index, entity in enumerate(entities)
        for spec in policy.factor_specs
        if spec.enabled
    )
    return CrossSectionalRanker(minimum_peer_count=2).rank(
        cutoff=_CUTOFF,
        entities=entities,
        factor_specs=policy.factor_specs,
        observations=observations,
    )


def _securities(
    count: int,
    *,
    bucket: MarketCapBucket = MarketCapBucket.LARGE,
    common_sector: bool = False,
    costs: dict[int, float] | None = None,
) -> tuple[QualityCompounderSecurity, ...]:
    costs = costs or {}
    return tuple(
        QualityCompounderSecurity(
            entity_id=f"security-{index:02d}",
            instrument_id=index + 1,
            symbol=f"S{index:02d}",
            factor_snapshot_id=f"{index + 1:064x}",
            sector="sector:shared" if common_sector else f"sector:{index:02d}",
            industry=f"industry:{index:02d}",
            market_cap_bucket=bucket,
            sector_score=1.0,
            industry_score=1.0,
            reference_price=100.0 + index,
            expected_round_trip_cost_bps=costs.get(index, 10.0),
        )
        for index in range(count)
    )


def _permissive_policy(**overrides: object) -> QualityCompounderPolicy:
    values: dict[str, object] = {
        "entry_score": -3.0,
        "entry_percentile": 1.0,
        "hold_score": -3.0,
        "hold_percentile": 1.0,
        "quality_floor": -3.0,
        "growth_floor": -3.0,
        "entry_group_floor": -3.0,
        "hold_group_floor": -3.0,
        "max_sector_weight": 1.0,
        "max_industry_weight": 1.0,
    }
    values.update(overrides)
    return QualityCompounderPolicy(**values)


def test_default_policy_registers_only_materializable_factors() -> None:
    policy = QualityCompounderPolicy()

    assert {spec.name: spec.weight for spec in policy.factor_specs} == {
        "fundamental_growth": 0.30,
        "momentum": 0.15,
        "quality": 0.35,
        "valuation": 0.20,
    }
    assert all(spec.enabled for spec in policy.factor_specs)

    with pytest.raises(ValueError, match="registered"):
        QualityCompounderPolicy(factor_specs=(FactorSpec("quality", 1.0),))
    with pytest.raises(ValueError, match=r"\[1, 25\]"):
        QualityCompounderPolicy(target_holdings=26)


def test_configuration_identity_is_pinned_to_the_current_semantic_version() -> None:
    assert quality_compounder_configuration_sha256("0.2.0") == (
        "950fa8d190f9793224289c74c54e946eaf305d369bf4746194666d134f00ff74"
    )
    with pytest.raises(ValueError, match=r"supports only strategy version 0\.2\.0"):
        quality_compounder_configuration_sha256("0.1.0")


def test_market_evidence_derivations_are_exact_and_fail_closed() -> None:
    assert quality_compounder_market_cap_bucket(1_999_999_999.0) is MarketCapBucket.SMALL
    assert quality_compounder_market_cap_bucket(2_000_000_000.0) is MarketCapBucket.MID
    assert quality_compounder_market_cap_bucket(10_000_000_000.0) is MarketCapBucket.LARGE

    eligible = {
        "quote_currency": "USD",
        "tradable": True,
        "market_cap_usd": 12_000_000_000.0,
        "reference_price": 25.0,
        "median_dollar_volume": 75_000_000.0,
        "expected_round_trip_cost_bps": 20.0,
        "worst_gap_return": -0.10,
        "downside_volatility": 0.30,
        "corporate_action_clear": True,
        "data_quality_passed": True,
    }
    assert quality_compounder_market_ineligibility_reason(**eligible) is None
    assert (
        quality_compounder_market_ineligibility_reason(
            **{**eligible, "median_dollar_volume": 49_999_999.0}
        )
        == "liquidity_below_minimum"
    )
    assert (
        quality_compounder_market_ineligibility_reason(
            **{**eligible, "expected_round_trip_cost_bps": None}
        )
        == "transaction_cost_unavailable"
    )

    with pytest.raises(ValueError, match="below the eligible minimum"):
        quality_compounder_market_cap_bucket(299_999_999.0)


def test_market_entry_gate_uses_lower_bound_coverage_and_volatility() -> None:
    assert quality_compounder_entries_allowed(
        benchmark_trend_score=1.0,
        breadth_score=0.50,
        breadth_coverage_ratio=0.95,
        realized_volatility=0.35,
    )
    assert not quality_compounder_entries_allowed(
        benchmark_trend_score=1.0,
        breadth_score=0.49,
        breadth_coverage_ratio=1.0,
        realized_volatility=0.20,
    )


def test_group_scores_use_registered_formula_and_drop_thin_groups() -> None:
    members = tuple(
        QualityCompounderGroupMember(
            entity_id=f"security-{group}-{member}",
            sector=f"sector-{group}",
            industry=f"industry-{group}",
            price_momentum=float(group),
            trend_return=1.0 if group >= 3 else -1.0,
            fundamental_growth=float(group),
        )
        for group in range(5)
        for member in range(5)
    )
    sector_scores = calculate_quality_compounder_group_scores(
        members,
        level=QualityCompounderGroupLevel.SECTOR,
    )

    assert tuple(sector_scores) == tuple(f"sector-{group}" for group in range(5))
    assert sector_scores["sector-4"] > sector_scores["sector-3"] > 0.0
    assert sector_scores["sector-0"] < sector_scores["sector-1"] < 0.0
    assert (
        calculate_quality_compounder_group_scores(
            members[:4],
            level=QualityCompounderGroupLevel.INDUSTRY,
        )
        == {}
    )


def test_default_entry_gate_selects_only_top_decile_and_leaves_cash() -> None:
    policy = QualityCompounderPolicy()
    snapshot = _snapshot(tuple(float(20 - index) for index in range(20)), policy=policy)

    selection = select_quality_compounders(
        snapshot=snapshot,
        securities=_securities(20),
        entries_allowed=True,
        policy=policy,
    )

    assert [item.security.symbol for item in selection.positions] == ["S00", "S01"]
    assert selection.intentional_cash_slots == 13
    assert selection.target_gross_exposure == pytest.approx(2.0 / 15.0)


def test_closed_market_gate_keeps_qualified_incumbent_without_new_entries() -> None:
    policy = QualityCompounderPolicy()
    snapshot = _snapshot(tuple(float(20 - index) for index in range(20)), policy=policy)
    incumbent = QualityCompounderHolding(
        "security-02", 3, "S02", f"{3:064x}", "sector:02", "industry:02"
    )

    selection = select_quality_compounders(
        snapshot=snapshot,
        securities=_securities(20),
        incumbents=(incumbent,),
        entries_allowed=False,
        policy=policy,
    )

    assert [(item.security.symbol, item.incumbent) for item in selection.positions] == [
        ("S02", True)
    ]
    assert not selection.exits
    assert dict(selection.exclusion_reasons)["security-00"] == "market_entry_gate_closed"


def test_challenger_must_clear_score_gap_before_replacing_incumbent() -> None:
    conservative = _permissive_policy(target_holdings=1, challenger_score_gap=0.35)
    values = tuple(float(10 - index) for index in range(10))
    snapshot = _snapshot(values, policy=conservative)
    incumbent = QualityCompounderHolding(
        "security-01", 2, "S01", f"{2:064x}", "sector:01", "industry:01"
    )

    retained = select_quality_compounders(
        snapshot=snapshot,
        securities=_securities(10),
        incumbents=(incumbent,),
        entries_allowed=True,
        policy=conservative,
    )

    assert [item.security.symbol for item in retained.positions] == ["S01"]
    assert dict(retained.exclusion_reasons)["security-00"] == "turnover_buffer"

    permissive = _permissive_policy(target_holdings=1, challenger_score_gap=0.01)
    replaced = select_quality_compounders(
        snapshot=_snapshot(values, policy=permissive),
        securities=_securities(10),
        incumbents=(incumbent,),
        entries_allowed=True,
        policy=permissive,
    )

    assert [item.security.symbol for item in replaced.positions] == ["S00"]
    assert [(item.holding.symbol, item.reason) for item in replaced.exits] == [
        ("S01", "replaced_by_superior_challenger")
    ]


def test_sector_cap_limits_positions_without_filling_with_inferior_names() -> None:
    policy = _permissive_policy(max_sector_weight=0.25)
    snapshot = _snapshot(tuple(float(10 - index) for index in range(10)), policy=policy)

    selection = select_quality_compounders(
        snapshot=snapshot,
        securities=_securities(10, common_sector=True),
        entries_allowed=True,
        policy=policy,
    )

    assert len(selection.positions) == 3
    assert selection.target_gross_exposure == pytest.approx(0.20)
    assert selection.intentional_cash_slots == 12
    assert set(dict(selection.exclusion_reasons).values()) == {"concentration_limit"}


def test_small_cap_position_is_capped_without_redistributing_the_cash() -> None:
    policy = QualityCompounderPolicy()
    snapshot = _snapshot((2.0, 1.0), policy=policy)

    selection = select_quality_compounders(
        snapshot=snapshot,
        securities=_securities(2, bucket=MarketCapBucket.SMALL),
        entries_allowed=True,
        policy=policy,
    )

    assert len(selection.positions) == 1
    assert selection.positions[0].target_weight == 0.05
    assert selection.target_gross_exposure == 0.05
    assert selection.intentional_cash_slots == 14


def test_expected_cost_blocks_entry() -> None:
    policy = _permissive_policy(target_holdings=2)
    snapshot = _snapshot((2.0, 1.0), policy=policy)

    selection = select_quality_compounders(
        snapshot=snapshot,
        securities=_securities(2, costs={0: 40.01}),
        entries_allowed=True,
        policy=policy,
    )

    assert [item.security.symbol for item in selection.positions] == ["S01"]
    assert dict(selection.exclusion_reasons)["security-00"] == "expected_cost_limit"


def test_market_ineligibility_blocks_entries_and_exits_incumbents() -> None:
    policy = _permissive_policy(target_holdings=2)
    snapshot = _snapshot((2.0, 1.0), policy=policy)
    securities = list(_securities(2))
    securities[0] = replace(
        securities[0],
        market_eligible=False,
        market_ineligibility_reason="liquidity_below_minimum",
    )
    incumbent = QualityCompounderHolding(
        entity_id=securities[0].entity_id,
        instrument_id=securities[0].instrument_id,
        symbol=securities[0].symbol,
        factor_snapshot_id=securities[0].factor_snapshot_id,
        sector=securities[0].sector,
        industry=securities[0].industry,
    )

    selection = select_quality_compounders(
        snapshot=snapshot,
        securities=securities,
        incumbents=(incumbent,),
        entries_allowed=True,
        policy=policy,
    )

    assert selection.exits[0].reason == "market_ineligible:liquidity_below_minimum"


def test_quality_deterioration_exits_an_otherwise_top_ranked_incumbent() -> None:
    policy = _permissive_policy(target_holdings=2, quality_floor=0.25)
    entities = tuple(
        CrossSectionalEntity(
            entity_id=f"security-{index:02d}",
            symbol=f"S{index:02d}",
            peer_groups=("industry:all",),
        )
        for index in range(10)
    )
    observations = tuple(
        FactorObservation(
            entity_id=entity.entity_id,
            factor_name=spec.name,
            raw_value=(
                5.5
                if index == 0 and spec.name == "quality"
                else 100.0
                if index == 0
                else float(10 - index)
            ),
            source_observation_ids=(f"source:{entity.entity_id}:{spec.name}",),
        )
        for index, entity in enumerate(entities)
        for spec in policy.factor_specs
        if spec.enabled
    )
    snapshot = CrossSectionalRanker(minimum_peer_count=2).rank(
        cutoff=_CUTOFF,
        entities=entities,
        factor_specs=policy.factor_specs,
        observations=observations,
    )
    incumbent = QualityCompounderHolding(
        "security-00",
        1,
        "S00",
        f"{1:064x}",
        "sector:00",
        "industry:00",
    )

    selection = select_quality_compounders(
        snapshot=snapshot,
        securities=_securities(10),
        incumbents=(incumbent,),
        entries_allowed=True,
        policy=policy,
    )

    assert snapshot.ranks[0].entity_id == "security-00"
    assert [(item.holding.symbol, item.reason) for item in selection.exits] == [
        ("S00", "quality_deterioration")
    ]


def test_security_evidence_must_exactly_cover_rank_snapshot() -> None:
    policy = QualityCompounderPolicy()
    snapshot = _snapshot((2.0, 1.0), policy=policy)

    with pytest.raises(ValueError, match="exactly cover"):
        select_quality_compounders(
            snapshot=snapshot,
            securities=_securities(1),
            entries_allowed=True,
            policy=policy,
        )


def test_rank_snapshot_must_use_the_registered_factor_policy() -> None:
    policy = QualityCompounderPolicy()
    snapshot = CrossSectionalRanker(minimum_peer_count=2).rank(
        cutoff=_CUTOFF,
        entities=(
            CrossSectionalEntity("security-00", "S00", ("industry:all",)),
            CrossSectionalEntity("security-01", "S01", ("industry:all",)),
        ),
        factor_specs=(FactorSpec("quality", 1.0),),
        observations=(
            FactorObservation("security-00", "quality", 2.0),
            FactorObservation("security-01", "quality", 1.0),
        ),
    )

    with pytest.raises(ValueError, match="factor policy"):
        select_quality_compounders(
            snapshot=snapshot,
            securities=_securities(2),
            entries_allowed=True,
            policy=policy,
        )


def test_departed_incumbent_cannot_reuse_a_current_instrument_identity() -> None:
    policy = _permissive_policy()
    snapshot = _snapshot((2.0, 1.0), policy=policy)
    stale = QualityCompounderHolding(
        "departed-security",
        1,
        "OLD",
        "f" * 64,
        "sector:old",
        "industry:old",
    )

    with pytest.raises(ValueError, match="identity conflicts"):
        select_quality_compounders(
            snapshot=snapshot,
            securities=_securities(2),
            incumbents=(stale,),
            entries_allowed=True,
            policy=policy,
        )
