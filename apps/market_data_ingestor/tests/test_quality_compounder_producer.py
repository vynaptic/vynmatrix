"""Tests for the one-shot Quality Compounder panel producer."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from lib_application.services.sec_filing_observations import (
    SecFilingPersistenceError,
    SecIssuerFilingFetch,
)
from lib_strategy.equity_market_factors import EquityMarketFactorPolicy
from lib_strategy.equity_transaction_costs import DailyBarCostModelPolicy
from lib_strategy.sec_filing_rules import SEC_FILING_COVERAGE_TOLERANCE
from market_data_ingestor import quality_compounder_producer as producer_mod
from market_data_ingestor import sec_filing_events as filing_events_mod
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
from market_data_ingestor.sec_edgar import SecEdgarError, SecSubmissionDataset
from market_data_ingestor.sec_filing_events import SecFilingAcquisitionFailure

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


class _GateSession:
    """Session fake recording each gate transaction's entry and exit."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __enter__(self) -> _GateSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _GateTransaction:
        return _GateTransaction(self._events)


class _GateTransaction:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __enter__(self) -> None:
        self._events.append("gate_begin")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        self._events.append("gate_rollback" if exc_type else "gate_commit")


def _identity(symbol: str, cik: str) -> SimpleNamespace:
    return SimpleNamespace(symbol=symbol, issuer_id=f"cik:{cik}")


def _clock(start: datetime) -> Iterator[datetime]:
    current = start
    while True:
        yield current
        current += timedelta(minutes=1)


def _gate_producer(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    acquire: object,
    seen: dict[str, object] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> QualityCompounderPanelProducer:
    """Drive ``produce`` with every panel stage faked and the gate stages observed."""

    identities = {
        "GOOG": _identity("GOOG", "0001652044"),
        "GOOGL": _identity("GOOGL", "0001652044"),
        "MSFT": _identity("MSFT", "0000789019"),
    }
    graphs = (
        SimpleNamespace(cik="0000789019", identities=(identities["MSFT"],)),
        SimpleNamespace(
            cik="0001652044",
            identities=(identities["GOOG"], identities["GOOGL"]),
        ),
    )
    universe = SimpleNamespace(
        membership=SimpleNamespace(components=(), decision_session=_CLOSE.date()),
        identities=identities,
        acquired_at=_CLOSE,
    )
    monkeypatch.setattr(
        producer_mod,
        "acquire_quality_compounder_membership",
        lambda **_kwargs: universe.membership,
    )
    monkeypatch.setattr(
        producer_mod, "acquire_quality_compounder_identities", lambda **_kwargs: universe
    )
    monkeypatch.setattr(
        producer_mod,
        "acquire_quality_compounder_benchmark_identity",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        producer_mod,
        "acquire_quality_compounder_market_series",
        lambda **_kwargs: object(),
    )

    def _sec_graphs(_client: object, **kwargs: object) -> object:
        events.append("sec_graphs")
        if seen is not None:
            seen["sec_cutoff"] = kwargs["cutoff"]
        return graphs

    def _acquire_gate(_client: object, **kwargs: object) -> object:
        events.append("gate_acquire")
        if seen is not None:
            seen["filing_cutoff"] = kwargs["fetch_cutoff"]
            seen["issuers"] = kwargs["issuers"]
            seen["stop_at"] = kwargs["stop_at"]
        assert callable(acquire)
        return acquire()

    def _register(_self: object, **kwargs: object) -> None:
        events.append("panel")
        if seen is not None:
            seen["panel_cutoff"] = kwargs["cutoff"]

    def _persist_gate(_session: object, fetch: object, **kwargs: object) -> tuple[str, ...]:
        assert kwargs == {"entitlement_owner_user_id": None}
        cik = str(getattr(fetch, "cik", ""))
        events.append(f"gate_persist:{cik}")
        if cik == "0000789019-fails":
            raise SecFilingPersistenceError("conflicting replay")
        if cik == "0000789019-db":
            raise SQLAlchemyError("connection lost")
        return ()

    monkeypatch.setattr(producer_mod, "acquire_quality_compounder_sec_graphs", _sec_graphs)
    monkeypatch.setattr(producer_mod, "acquire_sec_filing_events", _acquire_gate)
    monkeypatch.setattr(producer_mod, "persist_sec_filing_fetch", _persist_gate)
    monkeypatch.setattr(
        QualityCompounderPanelProducer, "_official_sessions", lambda _self, _window: ()
    )
    monkeypatch.setattr(
        QualityCompounderPanelProducer,
        "_catalogue_preflight",
        lambda _self, **_kwargs: {"GOOG": 7, "GOOGL": 8, "MSFT": 9, "SPY": 10},
    )
    monkeypatch.setattr(
        QualityCompounderPanelProducer,
        "_require_acquisition_cutoff",
        staticmethod(lambda **_kwargs: None),
    )
    monkeypatch.setattr(QualityCompounderPanelProducer, "_persist_and_register", _register)
    cost, market = _cost_and_market()
    ticks = _clock(datetime(2026, 6, 30, 21, 0, tzinfo=UTC))
    return QualityCompounderPanelProducer(
        session_factory=lambda: _GateSession(events),  # type: ignore[arg-type,return-value]
        eodhd_client=object(),  # type: ignore[arg-type]
        sec_client=object(),  # type: ignore[arg-type]
        entitlement_owner_user_id="owner-1",
        market_policy=market,
        cost_policy=cost,
        clock=clock or (lambda: next(ticks)),
    )


def _produce(producer: QualityCompounderPanelProducer) -> None:
    producer.produce(
        window=_window(),
        started_at=datetime(2026, 6, 30, 20, 30, tzinfo=UTC),
        complete_before=_OPEN,
    )


def test_gate_collection_persists_after_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    fetches = (
        SimpleNamespace(cik="0000789019", filings=()),
        SimpleNamespace(cik="0001652044", filings=()),
    )
    seen: dict[str, object] = {}
    producer = _gate_producer(monkeypatch, events, acquire=lambda: (fetches, ()), seen=seen)

    _produce(producer)

    assert seen["issuers"] == {"0000789019": (9,), "0001652044": (7, 8)}
    assert events[: events.index("panel") + 1] == ["sec_graphs", "gate_acquire", "panel"]
    assert events[events.index("panel") + 1 :] == [
        "gate_begin",
        "gate_persist:0000789019",
        "gate_commit",
        "gate_begin",
        "gate_persist:0001652044",
        "gate_commit",
    ]


def test_filing_cutoff_precedes_panel_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    seen: dict[str, object] = {}
    producer = _gate_producer(monkeypatch, events, acquire=lambda: ((), ()), seen=seen)

    _produce(producer)

    sec_cutoff, filing_cutoff, panel_cutoff = (
        seen["sec_cutoff"],
        seen["filing_cutoff"],
        seen["panel_cutoff"],
    )
    assert isinstance(sec_cutoff, datetime)
    assert isinstance(filing_cutoff, datetime)
    assert isinstance(panel_cutoff, datetime)
    assert sec_cutoff < filing_cutoff < panel_cutoff


def _raise(error: BaseException) -> object:
    def _acquire() -> object:
        raise error

    return _acquire


@pytest.mark.parametrize(
    "acquire",
    [
        lambda: ((), (SecFilingAcquisitionFailure(cik="0000789019", error="E: x"),)),
        _raise(SecEdgarError("rate limited")),
        _raise(SecFilingPersistenceError("invalid fetch")),
        _raise(SQLAlchemyError("connection lost")),
        _raise(ValueError("duplicate CIK")),
    ],
    ids=["issuer-failure", "sec-edgar", "persistence", "sqlalchemy", "value"],
)
def test_gate_issuer_failure_does_not_fail_panel(
    monkeypatch: pytest.MonkeyPatch,
    acquire: object,
) -> None:
    events: list[str] = []
    producer = _gate_producer(monkeypatch, events, acquire=acquire)

    _produce(producer)

    assert events.count("panel") == 1
    assert not any(item.startswith("gate_persist:") for item in events)


def test_gate_acquisition_does_not_swallow_unnamed_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    producer = _gate_producer(monkeypatch, events, acquire=_raise(KeyError("bug")))

    with pytest.raises(KeyError):
        _produce(producer)

    assert "panel" not in events


def test_gate_persist_failure_does_not_fail_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    fetches = (
        SimpleNamespace(cik="0000789019-fails", filings=()),
        SimpleNamespace(cik="0000789019-db", filings=()),
        SimpleNamespace(cik="0001652044", filings=()),
    )
    producer = _gate_producer(monkeypatch, events, acquire=lambda: (fetches, ()))

    _produce(producer)

    assert events[events.index("panel") + 1 :] == [
        "gate_begin",
        "gate_persist:0000789019-fails",
        "gate_rollback",
        "gate_begin",
        "gate_persist:0000789019-db",
        "gate_rollback",
        "gate_begin",
        "gate_persist:0001652044",
        "gate_commit",
    ]


class _LogRecorder:
    """Stand-in for the producer logger recording ``(level, event, fields)``."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.records.append(("info", event, fields))

    def warning(self, event: str, **fields: object) -> None:
        self.records.append(("warning", event, fields))


def test_gate_stop_time_is_coverage_tolerance_after_filing_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    seen: dict[str, object] = {}
    producer = _gate_producer(monkeypatch, events, acquire=lambda: ((), ()), seen=seen)

    _produce(producer)

    filing_cutoff = seen["filing_cutoff"]
    assert isinstance(filing_cutoff, datetime)
    assert seen["stop_at"] == filing_cutoff + SEC_FILING_COVERAGE_TOLERANCE


def test_gate_stop_time_leaves_margin_before_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    seen: dict[str, object] = {}
    ticks = _clock(_OPEN - timedelta(hours=1))
    producer = _gate_producer(
        monkeypatch,
        events,
        acquire=lambda: ((), ()),
        seen=seen,
        clock=lambda: next(ticks),
    )

    _produce(producer)

    assert seen["stop_at"] == _OPEN - producer_mod.SEC_FILING_GATE_DEADLINE_MARGIN
    assert events.count("panel") == 1


def test_gate_time_budget_exhausted_part_way_still_commits_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    now = [datetime(2026, 6, 30, 21, 0, tzinfo=UTC)]

    def _tick() -> datetime:
        now[0] += timedelta(minutes=1)
        return now[0]

    producer = _gate_producer(monkeypatch, events, acquire=lambda: ((), ()), clock=_tick)
    requested: list[str] = []

    class _SlowSec:
        def fetch_submissions(self, cik: str, **_kwargs: object) -> SecSubmissionDataset:
            requested.append(cik)
            now[0] += SEC_FILING_COVERAGE_TOLERANCE
            return SecSubmissionDataset(
                cik=cik, entity_name=None, current_sic=None, filings=(), sources=()
            )

    producer._sec = _SlowSec()  # type: ignore[assignment]
    results: list[
        tuple[tuple[SecIssuerFilingFetch, ...], tuple[SecFilingAcquisitionFailure, ...]]
    ] = []

    def _real_acquire(client: object, **kwargs: object) -> object:
        events.append("gate_acquire")
        result = filing_events_mod.acquire_sec_filing_events(client, **kwargs)  # type: ignore[arg-type]
        results.append(result)
        return result

    monkeypatch.setattr(producer_mod, "acquire_sec_filing_events", _real_acquire)
    log = _LogRecorder()
    monkeypatch.setattr(producer_mod, "logger", log)

    _produce(producer)

    fetches, failures = results[0]
    assert requested == ["0000789019"]
    assert [fetch.cik for fetch in fetches] == ["0000789019"]
    assert [failure.cik for failure in failures] == ["0001652044"]
    assert events.count("panel") == 1
    assert events[events.index("panel") + 1 :] == [
        "gate_begin",
        "gate_persist:0000789019",
        "gate_commit",
    ]
    assert (
        "warning",
        "sec_filing_gate_issuer_failed",
        {
            "cik": "0001652044",
            "filing_cutoff": "2026-06-30T21:02:00+00:00",
            "error": "SecFilingGateTimeBudgetExceeded",
        },
    ) in log.records


def test_whole_gate_acquisition_failure_logs_distinct_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    producer = _gate_producer(monkeypatch, events, acquire=_raise(ValueError("duplicate CIK")))
    log = _LogRecorder()
    monkeypatch.setattr(producer_mod, "logger", log)

    _produce(producer)

    failed = [record for record in log.records if record[0] == "warning"]
    assert [(event, fields["error"]) for _level, event, fields in failed] == [
        ("sec_filing_gate_acquisition_failed", "ValueError")
    ]
    assert all("cik" not in fields for _level, _event, fields in failed)
    assert events.count("panel") == 1
