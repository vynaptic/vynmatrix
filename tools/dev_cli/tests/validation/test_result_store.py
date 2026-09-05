"""Persist a registered deterministic validation report and read it back."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from validation_helpers import bars_from_ohlcv, load_market_fixture

from dev_cli.validation.backtest.engine import BacktestConfig, BacktestEngine
from dev_cli.validation.persistence.backtest_manifest_store import canonical_manifest_bytes
from dev_cli.validation.persistence.backtest_result_store import (
    BacktestResultStore,
    DerivedBacktestEvidence,
    DerivedEvidenceMetrics,
)
from dev_cli.validation.persistence.backtest_trial_store import (
    BacktestTrialRegistration,
    BacktestTrialStore,
)
from lib_application.db.models import (
    BacktestExperiment,
    BacktestResult,
    BacktestTrial,
    Base,
    Strategy,
    StrategyVersion,
)
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy

_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json"
)


class _Momentum(PureSignalStrategy):
    def __init__(self) -> None:
        super().__init__(strategy_id="bt_store_test", strategy_type="indicator")

    @property
    def warmup_bars_needed(self) -> int:
        return 2

    def initialize(self) -> None:
        self._prev: float | None = None
        self._pos = 0

    def on_data(self, state: MarketState) -> None:
        if self._prev is not None and self.warmup_complete(state.symbol):
            if state.close > self._prev and self._pos <= 0:
                self.emit_long(
                    symbol=state.symbol, entry_price=state.close, timestamp=state.timestamp
                )
                self._pos = 1
            elif state.close < self._prev and self._pos > 0:
                self.emit_close(
                    symbol=state.symbol, exit_price=state.close, timestamp=state.timestamp
                )
                self._pos = 0
        self._prev = state.close


def _report():  # type: ignore[no-untyped-def]
    fixture = load_market_fixture(_FIXTURE)
    bars = bars_from_ohlcv(fixture["bars"], symbol="BTCUSD", source="coinbase_live")
    cfg = BacktestConfig(symbol="BTCUSD", consolidation_minutes=15)
    return BacktestEngine().run(_Momentum(), bars, cfg)


def _engine():  # type: ignore[no-untyped-def]
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def _running_trial(eng, *, parameters=None, running=True):  # type: ignore[no-untyped-def]
    with Session(eng) as session:
        strategy = Strategy(
            strategy_id="bt_store_test",
            strategy_name="Backtest Store Test",
            asset_class="crypto",
        )
        session.add(strategy)
        session.flush()
        version = StrategyVersion(
            strategy_id=strategy.strategy_id,
            semver="1.0.0",
            param_schema={},
            default_params={},
        )
        experiment = BacktestExperiment(
            strategy_id=strategy.strategy_id,
            experiment_type="parameter_sweep",
            metric="sharpe_ratio",
            config={},
        )
        session.add_all([version, experiment])
        session.commit()
        experiment_id = experiment.experiment_id
        strat_ver_id = version.strat_ver_id

    trial_store = BacktestTrialStore(eng)
    trial_id = trial_store.register(
        BacktestTrialRegistration(
            experiment_id=experiment_id,
            strategy_id="bt_store_test",
            strategy_semver="1.0.0",
            sequence=0,
            trial_family="baseline",
            fold_id="full",
            cost_scenario="expected",
            parameters=parameters or {},
            manifest_hash="a" * 64,
        )
    )
    if running:
        trial_store.mark_running(trial_id)
    return trial_id, strat_ver_id, experiment_id


def _derived_evidence(experiment_id: int) -> DerivedBacktestEvidence:
    return DerivedBacktestEvidence(
        strategy_id="bt_store_test",
        strategy_semver="1.0.0",
        experiment_id=experiment_id,
        evidence_kind="pooled_portfolio",
        symbol="BTCUSD+ETHUSD",
        asset_class="crypto",
        timeframe="1d",
        start_date=date(2022, 1, 1),
        end_date=date(2022, 1, 2),
        payload={
            "component_trial_ids": ["b" * 64, "c" * 64],
            "portfolio_weighting": "equal_weight_daily_returns",
        },
        metrics=DerivedEvidenceMetrics(
            initial_capital=10_000.0,
            total_return_pct=1.5,
            sharpe_ratio=0.75,
            total_trades=4,
            winning_trades=3,
            losing_trades=1,
            total_signals=6,
            final_equity=10_150.0,
            peak_equity=10_200.0,
            fees_paid=4.5,
        ),
        equity_curve=(
            (datetime(2022, 1, 1, tzinfo=UTC), 10_000.0),
            (datetime(2022, 1, 2, tzinfo=UTC), 10_150.0),
        ),
    )


def test_save_persists_report_for_exact_registered_version() -> None:
    eng = _engine()
    trial_id, strat_ver_id, experiment_id = _running_trial(eng)
    report = _report()
    store = BacktestResultStore(eng)
    bid = store.save(report, trial_id=trial_id)

    with Session(eng) as s:
        row = s.query(BacktestResult).filter_by(backtest_id=bid).one()
        assert row.strategy_id == "bt_store_test"
        assert row.strat_ver_id == strat_ver_id
        assert row.experiment_id == experiment_id
        assert row.symbol == "BTCUSD"
        assert row.asset_class == "crypto"
        assert row.timeframe == "15m"
        assert row.total_trades == 29
        assert row.winning_trades == 5
        assert row.losing_trades == 24
        assert row.total_signals == 59
        # Decimal columns round to the report metrics.
        assert float(row.total_return_pct) == -6.9482
        assert float(row.max_drawdown_pct) == 6.9482
        assert float(row.initial_capital) == 100000.00
        # Trade list + equity curve persisted as JSON.
        assert len(row.trades_json) == 29
        assert set(row.trades_json[0]) == {
            "cost_breakdown",
            "entry_price",
            "entry_reference_price",
            "entry_ts",
            "exit_price",
            "exit_reason",
            "exit_reference_price",
            "exit_ts",
            "fees",
            "gross_pnl",
            "holding_bars",
            "pnl",
            "quantity",
            "side",
            "symbol",
        }
        assert len(row.equity_curve) == 100
        assert row.engine == "internal"
        assert row.meta["manifest_hash"] == "a" * 64
        assert row.meta["trial_id"] == trial_id
        assert row.meta["report"] == report.to_dict()
        assert row.meta["report_evidence_schema_id"] == "vynmatrix.backtest.report-evidence"
        assert len(row.meta["report_sha256"]) == 64
        # Dates default to the equity-curve span.
        assert row.start_date.isoformat() == "2026-06-10"
        trial = s.get(BacktestTrial, trial_id)
        assert trial is not None
        assert trial.status == "completed"
        assert trial.result_id == row.result_id


def test_save_records_winning_parameters() -> None:
    eng = _engine()
    trial_id, _, _ = _running_trial(eng, parameters={"threshold": 0.005})
    store = BacktestResultStore(eng)
    bid = store.save(
        _report(),
        trial_id=trial_id,
        meta={"source": "grid_search"},
    )
    with Session(eng) as s:
        row = s.query(BacktestResult).filter_by(backtest_id=bid).one()
        assert row.parameters == {"threshold": 0.005}
        assert row.meta["manifest_hash"] == "a" * 64
        assert row.meta["source"] == "grid_search"
        assert row.meta["trial_id"] == trial_id
        assert row.meta["report"] == _report().to_dict()
        assert len(row.meta["report_sha256"]) == 64


def test_save_is_idempotent_after_trial_completion() -> None:
    eng = _engine()
    trial_id, _, _ = _running_trial(eng)
    store = BacktestResultStore(eng)

    first = store.save(_report(), trial_id=trial_id)
    second = store.save(_report(), trial_id=trial_id)

    assert first == second == trial_id
    with Session(eng) as session:
        assert session.query(BacktestResult).count() == 1


def test_verified_report_is_content_idempotent_and_detects_mutation() -> None:
    eng = _engine()
    trial_id, _, _ = _running_trial(eng)
    store = BacktestResultStore(eng)
    report = _report()

    first = store.save(
        report,
        trial_id=trial_id,
        meta={"source": "validation_campaign"},
    )
    second = store.save(
        report,
        trial_id=trial_id,
        meta={"source": "validation_campaign"},
    )

    assert first == second == trial_id
    loaded = store.load_verified_report_payload(trial_id)
    assert loaded == report.to_dict()
    loaded["signals_emitted"] = -1
    assert store.load_verified_report_payload(trial_id) == report.to_dict()

    changed = replace(report, signals_emitted=report.signals_emitted + 1)
    with pytest.raises(ValueError, match="content hash cannot be changed"):
        store.save(
            changed,
            trial_id=trial_id,
            meta={"source": "validation_campaign"},
        )

    with Session(eng) as session:
        row = session.query(BacktestResult).one()
        mutated_meta = json.loads(json.dumps(row.meta))
        mutated_meta["report"]["signals_emitted"] = -1
        row.meta = mutated_meta
        session.commit()
    with pytest.raises(RuntimeError, match="stored content does not match its hash"):
        store.load_verified_report_payload(trial_id)


def test_load_verified_report_evidence_exposes_fresh_complete_ledgers() -> None:
    eng = _engine()
    trial_id, strat_ver_id, experiment_id = _running_trial(
        eng,
        parameters={"indicator_period": 14},
    )
    store = BacktestResultStore(eng)
    report = _report()
    store.save(
        report,
        trial_id=trial_id,
        backtest_id="verified-report-001",
        meta={"source": "validation_campaign"},
    )

    first = store.load_verified_report_evidence(trial_id)

    assert first["backtest_id"] == "verified-report-001"
    assert first["trial_id"] == trial_id
    assert first["strategy_id"] == "bt_store_test"
    assert first["strategy_semver"] == "1.0.0"
    assert first["strat_ver_id"] == strat_ver_id
    assert first["experiment_id"] == experiment_id
    assert first["parameters"] == {"indicator_period": 14}
    assert first["report_evidence_schema_id"] == ("vynmatrix.backtest.report-evidence")
    assert first["report_evidence_schema_version"] == 1
    assert len(first["report_sha256"]) == 64
    assert first["report"] == report.to_dict()
    assert first["equity_curve"] == [
        [timestamp.isoformat(), equity] for timestamp, equity in report.equity_curve
    ]
    assert len(first["trades"]) == len(report.trades)
    assert first["trades"][0] == {
        "symbol": report.trades[0].symbol,
        "side": report.trades[0].side,
        "entry_ts": report.trades[0].entry_ts.isoformat(),
        "exit_ts": report.trades[0].exit_ts.isoformat(),
        "entry_price": report.trades[0].entry_price,
        "exit_price": report.trades[0].exit_price,
        "quantity": report.trades[0].quantity,
        "fees": report.trades[0].fees,
        "pnl": report.trades[0].pnl,
        "gross_pnl": report.trades[0].gross_pnl,
        "exit_reason": report.trades[0].exit_reason,
        "holding_bars": report.trades[0].holding_bars,
        "entry_reference_price": report.trades[0].entry_reference_price,
        "exit_reference_price": report.trades[0].exit_reference_price,
        "cost_breakdown": report.trades[0].cost_breakdown.to_dict(),
    }
    assert first["meta_context"] == {
        "manifest_hash": "a" * 64,
        "source": "validation_campaign",
        "trial_id": trial_id,
    }

    first["report"]["signals_emitted"] = -1
    first["trades"][0]["pnl"] = 0.0
    first["equity_curve"][0][1] = 0.0
    first["meta_context"]["source"] = "mutated-caller-copy"
    second = store.load_verified_report_evidence(trial_id)
    assert second["report"] == report.to_dict()
    assert second["trades"][0]["pnl"] == report.trades[0].pnl
    assert second["equity_curve"][0][1] == report.equity_curve[0][1]
    assert second["meta_context"]["source"] == "validation_campaign"


@pytest.mark.parametrize(
    "mutation",
    [
        "net_pnl",
        "gross_pnl",
        "exit_reason",
        "holding_bars",
        "entry_reference_price",
        "exit_reference_price",
        "cost_breakdown",
        "equity_curve",
        "identity",
        "parameters",
        "meta_context",
        "backtest_ids_together",
    ],
)
def test_verified_report_hash_covers_complete_ledger_and_context(mutation: str) -> None:
    eng = _engine()
    trial_id, _, _ = _running_trial(eng, parameters={"indicator_period": 14})
    store = BacktestResultStore(eng)
    store.save(
        _report(),
        trial_id=trial_id,
        backtest_id="verified-report-original",
        meta={"source": "validation_campaign"},
    )

    with Session(eng) as session:
        row = session.query(BacktestResult).one()
        trades = json.loads(json.dumps(row.trades_json))
        curve = json.loads(json.dumps(row.equity_curve))
        meta = json.loads(json.dumps(row.meta))
        parameters = json.loads(json.dumps(row.parameters))
        if mutation == "net_pnl":
            trades[0]["pnl"] += 1.0
            row.trades_json = trades
        elif mutation == "gross_pnl":
            trades[0]["gross_pnl"] += 1.0
            row.trades_json = trades
        elif mutation == "exit_reason":
            trades[0]["exit_reason"] = "mutated"
            row.trades_json = trades
        elif mutation == "holding_bars":
            trades[0]["holding_bars"] += 1
            row.trades_json = trades
        elif mutation == "entry_reference_price":
            trades[0]["entry_reference_price"] = 1.0
            row.trades_json = trades
        elif mutation == "exit_reference_price":
            trades[0]["exit_reference_price"] = 1.0
            row.trades_json = trades
        elif mutation == "cost_breakdown":
            trades[0]["cost_breakdown"]["commission"] += 1.0
            row.trades_json = trades
        elif mutation == "equity_curve":
            curve[0][1] += 1.0
            row.equity_curve = curve
        elif mutation == "identity":
            row.symbol = "ETHUSD"
        elif mutation == "parameters":
            parameters["indicator_period"] = 15
            row.parameters = parameters
        elif mutation == "meta_context":
            meta["source"] = "mutated"
            row.meta = meta
        elif mutation == "backtest_ids_together":
            row.backtest_id = "verified-report-mutated"
            meta["result_backtest_id"] = "verified-report-mutated"
            row.meta = meta
        else:  # pragma: no cover - exhaustive parameter list
            raise AssertionError(mutation)
        session.commit()

    with pytest.raises(RuntimeError, match="stored content does not match its hash"):
        store.load_verified_report_evidence(trial_id)


@pytest.mark.parametrize(
    "field",
    [
        "initial_capital",
        "total_return_pct",
        "sharpe_ratio",
        "total_trades",
        "total_signals",
        "fees_paid",
    ],
)
def test_verified_report_loader_rejects_metric_projection_mutation(field: str) -> None:
    eng = _engine()
    trial_id, _, _ = _running_trial(eng)
    store = BacktestResultStore(eng)
    store.save(_report(), trial_id=trial_id)

    with Session(eng) as session:
        row = session.query(BacktestResult).one()
        current = getattr(row, field)
        if isinstance(current, Decimal):
            setattr(row, field, current + Decimal("1"))
        elif isinstance(current, int):
            setattr(row, field, current + 1)
        else:  # pragma: no cover - every registered projection is non-null
            raise TypeError(field)
        session.commit()

    with pytest.raises(ValueError, match=f"{field} projection is inconsistent"):
        store.load_verified_report_evidence(trial_id)


def test_save_refuses_unregistered_trial() -> None:
    eng = _engine()
    store = BacktestResultStore(eng)

    with pytest.raises(ValueError, match="not registered"):
        store.save(_report(), trial_id="f" * 64)


def test_save_derived_evidence_persists_exact_linkage_and_canonical_hash() -> None:
    eng = _engine()
    trial_id, strat_ver_id, experiment_id = _running_trial(eng)
    evidence = _derived_evidence(experiment_id)
    store = BacktestResultStore(eng)

    bid = store.save_derived_evidence(evidence, trial_id=trial_id)

    with Session(eng) as session:
        row = session.query(BacktestResult).filter_by(backtest_id=bid).one()
        trial = session.get(BacktestTrial, trial_id)
        assert trial is not None
        assert trial.status == "completed"
        assert trial.result_id == row.result_id
        assert row.strategy_id == evidence.strategy_id
        assert row.strat_ver_id == strat_ver_id
        assert row.experiment_id == experiment_id
        assert row.engine == "custom"
        assert row.symbol == "BTCUSD+ETHUSD"
        assert row.asset_class == "crypto"
        assert row.timeframe == "1d"
        assert row.parameters == {}
        assert row.trades_json is None
        assert row.profit_factor is None
        assert float(row.initial_capital) == 10_000.0
        assert float(row.total_return_pct) == 1.5
        assert row.total_trades == 4
        assert row.equity_curve == [
            ["2022-01-01T00:00:00+00:00", 10_000.0],
            ["2022-01-02T00:00:00+00:00", 10_150.0],
        ]
        assert row.meta["evidence_payload"] == evidence.payload
        assert row.meta["manifest_hash"] == "a" * 64
        assert row.meta["trial_id"] == trial_id
        assert row.meta["schema_initial_capital_sentinel"] is False
        assert len(row.meta["evidence_sha256"]) == 64

        envelope = {
            "asset_class": evidence.asset_class,
            "backtest_id": bid,
            "end_date": evidence.end_date.isoformat(),
            "equity_curve": row.equity_curve,
            "evidence_kind": evidence.evidence_kind,
            "evidence_schema_id": "vynmatrix.backtest.derived-evidence",
            "evidence_schema_version": 1,
            "experiment_id": experiment_id,
            "manifest_hash": "a" * 64,
            "metric_fields": row.meta["metric_fields"],
            "metrics": {
                key: value
                for key, value in asdict(evidence.metrics).items()  # type: ignore[arg-type]
                if value is not None
            },
            "parameters": {},
            "payload": evidence.payload,
            "schema_initial_capital_sentinel": False,
            "start_date": evidence.start_date.isoformat(),
            "strat_ver_id": strat_ver_id,
            "strategy_id": evidence.strategy_id,
            "strategy_semver": evidence.strategy_semver,
            "symbol": evidence.symbol,
            "timeframe": evidence.timeframe,
            "trial_id": trial_id,
        }
        expected_hash = hashlib.sha256(canonical_manifest_bytes(envelope)).hexdigest()
        assert row.meta["evidence_sha256"] == expected_hash


def test_save_derived_diagnostic_leaves_unavailable_metrics_null() -> None:
    eng = _engine()
    trial_id, _, experiment_id = _running_trial(eng)
    evidence = replace(
        _derived_evidence(experiment_id),
        evidence_kind="probability_of_backtest_overfitting",
        symbol="PORTFOLIO",
        metrics=None,
        equity_curve=(),
        payload={"pbo": 0.42, "candidate_order": ["B0", "A1"]},
    )

    BacktestResultStore(eng).save_derived_evidence(evidence, trial_id=trial_id)

    with Session(eng) as session:
        row = session.query(BacktestResult).one()
        assert float(row.initial_capital) == 0.0
        assert row.total_return_pct is None
        assert row.total_trades is None
        assert row.winning_trades is None
        assert row.losing_trades is None
        assert row.total_signals is None
        assert row.equity_curve is None
        assert row.meta["metric_fields"] == []
        assert row.meta["schema_initial_capital_sentinel"] is True


def test_save_derived_evidence_is_content_idempotent_and_immutable() -> None:
    eng = _engine()
    trial_id, _, experiment_id = _running_trial(eng)
    evidence = _derived_evidence(experiment_id)
    store = BacktestResultStore(eng)

    first = store.save_derived_evidence(
        evidence,
        trial_id=trial_id,
        backtest_id="derived-portfolio-001",
    )
    second = store.save_derived_evidence(
        evidence,
        trial_id=trial_id,
        backtest_id="derived-portfolio-001",
    )

    assert first == second == "derived-portfolio-001"
    with Session(eng) as session:
        assert session.query(BacktestResult).count() == 1

    changed = replace(evidence, payload={**evidence.payload, "portfolio_weighting": "risk_parity"})
    with pytest.raises(ValueError, match="content hash cannot be changed"):
        store.save_derived_evidence(changed, trial_id=trial_id)
    with pytest.raises(ValueError, match="different backtest_id"):
        store.save_derived_evidence(
            evidence,
            trial_id=trial_id,
            backtest_id="derived-portfolio-002",
        )


def test_load_derived_payload_verifies_hash_kind_and_returns_fresh_copy() -> None:
    eng = _engine()
    trial_id, _, experiment_id = _running_trial(eng)
    store = BacktestResultStore(eng)
    evidence = _derived_evidence(experiment_id)
    store.save_derived_evidence(evidence, trial_id=trial_id)

    first = store.load_derived_payload(
        trial_id,
        expected_evidence_kind=evidence.evidence_kind,
    )
    first["portfolio_weighting"] = "mutated-caller-copy"
    second = store.load_derived_payload(
        trial_id,
        expected_evidence_kind=evidence.evidence_kind,
    )

    assert second == evidence.payload
    with pytest.raises(ValueError, match="evidence_kind differs"):
        store.load_derived_payload(trial_id, expected_evidence_kind="wrong-kind")

    with Session(eng) as session:
        row = session.query(BacktestResult).one()
        mutated_meta = json.loads(json.dumps(row.meta))
        mutated_meta["evidence_payload"]["portfolio_weighting"] = "tampered"
        row.meta = mutated_meta
        session.commit()

    with pytest.raises(RuntimeError, match="stored content does not match its hash"):
        store.load_derived_payload(trial_id)


def test_save_derived_evidence_detects_stored_payload_and_result_id_mutation() -> None:
    payload_engine = _engine()
    trial_id, _, experiment_id = _running_trial(payload_engine)
    evidence = _derived_evidence(experiment_id)
    payload_store = BacktestResultStore(payload_engine)
    payload_store.save_derived_evidence(evidence, trial_id=trial_id)
    with Session(payload_engine) as session:
        row = session.query(BacktestResult).one()
        row.meta = {
            **row.meta,
            "evidence_payload": {"portfolio_weighting": "mutated_after_completion"},
        }
        session.commit()
    with pytest.raises(RuntimeError, match="stored content does not match"):
        payload_store.save_derived_evidence(evidence, trial_id=trial_id)

    id_engine = _engine()
    id_trial_id, _, id_experiment_id = _running_trial(id_engine)
    id_evidence = _derived_evidence(id_experiment_id)
    id_store = BacktestResultStore(id_engine)
    id_store.save_derived_evidence(
        id_evidence,
        trial_id=id_trial_id,
        backtest_id="derived-original",
    )
    with Session(id_engine) as session:
        row = session.query(BacktestResult).one()
        row.backtest_id = "derived-mutated"
        session.commit()
    with pytest.raises(ValueError, match="backtest_id was mutated"):
        id_store.save_derived_evidence(id_evidence, trial_id=id_trial_id)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("meta_result_backtest_id", "backtest_id was mutated"),
        ("backtest_ids_together", "stored content does not match its hash"),
        ("metric_fields", "metric_fields do not match"),
        ("sentinel", "sentinel is inconsistent"),
        ("schema_id", "unsupported evidence schema"),
        ("schema_version", "unsupported evidence schema"),
        ("metric_projection", "projection is inconsistent"),
    ],
)
def test_derived_loader_rejects_identity_schema_and_projection_mutations(
    mutation: str,
    message: str,
) -> None:
    eng = _engine()
    trial_id, _, experiment_id = _running_trial(eng)
    store = BacktestResultStore(eng)
    store.save_derived_evidence(
        _derived_evidence(experiment_id),
        trial_id=trial_id,
        backtest_id="derived-original",
    )

    with Session(eng) as session:
        row = session.query(BacktestResult).one()
        meta = json.loads(json.dumps(row.meta))
        if mutation == "meta_result_backtest_id":
            meta["result_backtest_id"] = "derived-mutated"
            row.meta = meta
        elif mutation == "backtest_ids_together":
            row.backtest_id = "derived-mutated"
            meta["result_backtest_id"] = "derived-mutated"
            row.meta = meta
        elif mutation == "metric_fields":
            meta["metric_fields"] = [*meta["metric_fields"], "profit_factor"]
            row.meta = meta
        elif mutation == "sentinel":
            meta["schema_initial_capital_sentinel"] = True
            row.meta = meta
        elif mutation == "schema_id":
            meta["evidence_schema_id"] = "mutated.schema"
            row.meta = meta
        elif mutation == "schema_version":
            meta["evidence_schema_version"] = 2
            row.meta = meta
        elif mutation == "metric_projection":
            row.total_return_pct = 99
        else:  # pragma: no cover - exhaustive parameter list
            raise AssertionError(mutation)
        session.commit()

    with pytest.raises((RuntimeError, ValueError), match=message):
        store.load_derived_payload(trial_id)


def test_save_derived_evidence_rejects_missing_nonrunning_and_identity_mismatches() -> None:
    eng = _engine()
    trial_id, _, experiment_id = _running_trial(eng, running=False)
    evidence = _derived_evidence(experiment_id)
    store = BacktestResultStore(eng)

    with pytest.raises(ValueError, match="not registered"):
        store.save_derived_evidence(evidence, trial_id="f" * 64)
    with pytest.raises(ValueError, match="must be running"):
        store.save_derived_evidence(evidence, trial_id=trial_id)

    BacktestTrialStore(eng).mark_running(trial_id)
    with pytest.raises(ValueError, match="strategy_semver"):
        store.save_derived_evidence(
            replace(evidence, strategy_semver="2.0.0"),
            trial_id=trial_id,
        )
    with pytest.raises(ValueError, match="experiment_id"):
        store.save_derived_evidence(
            replace(evidence, experiment_id=experiment_id + 1),
            trial_id=trial_id,
        )
    with pytest.raises(ValueError, match="strategy_id"):
        store.save_derived_evidence(
            replace(evidence, strategy_id="another_strategy"),
            trial_id=trial_id,
        )

    with Session(eng) as session:
        trial = session.get(BacktestTrial, trial_id)
        assert trial is not None
        assert trial.status == "running"
        assert session.query(BacktestResult).count() == 0


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"estimate": float("nan")}, "non-finite"),
        ({"api_key": "redacted"}, "sensitive field"),
        ({"source": "postgresql://user:password@example.invalid/db"}, "secret-like value"),
        ({"token": {"nested": "not-even-a-token"}}, "sensitive field"),
    ],
)
def test_save_derived_evidence_rejects_nan_and_secrets_atomically(
    payload: dict[str, object],
    message: str,
) -> None:
    eng = _engine()
    trial_id, _, experiment_id = _running_trial(eng)
    evidence = replace(_derived_evidence(experiment_id), payload=payload)

    with pytest.raises(ValueError, match=message):
        BacktestResultStore(eng).save_derived_evidence(evidence, trial_id=trial_id)

    with Session(eng) as session:
        trial = session.get(BacktestTrial, trial_id)
        assert trial is not None
        assert trial.status == "running"
        assert trial.result_id is None
        assert session.query(BacktestResult).count() == 0


def test_save_derived_evidence_rejects_invalid_curve_and_trade_counts() -> None:
    eng = _engine()
    trial_id, _, experiment_id = _running_trial(eng)
    evidence = _derived_evidence(experiment_id)
    store = BacktestResultStore(eng)

    invalid_curve = replace(
        evidence,
        equity_curve=((datetime(2022, 1, 1), 10_000.0),),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        store.save_derived_evidence(invalid_curve, trial_id=trial_id)

    invalid_counts = replace(
        evidence,
        metrics=DerivedEvidenceMetrics(total_trades=2, winning_trades=2, losing_trades=1),
    )
    with pytest.raises(ValueError, match="cannot exceed"):
        store.save_derived_evidence(invalid_counts, trial_id=trial_id)

    nonfinite_metric = replace(
        evidence,
        metrics=DerivedEvidenceMetrics(sharpe_ratio=float("inf")),
    )
    with pytest.raises(ValueError, match="must be finite"):
        store.save_derived_evidence(nonfinite_metric, trial_id=trial_id)

    nonfinite_curve = replace(
        evidence,
        equity_curve=((datetime(2022, 1, 1, tzinfo=UTC), float("-inf")),),
    )
    with pytest.raises(ValueError, match=r"equity.*finite"):
        store.save_derived_evidence(nonfinite_curve, trial_id=trial_id)


def test_save_derived_evidence_rejects_non_string_payload_keys() -> None:
    eng = _engine()
    trial_id, _, experiment_id = _running_trial(eng)
    evidence = replace(
        _derived_evidence(experiment_id),
        payload={1: "invalid"},  # type: ignore[dict-item]
    )

    with pytest.raises(TypeError, match="non-string object key"):
        BacktestResultStore(eng).save_derived_evidence(evidence, trial_id=trial_id)
