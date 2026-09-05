"""Operational freshness pins for synchronized strategy panels."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from indicator_runner.runtime_journal import StrategyOperationalStatusReader
from lib_application.outbox import OutboxBacklogSnapshot


class _PanelSession:
    def __init__(self, *, cutoff: datetime) -> None:
        decision = SimpleNamespace(
            strategy_input_sha256="input-sha",
            status="completed",
            decision_key="decision-key",
            created_at=cutoff,
            cutoff_at=cutoff,
        )
        self._scalar_results = iter(
            (
                SimpleNamespace(cutoff_at=cutoff, input_sha256="input-sha"),
                decision,
                decision,
            )
        )
        self._runtime = SimpleNamespace(
            panel_data_use_scope="paper_forward",
            panel_entitlement_owner_user_id="00000000-0000-0000-0000-000000000001",
            panel_activation_cutoff=cutoff - timedelta(days=1),
        )

    def __enter__(self) -> _PanelSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, *_args: object) -> object:
        return self._runtime

    def scalar(self, *_args: object) -> object:
        return next(self._scalar_results)


@pytest.mark.parametrize(
    ("max_panel_age_days", "expected_ready", "expected_reason"),
    [
        (40, False, "panel_input_stale"),
        (100, True, "within_panel_cadence"),
    ],
)
def test_panel_readiness_honors_the_strategy_specific_freshness_bound(
    max_panel_age_days: int,
    expected_ready: bool,
    expected_reason: str,
) -> None:
    now = datetime(2026, 7, 20, tzinfo=UTC)
    cutoff = now - timedelta(days=50)
    reader = StrategyOperationalStatusReader(
        lambda: _PanelSession(cutoff=cutoff),  # type: ignore[arg-type]
        clock=lambda: now,
    )

    status = reader._read_panel_status(
        worker_id="quality:paper",
        strategy_id="us_quality_compounder_v1",
        strategy_version="0.2.0",
        outbox_snapshot=OutboxBacklogSnapshot(counts={}, oldest_age_seconds=0.0),
        counts={},
        oldest_age=0.0,
        max_outbox_age_seconds=300.0,
        max_strategy_lag_seconds=300.0,
        max_panel_age_seconds=max_panel_age_days * 24 * 60 * 60,
    )

    assert status.ready is expected_ready
    assert status.panel_progress is not None
    assert status.panel_progress.reason == expected_reason
    assert status.panel_progress.age_seconds == 50 * 24 * 60 * 60


def test_operational_read_rejects_non_positive_panel_freshness() -> None:
    reader = StrategyOperationalStatusReader(lambda: None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Operational readiness SLOs must be positive"):
        reader.read(
            worker_id="quality:paper",
            strategy_id="us_quality_compounder_v1",
            symbols=(),
            source="eodhd",
            timeframe="1d",
            asset_class="equity",
            max_outbox_age_seconds=300.0,
            max_strategy_lag_seconds=300.0,
            panel_capable=True,
            strategy_version="0.2.0",
            max_panel_age_seconds=0.0,
        )
