"""Tests for the one-shot Quality Compounder panel producer."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from lib_strategy.equity_market_factors import EquityMarketFactorPolicy
from lib_strategy.equity_transaction_costs import DailyBarCostModelPolicy
from market_data_ingestor import quality_compounder_producer as producer_mod
from market_data_ingestor.quality_compounder_market import (
    QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY,
)
from market_data_ingestor.quality_compounder_producer import (
    QualityCompounderPanelProducer,
    QualityCompounderProducerError,
)
from market_data_ingestor.quality_compounder_quarterly import (
    QualityCompounderQuarterlyWindow,
)

_CLOSE = datetime(2026, 6, 30, 20, 0, tzinfo=UTC)
_OPEN = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)


def _cost_and_market() -> tuple[DailyBarCostModelPolicy, EquityMarketFactorPolicy]:
    cost = DailyBarCostModelPolicy()
    return cost, EquityMarketFactorPolicy(
        round_trip_commission_bps=1.25,
        cost_context_sha256=cost.configuration_sha256,
        required_adjustment_policy=QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY,
    )


def _window() -> QualityCompounderQuarterlyWindow:
    return QualityCompounderQuarterlyWindow(
        calendar_id=1,
        decision_session_id=10,
        decision_opens_at=datetime(2026, 6, 30, 13, 30, tzinfo=UTC),
        decision_closes_at=_CLOSE,
        execution_session_id=11,
        execution_opens_at=_OPEN,
        execution_closes_at=datetime(2026, 7, 1, 20, 0, tzinfo=UTC),
    )


def test_producer_rejects_mismatched_cost_policy_identity() -> None:
    acquisition_cost, market = _cost_and_market()
    other_cost = DailyBarCostModelPolicy(reference_order_notional_usd=25_000.0)
    assert acquisition_cost.configuration_sha256 != other_cost.configuration_sha256

    with pytest.raises(QualityCompounderProducerError, match="different cost identities"):
        QualityCompounderPanelProducer(
            session_factory=lambda: object(),
            eodhd_client=object(),  # type: ignore[arg-type]
            sec_client=object(),  # type: ignore[arg-type]
            entitlement_owner_user_id="owner-1",
            market_policy=market,
            cost_policy=other_cost,
        )


def test_late_registration_rolls_back_before_panel_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_exit: list[type[BaseException] | None] = []
    persisted = False

    class _Transaction:
        def __enter__(self) -> None:
            return None

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _traceback: object,
        ) -> None:
            transaction_exit.append(exc_type)

    class _Session:
        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def begin(self) -> _Transaction:
            return _Transaction()

        def scalar(self, _query: object) -> object:
            return SimpleNamespace(status="active", strat_ver_id=1401)

    class _Resolver:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def materialize(self, **_kwargs: object) -> object:
            return object()

    def _persist(*_args: object, **_kwargs: object) -> None:
        nonlocal persisted
        persisted = True

    monkeypatch.setattr(
        producer_mod,
        "build_quality_compounder_materialization_panel",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        producer_mod,
        "persist_quality_compounder_universe",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        producer_mod,
        "persist_quality_compounder_benchmark_identity",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        producer_mod,
        "persist_quality_compounder_sec_graphs",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(producer_mod, "QualityCompounderDatabaseFactorResolver", _Resolver)
    monkeypatch.setattr(producer_mod, "persist_quality_compounder_panel_revision", _persist)
    cost, market = _cost_and_market()
    producer = QualityCompounderPanelProducer(
        session_factory=_Session,
        eodhd_client=object(),  # type: ignore[arg-type]
        sec_client=object(),  # type: ignore[arg-type]
        entitlement_owner_user_id="owner-1",
        market_policy=market,
        cost_policy=cost,
        clock=lambda: _OPEN,
    )

    with pytest.raises(QualityCompounderProducerError, match="execution-session open"):
        producer._persist_and_register(
            window=_window(),
            cutoff=datetime(2026, 6, 30, 22, 0, tzinfo=UTC),
            universe=SimpleNamespace(
                membership=SimpleNamespace(
                    components=(),
                    current_evidence=object(),
                    historical_evidence=object(),
                    ticker_history_evidence=object(),
                    decision_session=_CLOSE.date(),
                ),
                identities={},
            ),  # type: ignore[arg-type]
            benchmark=object(),  # type: ignore[arg-type]
            market_series=(),
            sec_graphs=(),
        )

    assert persisted is False
    assert transaction_exit == [QualityCompounderProducerError]


def test_registration_crossing_next_open_rolls_back_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_exit: list[type[BaseException] | None] = []
    clocks = iter((datetime(2026, 7, 1, 13, 29, tzinfo=UTC), _OPEN))

    class _Transaction:
        def __enter__(self) -> None:
            return None

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _traceback: object,
        ) -> None:
            transaction_exit.append(exc_type)

    class _Session:
        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def begin(self) -> _Transaction:
            return _Transaction()

        def scalar(self, _query: object) -> object:
            return SimpleNamespace(status="active", strat_ver_id=1401)

    class _Resolver:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def materialize(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                market_snapshot=object(),
                fundamental_snapshot=object(),
                market_cap_by_symbol={},
            )

    for name in (
        "persist_quality_compounder_universe",
        "persist_quality_compounder_benchmark_identity",
        "persist_quality_compounder_sec_graphs",
        "persist_quality_compounder_panel_revision",
    ):
        monkeypatch.setattr(producer_mod, name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        producer_mod,
        "build_quality_compounder_materialization_panel",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(producer_mod, "QualityCompounderDatabaseFactorResolver", _Resolver)
    cost, market = _cost_and_market()
    producer = QualityCompounderPanelProducer(
        session_factory=_Session,
        eodhd_client=object(),  # type: ignore[arg-type]
        sec_client=object(),  # type: ignore[arg-type]
        entitlement_owner_user_id="owner-1",
        market_policy=market,
        cost_policy=cost,
        clock=lambda: next(clocks),
    )

    with pytest.raises(QualityCompounderProducerError, match="panel commit clock"):
        producer._persist_and_register(
            window=_window(),
            cutoff=datetime(2026, 6, 30, 22, 0, tzinfo=UTC),
            universe=SimpleNamespace(
                membership=SimpleNamespace(
                    components=(),
                    current_evidence=object(),
                    historical_evidence=object(),
                    ticker_history_evidence=object(),
                    decision_session=_CLOSE.date(),
                ),
                identities={},
            ),  # type: ignore[arg-type]
            benchmark=object(),  # type: ignore[arg-type]
            market_series=(),
            sec_graphs=(),
        )

    assert transaction_exit == [QualityCompounderProducerError]
