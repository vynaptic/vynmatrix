"""Persist exact-version deterministic validation results and trial links."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from dev_cli.validation.persistence.backtest_manifest_store import (
    canonical_manifest_bytes,
    manifest_sha256,
)
from dev_cli.validation.persistence.backtest_trial_store import BacktestTrialStatus
from lib_application.db.models import (
    BacktestExperiment,
    BacktestResult,
    BacktestTrial,
    Strategy,
    StrategyVersion,
)
from lib_common.asset_classes import CANONICAL_ASSET_CLASSES

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from dev_cli.validation.backtest.engine import BacktestReport


def _dec(value: float, places: int) -> Decimal:
    """Quantize a float to a fixed-scale Decimal for the Numeric columns."""

    quant = Decimal(10) ** -places
    return Decimal(str(value)).quantize(quant)


_DERIVED_EVIDENCE_SCHEMA_ID: Final = "vynmatrix.backtest.derived-evidence"
_DERIVED_EVIDENCE_SCHEMA_VERSION: Final = 1
_REPORT_EVIDENCE_SCHEMA_ID: Final = "vynmatrix.backtest.report-evidence"
_REPORT_EVIDENCE_SCHEMA_VERSION: Final = 1
_REPORT_HASH_META_KEYS: Final = frozenset(
    {
        "report_evidence_schema_id",
        "report_evidence_schema_version",
        "report_sha256",
        "result_backtest_id",
        "strategy_semver",
    }
)
_EQUITY_POINT_LENGTH: Final = 2
_TIMEFRAME_MAX_LENGTH: Final = 20
_SUPPORTED_ASSET_CLASSES: Final = CANONICAL_ASSET_CLASSES
_SENSITIVE_KEY_RE: Final = re.compile(
    r"(?:^|_)(?:api_key|apikey|password|passwd|pwd|secret|access_token|refresh_token|"
    r"auth_token|token|private_key|client_secret|credentials?|database_url|db_url|dsn|"
    r"connection_string)(?:_|$)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RES: Final = (
    re.compile(r"[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:^|\s)(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|xox[baprs]-\S+)"),
    re.compile(r"(?:^|\s)Bearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"(?:^|\s)AKIA[0-9A-Z]{16}(?:\s|$)"),
)


@dataclass(frozen=True, slots=True)
class DerivedEvidenceMetrics:
    """Optional metrics that a deterministic derived analysis actually computed.

    Omitted values remain SQL ``NULL``. In particular, callers must not invent
    trade counts or trade statistics for analyses such as PBO or power studies.
    """

    initial_capital: float | None = None
    total_return_pct: float | None = None
    cagr_pct: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    max_drawdown_pct: float | None = None
    win_rate_pct: float | None = None
    profit_factor: float | None = None
    avg_win_pct: float | None = None
    avg_loss_pct: float | None = None
    total_trades: int | None = None
    winning_trades: int | None = None
    losing_trades: int | None = None
    total_signals: int | None = None
    long_signals: int | None = None
    short_signals: int | None = None
    correct_predictions: int | None = None
    prediction_accuracy_pct: float | None = None
    final_equity: float | None = None
    peak_equity: float | None = None
    fees_paid: float | None = None


@dataclass(frozen=True, slots=True)
class DerivedBacktestEvidence:
    """Frozen input for non-report evidence linked to one immutable trial.

    ``payload`` contains analysis-specific evidence (for example portfolio
    composition, event-study estimates, PBO ranks, or power curves). An optional
    equity curve and only the metrics genuinely calculated from it may accompany
    that payload.
    """

    strategy_id: str
    strategy_semver: str
    experiment_id: int
    evidence_kind: str
    symbol: str
    asset_class: str
    timeframe: str | None
    start_date: date
    end_date: date
    payload: dict[str, Any]
    metrics: DerivedEvidenceMetrics | None = None
    equity_curve: tuple[tuple[datetime, float], ...] = ()


class BacktestResultStore:
    """Persist reports only for a running, exact-version registered trial."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(
        self,
        report: BacktestReport,
        *,
        trial_id: str,
        asset_class: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        backtest_id: str | None = None,
        meta: dict[str, Any] | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> str:
        """Persist a report and atomically complete its registered trial.

        Every idempotent resume recomputes and compares the complete report,
        trades, and equity curve against an immutable hash.
        """

        bid = backtest_id or trial_id
        curve = report.equity_curve
        if start_date is None:
            if not curve:
                msg = "start_date is required when the equity curve is empty"
                raise ValueError(msg)
            start_date = curve[0][0].date()
        if end_date is None:
            end_date = curve[-1][0].date() if curve else start_date

        metrics = report.metrics
        finished_at = completed_at or datetime.now(tz=UTC)
        with Session(self._engine) as session:
            trial = session.execute(
                select(BacktestTrial).where(BacktestTrial.trial_id == trial_id).with_for_update()
            ).scalar_one_or_none()
            if trial is None:
                msg = f"backtest trial is not registered: {trial_id}"
                raise ValueError(msg)

            if trial.status not in {
                BacktestTrialStatus.RUNNING.value,
                BacktestTrialStatus.COMPLETED.value,
            }:
                msg = f"backtest trial must be running before result persistence: {trial.status}"
                raise ValueError(msg)

            strategy = session.get(Strategy, trial.strategy_id)
            version = session.get(StrategyVersion, trial.strat_ver_id)
            if strategy is None or version is None or version.strategy_id != trial.strategy_id:
                msg = "trial does not reference an existing exact strategy/version"
                raise ValueError(msg)
            if asset_class is not None and strategy.asset_class not in (None, asset_class):
                msg = "result asset_class does not match the registered strategy"
                raise ValueError(msg)
            resolved_asset_class = asset_class or strategy.asset_class
            if resolved_asset_class is None:
                msg = "asset_class is required when the registered strategy has none"
                raise ValueError(msg)

            result_meta = dict(meta or {})
            for key, value in (
                ("manifest_hash", trial.manifest_hash),
                ("trial_id", trial.trial_id),
            ):
                if key in result_meta and result_meta[key] != value:
                    msg = f"result metadata cannot override registered {key}"
                    raise ValueError(msg)
                result_meta[key] = value

            result_meta, report_sha256 = self._prepare_verified_report_meta(
                report=report,
                trial=trial,
                strategy_semver=version.semver,
                asset_class=resolved_asset_class,
                start_date=start_date,
                end_date=end_date,
                backtest_id=bid,
                result_meta=result_meta,
            )
            if trial.status == BacktestTrialStatus.COMPLETED.value:
                return self._existing_verified_report_backtest_id(
                    session,
                    trial,
                    requested_backtest_id=backtest_id,
                    expected_sha256=report_sha256,
                )

            result = BacktestResult(
                strategy_id=trial.strategy_id,
                strat_ver_id=trial.strat_ver_id,
                experiment_id=trial.experiment_id,
                backtest_id=bid,
                symbol=report.config.symbol,
                asset_class=resolved_asset_class,
                start_date=start_date,
                end_date=end_date,
                initial_capital=_dec(metrics.initial_capital, 2),
                timeframe=f"{report.config.consolidation_minutes}m",
                parameters=trial.parameters,
                total_return_pct=_dec(metrics.total_return_pct, 4),
                cagr_pct=_dec(metrics.cagr_pct, 4),
                sharpe_ratio=_dec(metrics.sharpe_ratio, 4),
                sortino_ratio=_dec(metrics.sortino_ratio, 4),
                max_drawdown_pct=_dec(metrics.max_drawdown_pct, 4),
                win_rate_pct=_dec(metrics.win_rate_pct, 4),
                profit_factor=_dec(metrics.profit_factor, 4),
                total_trades=metrics.total_trades,
                winning_trades=metrics.winning_trades,
                losing_trades=metrics.losing_trades,
                total_signals=report.signals_emitted,
                final_equity=_dec(metrics.final_equity, 2),
                peak_equity=_dec(metrics.peak_equity, 2),
                fees_paid=_dec(sum(trade.fees for trade in report.trades), 2),
                engine="internal",
                trades_json=self._report_trade_rows(report),
                equity_curve=self._report_curve_rows(report),
                meta=result_meta,
                started_at=started_at or trial.started_at,
                completed_at=finished_at,
            )
            session.add(result)
            session.flush()
            trial.status = BacktestTrialStatus.COMPLETED.value
            trial.result_id = result.result_id
            trial.completed_at = finished_at
            trial.updated_at = finished_at
            session.commit()
        return bid

    def save_derived_evidence(
        self,
        evidence: DerivedBacktestEvidence,
        *,
        trial_id: str,
        backtest_id: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> str:
        """Atomically persist deterministic derived evidence and complete a trial.

        This method deliberately stores no trade list. Metrics that were not
        supplied remain null. ``backtest_results.initial_capital`` predates this
        evidence path and is non-nullable, so a zero schema sentinel is used only
        when the metric is not applicable; ``meta.metric_fields`` records exactly
        which values were supplied.
        """

        if not isinstance(evidence, DerivedBacktestEvidence):
            msg = "evidence must be DerivedBacktestEvidence"
            raise TypeError(msg)

        bid = backtest_id or trial_id
        self._reject_secrets(
            {
                "backtest_id": bid,
                "evidence_kind": evidence.evidence_kind,
                "strategy_id": evidence.strategy_id,
                "strategy_semver": evidence.strategy_semver,
                "symbol": evidence.symbol,
                "timeframe": evidence.timeframe,
                "trial_id": trial_id,
            },
            path="identity",
        )
        finished_at = completed_at or datetime.now(tz=UTC)
        with Session(self._engine) as session:
            trial = session.execute(
                select(BacktestTrial).where(BacktestTrial.trial_id == trial_id).with_for_update()
            ).scalar_one_or_none()
            if trial is None:
                msg = f"backtest trial is not registered: {trial_id}"
                raise ValueError(msg)

            strategy = session.get(Strategy, trial.strategy_id)
            version = session.get(StrategyVersion, trial.strat_ver_id)
            experiment = session.get(BacktestExperiment, trial.experiment_id)
            self._require_derived_identity(
                evidence,
                trial=trial,
                strategy=strategy,
                version=version,
                experiment=experiment,
            )
            payload, metrics, curve = self._normalize_derived_evidence(evidence)
            metric_fields = sorted(metrics)
            schema_initial_capital_sentinel = "initial_capital" not in metrics
            envelope = {
                "asset_class": evidence.asset_class,
                "backtest_id": bid,
                "end_date": evidence.end_date.isoformat(),
                "equity_curve": curve,
                "evidence_kind": evidence.evidence_kind,
                "evidence_schema_id": _DERIVED_EVIDENCE_SCHEMA_ID,
                "evidence_schema_version": _DERIVED_EVIDENCE_SCHEMA_VERSION,
                "experiment_id": evidence.experiment_id,
                "manifest_hash": trial.manifest_hash,
                "metric_fields": metric_fields,
                "metrics": metrics,
                "parameters": trial.parameters,
                "payload": payload,
                "schema_initial_capital_sentinel": schema_initial_capital_sentinel,
                "start_date": evidence.start_date.isoformat(),
                "strat_ver_id": trial.strat_ver_id,
                "strategy_id": evidence.strategy_id,
                "strategy_semver": evidence.strategy_semver,
                "symbol": evidence.symbol,
                "timeframe": evidence.timeframe,
                "trial_id": trial_id,
            }
            evidence_sha256 = manifest_sha256(envelope)

            if trial.status == BacktestTrialStatus.COMPLETED.value:
                return self._existing_derived_backtest_id(
                    session,
                    trial,
                    requested_backtest_id=backtest_id,
                    expected_sha256=evidence_sha256,
                )
            if trial.status != BacktestTrialStatus.RUNNING.value:
                msg = f"backtest trial must be running before result persistence: {trial.status}"
                raise ValueError(msg)

            result = BacktestResult(
                strategy_id=trial.strategy_id,
                strat_ver_id=trial.strat_ver_id,
                experiment_id=trial.experiment_id,
                backtest_id=bid,
                symbol=evidence.symbol,
                asset_class=evidence.asset_class,
                start_date=evidence.start_date,
                end_date=evidence.end_date,
                initial_capital=self._derived_decimal(metrics, "initial_capital", 2)
                or Decimal("0.00"),
                timeframe=evidence.timeframe,
                parameters=trial.parameters,
                total_return_pct=self._derived_decimal(metrics, "total_return_pct", 4),
                cagr_pct=self._derived_decimal(metrics, "cagr_pct", 4),
                sharpe_ratio=self._derived_decimal(metrics, "sharpe_ratio", 4),
                sortino_ratio=self._derived_decimal(metrics, "sortino_ratio", 4),
                max_drawdown_pct=self._derived_decimal(metrics, "max_drawdown_pct", 4),
                win_rate_pct=self._derived_decimal(metrics, "win_rate_pct", 4),
                profit_factor=self._derived_decimal(metrics, "profit_factor", 4),
                avg_win_pct=self._derived_decimal(metrics, "avg_win_pct", 4),
                avg_loss_pct=self._derived_decimal(metrics, "avg_loss_pct", 4),
                total_trades=metrics.get("total_trades"),
                winning_trades=metrics.get("winning_trades"),
                losing_trades=metrics.get("losing_trades"),
                total_signals=metrics.get("total_signals"),
                long_signals=metrics.get("long_signals"),
                short_signals=metrics.get("short_signals"),
                correct_predictions=metrics.get("correct_predictions"),
                prediction_accuracy_pct=self._derived_decimal(
                    metrics, "prediction_accuracy_pct", 4
                ),
                final_equity=self._derived_decimal(metrics, "final_equity", 2),
                peak_equity=self._derived_decimal(metrics, "peak_equity", 2),
                fees_paid=self._derived_decimal(metrics, "fees_paid", 2),
                engine="custom",
                trades_json=None,
                equity_curve=curve or None,
                meta={
                    "evidence_kind": evidence.evidence_kind,
                    "evidence_metrics": metrics,
                    "evidence_payload": payload,
                    "evidence_schema_id": _DERIVED_EVIDENCE_SCHEMA_ID,
                    "evidence_schema_version": _DERIVED_EVIDENCE_SCHEMA_VERSION,
                    "evidence_sha256": evidence_sha256,
                    "manifest_hash": trial.manifest_hash,
                    "metric_fields": metric_fields,
                    "result_backtest_id": bid,
                    "schema_initial_capital_sentinel": schema_initial_capital_sentinel,
                    "strategy_semver": evidence.strategy_semver,
                    "trial_id": trial_id,
                },
                started_at=started_at or trial.started_at,
                completed_at=finished_at,
            )
            session.add(result)
            session.flush()
            trial.status = BacktestTrialStatus.COMPLETED.value
            trial.result_id = result.result_id
            trial.completed_at = finished_at
            trial.updated_at = finished_at
            session.commit()
        return bid

    def load_derived_payload(
        self,
        trial_id: str,
        *,
        expected_evidence_kind: str | None = None,
    ) -> dict[str, Any]:
        """Load a completed derived payload after verifying its immutable envelope.

        Derived campaign stages may consume only previously checkpointed evidence,
        never mutable ORM objects or a second strategy run.  The completed result's
        stored content hash is recomputed before a fresh JSON copy of the payload is
        returned.  Callers can pin the expected evidence kind to prevent one stage
        from accidentally consuming a different custom-result family.
        """

        if not isinstance(trial_id, str) or not trial_id.strip():
            msg = "trial_id must be a non-blank string"
            raise ValueError(msg)
        if expected_evidence_kind is not None and (
            not isinstance(expected_evidence_kind, str) or not expected_evidence_kind.strip()
        ):
            msg = "expected_evidence_kind must be a non-blank string when supplied"
            raise ValueError(msg)

        with Session(self._engine) as session:
            trial = session.execute(
                select(BacktestTrial).where(BacktestTrial.trial_id == trial_id)
            ).scalar_one_or_none()
            if trial is None:
                msg = f"backtest trial is not registered: {trial_id}"
                raise ValueError(msg)
            if trial.status != BacktestTrialStatus.COMPLETED.value:
                msg = f"derived evidence trial is not completed: {trial.status}"
                raise ValueError(msg)
            if trial.result_id is None:
                msg = "completed backtest trial has no result link"
                raise RuntimeError(msg)
            result = session.get(BacktestResult, trial.result_id)
            if result is None or not isinstance(result.meta, dict):
                msg = "completed derived result is missing or has invalid metadata"
                raise RuntimeError(msg)
            stored_sha256 = result.meta.get("evidence_sha256")
            if not isinstance(stored_sha256, str):
                msg = "completed derived result has no evidence hash"
                raise TypeError(msg)
            self._existing_derived_backtest_id(
                session,
                trial,
                requested_backtest_id=None,
                expected_sha256=stored_sha256,
            )
            evidence_kind = result.meta.get("evidence_kind")
            if expected_evidence_kind is not None and evidence_kind != expected_evidence_kind:
                msg = (
                    "completed derived result evidence_kind differs from the "
                    f"required source: {evidence_kind!r}"
                )
                raise ValueError(msg)
            payload = result.meta.get("evidence_payload")
            if not isinstance(payload, dict):
                msg = "completed derived result has no JSON-object evidence payload"
                raise TypeError(msg)
            copied = json.loads(canonical_manifest_bytes({"payload": payload}))["payload"]
            if not isinstance(copied, dict):  # pragma: no cover - canonical serializer invariant
                msg = "completed derived result payload did not normalize to an object"
                raise TypeError(msg)
            return copied

    def verified_report_sha256(self, trial_id: str) -> str:
        """Verify and return one completed campaign report's content hash."""

        if not isinstance(trial_id, str) or not trial_id.strip():
            msg = "trial_id must be a non-blank string"
            raise ValueError(msg)
        with Session(self._engine) as session:
            trial = session.execute(
                select(BacktestTrial).where(BacktestTrial.trial_id == trial_id)
            ).scalar_one_or_none()
            if trial is None:
                msg = f"backtest trial is not registered: {trial_id}"
                raise ValueError(msg)
            if trial.status != BacktestTrialStatus.COMPLETED.value:
                msg = f"report evidence trial is not completed: {trial.status}"
                raise ValueError(msg)
            stored_sha256 = self._stored_report_sha256(session, trial)
            self._existing_verified_report_backtest_id(
                session,
                trial,
                requested_backtest_id=None,
                expected_sha256=stored_sha256,
            )
            return stored_sha256

    def load_verified_report_payload(self, trial_id: str) -> dict[str, Any]:
        """Load one completed campaign report after full content verification."""

        evidence = self.load_verified_report_evidence(trial_id)
        payload = evidence.get("report")
        if not isinstance(payload, dict):  # pragma: no cover - checked by the strict loader
            msg = "completed report result has no JSON-object report payload"
            raise TypeError(msg)
        return payload

    def load_verified_report_evidence(self, trial_id: str) -> dict[str, Any]:
        """Return a detached, fully verified report/equity/trade evidence envelope.

        The returned JSON object is a fresh copy.  Mutating it cannot alter the
        append-only stored result, and every subsequent load verifies the exact
        report summary, complete trade ledger, equity curve, parameters, and
        exact-version identity before returning another copy.
        """

        if not isinstance(trial_id, str) or not trial_id.strip():
            msg = "trial_id must be a non-blank string"
            raise ValueError(msg)
        with Session(self._engine) as session:
            trial = session.execute(
                select(BacktestTrial).where(BacktestTrial.trial_id == trial_id)
            ).scalar_one_or_none()
            if trial is None:
                msg = f"backtest trial is not registered: {trial_id}"
                raise ValueError(msg)
            if trial.status != BacktestTrialStatus.COMPLETED.value:
                msg = f"report evidence trial is not completed: {trial.status}"
                raise ValueError(msg)
            self._existing_verified_report_backtest_id(
                session,
                trial,
                requested_backtest_id=None,
                expected_sha256=self._stored_report_sha256(session, trial),
            )
            if trial.result_id is None:  # pragma: no cover - checked above
                msg = "completed backtest trial has no result link"
                raise RuntimeError(msg)
            result = session.get(BacktestResult, trial.result_id)
            if result is None or not isinstance(result.meta, dict):  # pragma: no cover
                msg = "completed report result is missing or has invalid metadata"
                raise RuntimeError(msg)
            envelope = self._stored_report_evidence_envelope(result)
            context = envelope.get("meta_context")
            if not isinstance(context, dict):  # pragma: no cover - built from metadata above
                msg = "completed report result has invalid evidence context"
                raise TypeError(msg)
            report = context.get("report")
            if not isinstance(report, dict):
                msg = "completed report result has no JSON-object report payload"
                raise TypeError(msg)
            trades = envelope.get("trades")
            equity_curve = envelope.get("equity_curve")
            if not isinstance(trades, list):
                msg = "completed report result has no JSON-array trade ledger"
                raise TypeError(msg)
            if not isinstance(equity_curve, list):
                msg = "completed report result has no JSON-array equity curve"
                raise TypeError(msg)

            public_context = {key: value for key, value in context.items() if key != "report"}
            evidence = {
                **envelope,
                "meta_context": public_context,
                "report": report,
                "report_sha256": result.meta.get("report_sha256"),
            }
            copied = json.loads(canonical_manifest_bytes({"evidence": evidence}))["evidence"]
            if not isinstance(copied, dict):  # pragma: no cover - serializer invariant
                msg = "completed report evidence did not normalize to an object"
                raise TypeError(msg)
            return copied

    @staticmethod
    def _require_derived_identity(
        evidence: DerivedBacktestEvidence,
        *,
        trial: BacktestTrial,
        strategy: Strategy | None,
        version: StrategyVersion | None,
        experiment: BacktestExperiment | None,
    ) -> None:
        if strategy is None or version is None or version.strategy_id != trial.strategy_id:
            msg = "trial does not reference an existing exact strategy/version"
            raise ValueError(msg)
        if experiment is None or experiment.strategy_id != trial.strategy_id:
            msg = "trial does not reference an existing matching experiment"
            raise ValueError(msg)
        if evidence.strategy_id != trial.strategy_id:
            msg = "derived evidence strategy_id does not match the registered trial"
            raise ValueError(msg)
        if evidence.strategy_semver != version.semver:
            msg = "derived evidence strategy_semver does not match the registered trial"
            raise ValueError(msg)
        if evidence.experiment_id != trial.experiment_id:
            msg = "derived evidence experiment_id does not match the registered trial"
            raise ValueError(msg)
        if strategy.asset_class not in (None, evidence.asset_class):
            msg = "derived evidence asset_class does not match the registered strategy"
            raise ValueError(msg)

    @classmethod
    def _normalize_derived_evidence(
        cls,
        evidence: DerivedBacktestEvidence,
    ) -> tuple[dict[str, Any], dict[str, float | int], list[list[str | float]]]:
        cls._validate_derived_context(evidence)
        payload = cls._normalize_payload(evidence.payload)
        metrics = cls._normalize_metrics(evidence.metrics)
        curve = cls._normalize_equity_curve(evidence)
        return payload, metrics, curve

    @staticmethod
    def _validate_derived_context(evidence: DerivedBacktestEvidence) -> None:
        for field, value, maximum in (
            ("strategy_id", evidence.strategy_id, 50),
            ("strategy_semver", evidence.strategy_semver, 20),
            ("evidence_kind", evidence.evidence_kind, 64),
            ("symbol", evidence.symbol, 50),
        ):
            if not value or len(value) > maximum:
                msg = f"{field} must contain 1-{maximum} characters"
                raise ValueError(msg)
        if evidence.experiment_id <= 0:
            msg = "experiment_id must be positive"
            raise ValueError(msg)
        if evidence.asset_class not in _SUPPORTED_ASSET_CLASSES:
            msg = f"unsupported derived evidence asset_class: {evidence.asset_class}"
            raise ValueError(msg)
        if evidence.timeframe is not None and (
            not evidence.timeframe or len(evidence.timeframe) > _TIMEFRAME_MAX_LENGTH
        ):
            msg = "timeframe must be None or contain 1-20 characters"
            raise ValueError(msg)
        if type(evidence.start_date) is not date or type(evidence.end_date) is not date:
            msg = "derived evidence dates must be date values"
            raise TypeError(msg)
        if evidence.end_date < evidence.start_date:
            msg = "derived evidence end_date cannot precede start_date"
            raise ValueError(msg)
        if not isinstance(evidence.payload, dict):
            msg = "derived evidence payload must be a JSON object"
            raise TypeError(msg)

    @classmethod
    def _normalize_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        cls._reject_secrets(value, path="payload")
        payload_bytes = canonical_manifest_bytes({"payload": value})
        payload = json.loads(payload_bytes)["payload"]
        if not isinstance(payload, dict):  # Defensive against future serializer changes.
            msg = "derived evidence payload must normalize to a JSON object"
            raise TypeError(msg)
        return payload

    @classmethod
    def _normalize_metrics(
        cls,
        value: DerivedEvidenceMetrics | None,
    ) -> dict[str, float | int]:
        metrics: dict[str, float | int] = {}
        if value is not None:
            if not isinstance(value, DerivedEvidenceMetrics):
                msg = "metrics must be DerivedEvidenceMetrics"
                raise TypeError(msg)
            for key, metric in asdict(value).items():
                if metric is None:
                    continue
                if isinstance(metric, bool) or not isinstance(metric, (int, float)):
                    msg = f"derived metric {key} must be numeric"
                    raise TypeError(msg)
                try:
                    finite = math.isfinite(float(metric))
                except OverflowError:
                    finite = False
                if not finite:
                    msg = f"derived metric {key} must be finite"
                    raise ValueError(msg)
                metrics[key] = metric
            canonical_manifest_bytes({"metrics": metrics})
            cls._validate_metric_counts(metrics)
        return metrics

    @staticmethod
    def _normalize_equity_curve(evidence: DerivedBacktestEvidence) -> list[list[str | float]]:
        curve: list[list[str | float]] = []
        previous: datetime | None = None
        for index, point in enumerate(evidence.equity_curve):
            if not isinstance(point, tuple) or len(point) != _EQUITY_POINT_LENGTH:
                msg = f"equity_curve[{index}] must be a (timestamp, equity) tuple"
                raise TypeError(msg)
            timestamp, equity = point
            if (
                not isinstance(timestamp, datetime)
                or timestamp.tzinfo is None
                or timestamp.utcoffset() is None
            ):
                msg = f"equity_curve[{index}] timestamp must be timezone-aware"
                raise ValueError(msg)
            normalized_timestamp = timestamp.astimezone(UTC)
            if previous is not None and normalized_timestamp <= previous:
                msg = "equity_curve timestamps must be strictly increasing"
                raise ValueError(msg)
            if not evidence.start_date <= normalized_timestamp.date() <= evidence.end_date:
                msg = f"equity_curve[{index}] timestamp falls outside the evidence dates"
                raise ValueError(msg)
            if isinstance(equity, bool) or not isinstance(equity, (int, float)):
                msg = f"equity_curve[{index}] equity must be numeric"
                raise TypeError(msg)
            try:
                normalized_equity = float(equity)
            except OverflowError:
                normalized_equity = math.inf
            if not math.isfinite(normalized_equity):
                msg = f"equity_curve[{index}] equity must be finite"
                raise ValueError(msg)
            curve.append([normalized_timestamp.isoformat(), normalized_equity])
            previous = normalized_timestamp
        return curve

    @staticmethod
    def _validate_metric_counts(metrics: dict[str, float | int]) -> None:
        count_fields = (
            "total_trades",
            "winning_trades",
            "losing_trades",
            "total_signals",
            "long_signals",
            "short_signals",
            "correct_predictions",
        )
        for field in count_fields:
            value = metrics.get(field)
            if value is not None and (not isinstance(value, int) or value < 0):
                msg = f"derived metric {field} must be a non-negative integer"
                raise ValueError(msg)
        total = metrics.get("total_trades")
        winning = metrics.get("winning_trades")
        losing = metrics.get("losing_trades")
        if (
            isinstance(total, int)
            and isinstance(winning, int)
            and isinstance(losing, int)
            and winning + losing > total
        ):
            msg = "winning_trades plus losing_trades cannot exceed total_trades"
            raise ValueError(msg)

    @classmethod
    def _reject_secrets(cls, value: Any, *, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    msg = f"derived evidence contains a non-string object key at {path}"
                    raise TypeError(msg)
                normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
                if _SENSITIVE_KEY_RE.search(normalized_key):
                    msg = f"derived evidence cannot persist sensitive field {path}.{key}"
                    raise ValueError(msg)
                cls._reject_secrets(item, path=f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                cls._reject_secrets(item, path=f"{path}[{index}]")
        elif isinstance(value, str) and any(
            pattern.search(value) for pattern in _SENSITIVE_VALUE_RES
        ):
            msg = f"derived evidence cannot persist a secret-like value at {path}"
            raise ValueError(msg)

    @staticmethod
    def _derived_decimal(
        metrics: dict[str, float | int],
        key: str,
        places: int,
    ) -> Decimal | None:
        value = metrics.get(key)
        return None if value is None else _dec(float(value), places)

    @classmethod
    def _prepare_verified_report_meta(
        cls,
        *,
        report: BacktestReport,
        trial: BacktestTrial,
        strategy_semver: str,
        asset_class: str,
        start_date: date,
        end_date: date,
        backtest_id: str,
        result_meta: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        reserved_keys = sorted(_REPORT_HASH_META_KEYS.intersection(result_meta))
        if reserved_keys:
            msg = "result metadata cannot supply reserved verification fields: " + ", ".join(
                reserved_keys
            )
            raise ValueError(msg)
        report_payload = report.to_dict()
        supplied_report = result_meta.get("report")
        if supplied_report is not None and canonical_manifest_bytes(
            {"report": supplied_report}
        ) != canonical_manifest_bytes({"report": report_payload}):
            msg = "result metadata report differs from the supplied report"
            raise ValueError(msg)
        context = {**result_meta, "report": report_payload}
        report_envelope = cls._report_evidence_envelope(
            report=report,
            trial=trial,
            strategy_semver=strategy_semver,
            asset_class=asset_class,
            start_date=start_date,
            end_date=end_date,
            backtest_id=backtest_id,
            context=context,
        )
        report_sha256 = manifest_sha256(report_envelope)
        return (
            {
                **context,
                "report_evidence_schema_id": _REPORT_EVIDENCE_SCHEMA_ID,
                "report_evidence_schema_version": _REPORT_EVIDENCE_SCHEMA_VERSION,
                "report_sha256": report_sha256,
                "result_backtest_id": backtest_id,
                "strategy_semver": strategy_semver,
            },
            report_sha256,
        )

    @staticmethod
    def _report_trade_rows(report: BacktestReport) -> list[dict[str, Any]]:
        return [
            {
                "symbol": trade.symbol,
                "side": trade.side,
                "entry_ts": trade.entry_ts.isoformat(),
                "exit_ts": trade.exit_ts.isoformat(),
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "quantity": trade.quantity,
                "fees": trade.fees,
                "pnl": trade.pnl,
                "gross_pnl": trade.gross_pnl,
                "exit_reason": trade.exit_reason,
                "holding_bars": trade.holding_bars,
                "entry_reference_price": trade.entry_reference_price,
                "exit_reference_price": trade.exit_reference_price,
                "cost_breakdown": trade.cost_breakdown.to_dict(),
            }
            for trade in report.trades
        ]

    @staticmethod
    def _report_curve_rows(report: BacktestReport) -> list[list[str | float]]:
        return [[timestamp.isoformat(), equity] for timestamp, equity in report.equity_curve]

    @classmethod
    def _report_evidence_envelope(
        cls,
        *,
        report: BacktestReport,
        trial: BacktestTrial,
        strategy_semver: str,
        asset_class: str,
        start_date: date,
        end_date: date,
        backtest_id: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "asset_class": asset_class,
            "backtest_id": backtest_id,
            "end_date": end_date.isoformat(),
            "equity_curve": cls._report_curve_rows(report),
            "experiment_id": trial.experiment_id,
            "meta_context": context,
            "parameters": trial.parameters,
            "report_evidence_schema_id": _REPORT_EVIDENCE_SCHEMA_ID,
            "report_evidence_schema_version": _REPORT_EVIDENCE_SCHEMA_VERSION,
            "start_date": start_date.isoformat(),
            "strat_ver_id": trial.strat_ver_id,
            "strategy_id": trial.strategy_id,
            "strategy_semver": strategy_semver,
            "symbol": report.config.symbol,
            "timeframe": f"{report.config.consolidation_minutes}m",
            "trades": cls._report_trade_rows(report),
            "trial_id": trial.trial_id,
        }

    @staticmethod
    def _stored_report_sha256(session: Session, trial: BacktestTrial) -> str:
        if trial.result_id is None:
            msg = "completed backtest trial has no result link"
            raise RuntimeError(msg)
        existing = session.get(BacktestResult, trial.result_id)
        if existing is None or not isinstance(existing.meta, dict):
            msg = "completed report result is missing or has invalid metadata"
            raise RuntimeError(msg)
        value = existing.meta.get("report_sha256")
        if not isinstance(value, str):
            msg = "completed report result has no content hash"
            raise TypeError(msg)
        return value

    @classmethod
    def _stored_report_evidence_envelope(
        cls,
        existing: BacktestResult,
    ) -> dict[str, Any]:
        if not isinstance(existing.meta, dict):
            msg = "completed report result has invalid metadata"
            raise TypeError(msg)
        if not isinstance(existing.trades_json, list):
            msg = "completed report result has no JSON-array trade ledger"
            raise TypeError(msg)
        if not isinstance(existing.equity_curve, list):
            msg = "completed report result has no JSON-array equity curve"
            raise TypeError(msg)
        context = {
            key: value for key, value in existing.meta.items() if key not in _REPORT_HASH_META_KEYS
        }
        return {
            "asset_class": existing.asset_class,
            "backtest_id": existing.backtest_id,
            "end_date": existing.end_date.isoformat(),
            "equity_curve": existing.equity_curve,
            "experiment_id": existing.experiment_id,
            "meta_context": context,
            "parameters": existing.parameters,
            "report_evidence_schema_id": existing.meta.get("report_evidence_schema_id"),
            "report_evidence_schema_version": existing.meta.get("report_evidence_schema_version"),
            "start_date": existing.start_date.isoformat(),
            "strat_ver_id": existing.strat_ver_id,
            "strategy_id": existing.strategy_id,
            "strategy_semver": existing.meta.get("strategy_semver"),
            "symbol": existing.symbol,
            "timeframe": existing.timeframe,
            "trades": existing.trades_json,
            "trial_id": existing.meta.get("trial_id"),
        }

    @classmethod
    def _existing_verified_report_backtest_id(
        cls,
        session: Session,
        trial: BacktestTrial,
        *,
        requested_backtest_id: str | None,
        expected_sha256: str,
    ) -> str:
        if trial.result_id is None:
            msg = "completed backtest trial has no result link"
            raise RuntimeError(msg)
        existing = session.get(BacktestResult, trial.result_id)
        if existing is None:
            msg = "completed backtest trial references a missing result"
            raise RuntimeError(msg)
        if existing.engine != "internal":
            msg = "completed trial cannot be replaced with report evidence"
            raise ValueError(msg)
        if not isinstance(existing.meta, dict):
            msg = "completed report result has invalid metadata"
            raise TypeError(msg)
        if (
            existing.strategy_id != trial.strategy_id
            or existing.strat_ver_id != trial.strat_ver_id
            or existing.experiment_id != trial.experiment_id
        ):
            msg = "completed report result linkage does not match its trial"
            raise RuntimeError(msg)
        if (
            existing.meta.get("trial_id") != trial.trial_id
            or existing.meta.get("manifest_hash") != trial.manifest_hash
        ):
            msg = "completed report result provenance does not match its trial"
            raise RuntimeError(msg)
        if existing.meta.get("result_backtest_id") != existing.backtest_id:
            msg = "completed report result backtest_id was mutated"
            raise ValueError(msg)
        if (
            existing.meta.get("report_evidence_schema_id") != _REPORT_EVIDENCE_SCHEMA_ID
            or existing.meta.get("report_evidence_schema_version")
            != _REPORT_EVIDENCE_SCHEMA_VERSION
        ):
            msg = "completed report result has an unsupported evidence schema"
            raise ValueError(msg)
        try:
            stored_sha256 = manifest_sha256(cls._stored_report_evidence_envelope(existing))
        except (AttributeError, TypeError, ValueError) as exc:
            msg = "completed report result cannot be content-verified"
            raise RuntimeError(msg) from exc
        actual_sha256 = existing.meta.get("report_sha256")
        if actual_sha256 != stored_sha256:
            msg = "completed report result stored content does not match its hash"
            raise RuntimeError(msg)
        cls._verify_report_metric_projection(existing)
        if requested_backtest_id is not None and existing.backtest_id != requested_backtest_id:
            msg = "completed trial cannot be linked to a different backtest_id"
            raise ValueError(msg)
        if actual_sha256 != expected_sha256:
            msg = "completed trial report evidence content hash cannot be changed"
            raise ValueError(msg)
        stored_backtest_id: str = existing.backtest_id
        return stored_backtest_id

    @classmethod
    def _verify_report_metric_projection(cls, existing: BacktestResult) -> None:
        if not isinstance(existing.meta, dict):  # pragma: no cover - checked by caller
            msg = "completed report result has invalid metadata"
            raise TypeError(msg)
        report = existing.meta.get("report")
        if not isinstance(report, dict):
            msg = "completed report result has no JSON-object report payload"
            raise TypeError(msg)

        cls._verify_report_decimal_projections(existing, report)
        cls._verify_report_count_projections(existing, report)
        cls._verify_report_fee_projection(existing)
        cls._verify_report_null_projections(existing)

    @staticmethod
    def _verify_report_decimal_projections(
        existing: BacktestResult,
        report: dict[str, Any],
    ) -> None:
        decimal_fields = {
            "initial_capital": 2,
            "total_return_pct": 4,
            "cagr_pct": 4,
            "sharpe_ratio": 4,
            "sortino_ratio": 4,
            "max_drawdown_pct": 4,
            "win_rate_pct": 4,
            "profit_factor": 4,
            "final_equity": 2,
            "peak_equity": 2,
        }
        for field, places in decimal_fields.items():
            value = report.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                msg = f"completed report result has invalid {field} evidence"
                raise TypeError(msg)
            if not math.isfinite(float(value)):
                msg = f"completed report result has non-finite {field} evidence"
                raise ValueError(msg)
            if getattr(existing, field) != _dec(float(value), places):
                msg = f"completed report result {field} projection is inconsistent"
                raise ValueError(msg)

    @staticmethod
    def _verify_report_count_projections(
        existing: BacktestResult,
        report: dict[str, Any],
    ) -> None:
        count_fields = {
            "total_trades": "total_trades",
            "winning_trades": "winning_trades",
            "losing_trades": "losing_trades",
            "total_signals": "signals_emitted",
        }
        for field, report_field in count_fields.items():
            value = report.get(report_field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                msg = f"completed report result has invalid {report_field} evidence"
                raise TypeError(msg)
            if getattr(existing, field) != value:
                msg = f"completed report result {field} projection is inconsistent"
                raise ValueError(msg)

    @staticmethod
    def _verify_report_fee_projection(existing: BacktestResult) -> None:
        if not isinstance(existing.trades_json, list):  # pragma: no cover - envelope check
            msg = "completed report result has no JSON-array trade ledger"
            raise TypeError(msg)
        trade_fees: list[float] = []
        for index, trade in enumerate(existing.trades_json):
            if not isinstance(trade, dict):
                msg = f"completed report trade ledger row {index} is not an object"
                raise TypeError(msg)
            value = trade.get("fees")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                msg = f"completed report trade ledger row {index} has invalid fees"
                raise TypeError(msg)
            fee = float(value)
            if not math.isfinite(fee):
                msg = f"completed report trade ledger row {index} has non-finite fees"
                raise ValueError(msg)
            trade_fees.append(fee)
        if existing.fees_paid != _dec(sum(trade_fees), 2):
            msg = "completed report result fees_paid projection is inconsistent"
            raise ValueError(msg)

    @staticmethod
    def _verify_report_null_projections(existing: BacktestResult) -> None:
        for field in (
            "avg_win_pct",
            "avg_loss_pct",
            "long_signals",
            "short_signals",
            "correct_predictions",
            "prediction_accuracy_pct",
        ):
            if getattr(existing, field) is not None:
                msg = f"completed report result {field} projection must be null"
                raise ValueError(msg)

    @classmethod
    def _existing_derived_backtest_id(
        cls,
        session: Session,
        trial: BacktestTrial,
        *,
        requested_backtest_id: str | None,
        expected_sha256: str,
    ) -> str:
        if trial.result_id is None:
            msg = "completed backtest trial has no result link"
            raise RuntimeError(msg)
        existing = session.get(BacktestResult, trial.result_id)
        if existing is None:
            msg = "completed backtest trial references a missing result"
            raise RuntimeError(msg)
        if existing.engine != "custom":
            msg = "completed trial cannot be replaced with derived evidence"
            raise ValueError(msg)
        if not isinstance(existing.meta, dict):
            msg = "completed derived result has invalid metadata"
            raise TypeError(msg)
        if (
            existing.strategy_id != trial.strategy_id
            or existing.strat_ver_id != trial.strat_ver_id
            or existing.experiment_id != trial.experiment_id
        ):
            msg = "completed derived result linkage does not match its trial"
            raise RuntimeError(msg)
        if existing.meta.get("result_backtest_id") != existing.backtest_id:
            msg = "completed derived result backtest_id was mutated"
            raise ValueError(msg)
        if (
            existing.meta.get("evidence_schema_id") != _DERIVED_EVIDENCE_SCHEMA_ID
            or existing.meta.get("evidence_schema_version") != _DERIVED_EVIDENCE_SCHEMA_VERSION
        ):
            msg = "completed derived result has an unsupported evidence schema"
            raise ValueError(msg)
        if (
            existing.meta.get("trial_id") != trial.trial_id
            or existing.meta.get("manifest_hash") != trial.manifest_hash
        ):
            msg = "completed derived result provenance does not match its trial"
            raise RuntimeError(msg)

        metrics = cls._validated_stored_derived_metrics(existing)
        metric_fields = sorted(metrics)
        if existing.meta.get("metric_fields") != metric_fields:
            msg = "completed derived result metric_fields do not match its metrics"
            raise ValueError(msg)
        sentinel = "initial_capital" not in metrics
        if existing.meta.get("schema_initial_capital_sentinel") is not sentinel:
            msg = "completed derived result initial-capital sentinel is inconsistent"
            raise ValueError(msg)
        cls._verify_derived_metric_projection(existing, metrics, sentinel=sentinel)

        actual_sha256 = existing.meta.get("evidence_sha256")
        try:
            stored_sha256 = manifest_sha256(
                {
                    "asset_class": existing.asset_class,
                    "backtest_id": existing.backtest_id,
                    "end_date": existing.end_date.isoformat(),
                    "equity_curve": existing.equity_curve or [],
                    "evidence_kind": existing.meta.get("evidence_kind"),
                    "evidence_schema_id": existing.meta.get("evidence_schema_id"),
                    "evidence_schema_version": existing.meta.get("evidence_schema_version"),
                    "experiment_id": existing.experiment_id,
                    "manifest_hash": existing.meta.get("manifest_hash"),
                    "metric_fields": metric_fields,
                    "metrics": metrics,
                    "parameters": existing.parameters,
                    "payload": existing.meta.get("evidence_payload"),
                    "schema_initial_capital_sentinel": sentinel,
                    "start_date": existing.start_date.isoformat(),
                    "strat_ver_id": existing.strat_ver_id,
                    "strategy_id": existing.strategy_id,
                    "strategy_semver": existing.meta.get("strategy_semver"),
                    "symbol": existing.symbol,
                    "timeframe": existing.timeframe,
                    "trial_id": existing.meta.get("trial_id"),
                }
            )
        except (AttributeError, TypeError, ValueError) as exc:
            msg = "completed derived result cannot be content-verified"
            raise RuntimeError(msg) from exc
        if actual_sha256 != stored_sha256:
            msg = "completed derived result stored content does not match its hash"
            raise RuntimeError(msg)
        if requested_backtest_id is not None and existing.backtest_id != requested_backtest_id:
            msg = "completed trial cannot be linked to a different backtest_id"
            raise ValueError(msg)
        if actual_sha256 != expected_sha256:
            msg = "completed trial derived evidence content hash cannot be changed"
            raise ValueError(msg)
        stored_backtest_id: str = existing.backtest_id
        return stored_backtest_id

    @classmethod
    def _validated_stored_derived_metrics(
        cls,
        existing: BacktestResult,
    ) -> dict[str, float | int]:
        if not isinstance(existing.meta, dict):  # pragma: no cover - checked by caller
            msg = "completed derived result has invalid metadata"
            raise TypeError(msg)
        raw_metrics = existing.meta.get("evidence_metrics")
        if not isinstance(raw_metrics, dict):
            msg = "completed derived result has invalid evidence metrics"
            raise TypeError(msg)
        allowed_fields = set(DerivedEvidenceMetrics.__dataclass_fields__)
        unknown_fields = sorted(set(raw_metrics).difference(allowed_fields))
        if unknown_fields:
            msg = "completed derived result has unsupported metric fields: " + ", ".join(
                unknown_fields
            )
            raise ValueError(msg)
        try:
            evidence_metrics = DerivedEvidenceMetrics(**raw_metrics)
            metrics = cls._normalize_metrics(evidence_metrics)
        except (TypeError, ValueError) as exc:
            msg = "completed derived result has invalid evidence metrics"
            raise RuntimeError(msg) from exc
        if canonical_manifest_bytes({"metrics": metrics}) != canonical_manifest_bytes(
            {"metrics": raw_metrics}
        ):
            msg = "completed derived result evidence metrics are not canonical"
            raise ValueError(msg)
        return metrics

    @classmethod
    def _verify_derived_metric_projection(
        cls,
        existing: BacktestResult,
        metrics: dict[str, float | int],
        *,
        sentinel: bool,
    ) -> None:
        decimal_fields = {
            "initial_capital": 2,
            "total_return_pct": 4,
            "cagr_pct": 4,
            "sharpe_ratio": 4,
            "sortino_ratio": 4,
            "max_drawdown_pct": 4,
            "win_rate_pct": 4,
            "profit_factor": 4,
            "avg_win_pct": 4,
            "avg_loss_pct": 4,
            "prediction_accuracy_pct": 4,
            "final_equity": 2,
            "peak_equity": 2,
            "fees_paid": 2,
        }
        for field, places in decimal_fields.items():
            stored = getattr(existing, field)
            if field == "initial_capital" and sentinel:
                expected: Decimal | None = Decimal("0.00")
            else:
                expected = cls._derived_decimal(metrics, field, places)
            if stored != expected:
                msg = f"completed derived result {field} projection is inconsistent"
                raise ValueError(msg)

        for field in (
            "total_trades",
            "winning_trades",
            "losing_trades",
            "total_signals",
            "long_signals",
            "short_signals",
            "correct_predictions",
        ):
            if getattr(existing, field) != metrics.get(field):
                msg = f"completed derived result {field} projection is inconsistent"
                raise ValueError(msg)
        if existing.trades_json is not None:
            msg = "completed derived result must not contain a trade ledger"
            raise ValueError(msg)

    @staticmethod
    def _existing_backtest_id(
        session: Session,
        trial: BacktestTrial,
        requested_backtest_id: str | None,
    ) -> str:
        if trial.result_id is None:
            msg = "completed backtest trial has no result link"
            raise RuntimeError(msg)
        existing = session.get(BacktestResult, trial.result_id)
        if existing is None:
            msg = "completed backtest trial references a missing result"
            raise RuntimeError(msg)
        if requested_backtest_id is not None and existing.backtest_id != requested_backtest_id:
            msg = "completed trial cannot be linked to a different backtest_id"
            raise ValueError(msg)
        stored_backtest_id: str = existing.backtest_id
        return stored_backtest_id


__all__ = [
    "BacktestResultStore",
    "DerivedBacktestEvidence",
    "DerivedEvidenceMetrics",
]
