"""Focused fail-closed tests for quality-compounder panel registration."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from lib_strategy.equity_market_factors import EquityMarketFactorPolicy
from market_data_ingestor.quality_compounder_panel import (
    quality_compounder_provider_authority_policy,
)
from market_data_ingestor.quality_compounder_quarterly import (
    QualityCompounderQuarterlyWindow,
)
from market_data_ingestor.quality_compounder_registration import (
    QualityCompounderRegistrationError,
    persist_quality_compounder_panel_revision,
    require_quality_compounder_factor_coverage,
)


def _coverage_snapshots(*, complete: int, total: int) -> dict[int, SimpleNamespace]:
    return {
        instrument_id: SimpleNamespace(
            completeness_status="complete" if instrument_id <= complete else "incomplete"
        )
        for instrument_id in range(1, total + 1)
    }


def test_factor_coverage_accepts_exact_overall_and_material_sector_floors() -> None:
    snapshots = {
        instrument_id: SimpleNamespace(
            completeness_status=(
                "complete" if instrument_id <= 7 or 11 <= instrument_id <= 19 else "incomplete"
            )
        )
        for instrument_id in range(1, 21)
    }
    sectors = {
        instrument_id: "Technology" if instrument_id <= 10 else "Industrials"
        for instrument_id in snapshots
    }

    require_quality_compounder_factor_coverage(
        sector_by_instrument=sectors,
        snapshots=snapshots,
    )


def test_factor_coverage_reports_deterministic_overall_failure() -> None:
    snapshots = _coverage_snapshots(complete=15, total=20)
    sectors = {instrument_id: f"Sector {instrument_id}" for instrument_id in snapshots}

    with pytest.raises(
        QualityCompounderRegistrationError,
        match=(
            r"overall=15/20 \(75\.00%, required>=80\.00%\); "
            r"material_sector_failures=none"
        ),
    ):
        require_quality_compounder_factor_coverage(
            sector_by_instrument=sectors,
            snapshots=snapshots,
        )


def test_factor_coverage_reports_sorted_material_sector_failures() -> None:
    snapshots = {
        instrument_id: SimpleNamespace(
            completeness_status=(
                "complete" if instrument_id <= 6 or instrument_id >= 11 else "incomplete"
            )
        )
        for instrument_id in range(1, 21)
    }
    sectors = {
        instrument_id: "Technology" if instrument_id <= 10 else "Industrials"
        for instrument_id in snapshots
    }
    # Overall remains exactly 80%, while Technology is only 60% complete and
    # must independently block.

    with pytest.raises(
        QualityCompounderRegistrationError,
        match=(
            r"overall=16/20 \(80\.00%, required>=80\.00%\); "
            r"material_sector_failures=Technology=6/10 "
            r"\(60\.00%, required>=70\.00%\)"
        ),
    ):
        require_quality_compounder_factor_coverage(
            sector_by_instrument=sectors,
            snapshots=snapshots,
        )


def test_factor_coverage_does_not_treat_nine_members_as_a_material_sector() -> None:
    snapshots = _coverage_snapshots(complete=8, total=10)
    sectors = dict.fromkeys(range(1, 10), "Small Sector")
    sectors[10] = "Other"

    require_quality_compounder_factor_coverage(
        sector_by_instrument=sectors,
        snapshots=snapshots,
    )


def test_registration_clock_cannot_precede_knowledge_cutoff() -> None:
    cutoff = datetime(2026, 6, 30, 21, 30, tzinfo=UTC)
    window = QualityCompounderQuarterlyWindow(
        calendar_id=1,
        decision_session_id=1,
        decision_opens_at=datetime(2026, 6, 30, 13, 30, tzinfo=UTC),
        decision_closes_at=datetime(2026, 6, 30, 20, 0, tzinfo=UTC),
        execution_session_id=2,
        execution_opens_at=datetime(2026, 7, 1, 13, 30, tzinfo=UTC),
        execution_closes_at=datetime(2026, 7, 1, 20, 0, tzinfo=UTC),
    )

    with (
        Session() as session,
        pytest.raises(
            QualityCompounderRegistrationError,
            match="cannot precede",
        ),
    ):
        persist_quality_compounder_panel_revision(
            session,
            window=window,
            cutoff=cutoff,
            now=cutoff.replace(minute=29),
            market=SimpleNamespace(),
            market_policy=EquityMarketFactorPolicy(
                round_trip_commission_bps=1.25,
                cost_context_sha256="a" * 64,
                required_adjustment_policy=("in-house-split-and-dividend-total-return-v1"),
            ),
            fundamentals=SimpleNamespace(),
            market_cap_by_symbol={},
            provider_authority_policy=quality_compounder_provider_authority_policy("owner-1"),
            entitlement_owner_user_id="owner-1",
        )


def test_registration_rejects_a_market_snapshot_from_another_policy() -> None:
    cutoff = datetime(2026, 6, 30, 21, 30, tzinfo=UTC)
    window = QualityCompounderQuarterlyWindow(
        calendar_id=1,
        decision_session_id=1,
        decision_opens_at=datetime(2026, 6, 30, 13, 30, tzinfo=UTC),
        decision_closes_at=datetime(2026, 6, 30, 20, 0, tzinfo=UTC),
        execution_session_id=2,
        execution_opens_at=datetime(2026, 7, 1, 13, 30, tzinfo=UTC),
        execution_closes_at=datetime(2026, 7, 1, 20, 0, tzinfo=UTC),
    )
    market_policy = EquityMarketFactorPolicy(
        round_trip_commission_bps=1.25,
        cost_context_sha256="a" * 64,
        required_adjustment_policy="in-house-split-and-dividend-total-return-v1",
    )

    with (
        Session() as session,
        pytest.raises(
            QualityCompounderRegistrationError,
            match="policy identities differ",
        ),
    ):
        persist_quality_compounder_panel_revision(
            session,
            window=window,
            cutoff=cutoff,
            now=cutoff,
            market=SimpleNamespace(policy_sha256="b" * 64),
            market_policy=market_policy,
            fundamentals=SimpleNamespace(),
            market_cap_by_symbol={},
            provider_authority_policy=quality_compounder_provider_authority_policy("owner-1"),
            entitlement_owner_user_id="owner-1",
        )
