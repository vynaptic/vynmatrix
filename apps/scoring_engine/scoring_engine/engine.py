from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from lib_common.config_validation import ScoringRuntimeConfig
from lib_common.logging import get_logger
from lib_common.observability import build_trace_context, ensure_run_id
from lib_common.paper_promotion import PaperPromotionModelContext, PaperPromotionScope
from lib_strategy.signals.adapters.scoring import ScoringSignalView, build_scoring_view
from lib_strategy.signals.normalization import normalize_scoring_action, normalize_signal_action
from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.signals.utils import ensure_utc

from ._score_calculator import ScoreCalculator
from .binding_evaluation import BindingEvaluator
from .domain import GlobalScore
from .models import (
    ModePerformance,
    ScoreComponent,
    ScoreRecord,
    ScoringUserBinding,
    SignalRecord,
    TriggerDecision,
)
from .pipeline import PipelineConfig, PipelineResult, ScoringPipeline
from .storage import ScoreStore

logger = get_logger(__name__)

# Strategy type the canonical Signal recognises. A stored signal whose type is
# outside this set (e.g. "unknown" from a null source_runner) cannot be safely
# reconstructed for scoring and is dropped rather than fed into the pipeline.
_VALID_STRATEGY_TYPES = frozenset({"indicator"})


def signal_from_record(record: SignalRecord) -> Signal | None:
    """Reconstruct a canonical ``Signal`` from a stored ``SignalRecord``.

    Returns ``None`` (never raises) for a malformed or unknown-strategy_type
    record so a bad row can never abort an ingest. Mirrors the field mapping
    used when the signal was originally ingested. Single source of truth for the
    record→Signal reconstruction used by both the dispatch path
    (``_build_latest_signal``) and the cross-strategy ensemble gather.
    """
    try:
        strategy_type = (record.strategy_type or "").strip().lower()
        if strategy_type not in _VALID_STRATEGY_TYPES:
            return None
        return Signal(
            signal_id=record.signal_id,
            strategy_id=record.strategy_id,
            strategy_type=strategy_type,
            symbol=record.symbol,
            action=normalize_signal_action(record.action),
            confidence=record.confidence,
            timestamp=record.timestamp,
            expected_return=record.expected_return,
            predicted_risk=record.predicted_risk,
            horizon_days=record.horizon_days,
            entry_price=record.entry_price,
            stop_loss=record.stop_loss,
            take_profit=record.take_profit,
            size_hint=record.size_hint,
            asset_class=record.asset_class,
            sector=record.sector,
            industry=record.industry,
            index=record.index,
            instrument_id=record.instrument_id,
            strategy_version=record.strategy_version,
            run_id=record.run_id,
            source=record.source,
            metadata=record.metadata,
            expires_at=record.expires_at,
        )
    except (ValueError, TypeError):
        return None


@dataclass
class _EnrichedIngest:
    """Plumbing carried between the three ``ingest_signal`` stages.

    Pure data hand-off — every field is produced by ``_enrich_signal`` exactly
    as the pre-split inline code produced its locals; no stage re-derives or
    mutates another stage's inputs.
    """

    incoming: Signal
    enriched_signal: Signal
    scoring_view: ScoringSignalView
    pipeline_result: PipelineResult
    batch: list[Signal]
    run_id: str
    trace_ctx: dict[str, Any]
    scoring_action_override: SignalAction | None = None


class ScoreEngine:
    """
    Aggregates canonical Signals into scores and evaluates user bindings.

    Responsibilities:
    - Ingest Signals
    - Apply weights/decay to compute asset/sector scores
    - Persist signals and scores
    - Evaluate user bindings and return trigger decisions
    """

    def __init__(
        self,
        store: ScoreStore,
        weights: dict[str, float] | None = None,
        default_weight: float = 1.0,
        half_life_bars: int = 20,
        pipeline: ScoringPipeline | None = None,
        runtime_config: ScoringRuntimeConfig | None = None,
        paper_promotion_scope: PaperPromotionScope | None = None,
        paper_promotion_required: bool = False,
    ) -> None:
        runtime = runtime_config or ScoringRuntimeConfig()
        self.store = store
        self.weights = weights or {}
        self.default_weight = default_weight
        self.half_life_bars = max(1, half_life_bars)
        self.pipeline = pipeline or ScoringPipeline(config=PipelineConfig())
        # Score-axis aggregation lives in its own helper so this class can
        # focus on signal ingestion + binding evaluation.
        self._calculator = ScoreCalculator(
            store=store,
            weight_for=self._weight_for,
            half_life_bars=self.half_life_bars,
        )
        # SC-asset: when enabled, the gating asset score blends the recent
        # one-per-strategy signals for the asset through the ensemble (a true
        # cross-strategy alpha combination) instead of scoring the lone incoming
        # signal. DEFAULT OFF — single-strategy gating is byte-identical to the
        # pre-change path, so promotion is gated on a deliberate flip + e2e.
        self._persist_decision_context_enabled = runtime.persist_decision_context
        self._cross_strategy_ensemble = runtime.ensemble.enabled
        # A sibling signal is admitted only if its timestamp is within this
        # (two-sided) window of the incoming signal's timestamp. Coarse global
        # guard — the scoring engine has no per-asset bar cadence.
        self._sibling_freshness_seconds = runtime.ensemble.sibling_freshness_seconds
        # Hard cap on sibling strategies blended per ingest (bounds blast radius
        # and the extra list_signals read on the hot path).
        self._max_siblings = runtime.ensemble.max_siblings
        self._paper_promotion_scope = paper_promotion_scope
        self._paper_promotion_required = paper_promotion_required
        # Binding evaluation + mode selection live in their own collaborator
        # (F8 split); ``evaluate_bindings`` / ``select_best_mode`` below stay
        # as the documented public surface and delegate to it.
        self._binding_evaluator = BindingEvaluator(
            store=store,
            paper_promotion_scope=paper_promotion_scope,
            paper_promotion_required=paper_promotion_required,
        )

    def _weight_for(self, strategy_id: str) -> float:
        return float(self.weights.get(strategy_id, self.default_weight))

    def _gather_cross_strategy_batch(self, incoming: Signal) -> list[Signal]:
        """Build the per-asset batch fed to the scoring pipeline (SC-asset).

        Returns ``[incoming]`` unchanged when the flag is off (the default) — so
        single-strategy gating is byte-identical to the pre-change path and the
        store is not even read. When enabled, appends the latest fresh,
        directional signal from each OTHER strategy for the same asset (one per
        strategy), so the existing ensemble aggregates across strategies. The
        incoming signal is always element 0 and represents its own strategy.
        """
        if not self._cross_strategy_ensemble:
            return [incoming]

        incoming_ts = ensure_utc(incoming.timestamp)
        # Seed with the incoming strategy so its own (stale or re-posted) stored
        # rows are never added — the live incoming Signal is the only copy.
        seen = {(incoming.strategy_id or "").strip().lower()}
        siblings: list[Signal] = []
        # Read the latest row PER strategy (not a flat newest-N page) so one chatty
        # strategy can't crowd slower strategies off the page. +1 covers the
        # incoming strategy's own row, which `seen` then skips.
        for record in self.store.list_latest_signal_per_strategy(
            incoming.symbol, limit=self._max_siblings + 1
        ):
            if len(siblings) >= self._max_siblings:
                break
            sid = (record.strategy_id or "").strip().lower()
            if sid in seen:
                # Incoming strategy, or a strategy whose latest in-window row we
                # already decided on (stores return newest-first, so first wins).
                continue
            # Two-sided freshness vs the incoming timestamp (deterministic for
            # replay; a sibling newer than the incoming would otherwise get an
            # un-clamped age decay > 1 and over-weight the blend). A malformed /
            # missing timestamp is skipped, never raised — a bad row must never
            # abort the ingest.
            try:
                age_seconds = abs((incoming_ts - ensure_utc(record.timestamp)).total_seconds())
            except (AttributeError, TypeError):
                continue
            if age_seconds > self._sibling_freshness_seconds:
                continue
            # The LATEST in-window row decides this strategy's vote: mark it seen
            # before the direction/validity checks so a newer CLOSE/neutral (or a
            # malformed) row suppresses older directional rows of the same
            # strategy, instead of letting a stale entry sneak in.
            seen.add(sid)
            # Drop a strategy whose latest signal is neutral/CLOSE: it is flat, so
            # including it would only inflate the ensemble's total weight and
            # dilute the live signal's gate magnitude. (The incoming's own CLOSE
            # still flows through as element 0.)
            if record.action not in ("long", "short"):
                continue
            sibling = signal_from_record(record)
            if sibling is None:
                continue
            siblings.append(sibling)
        if not siblings:
            return [incoming]
        # Every sibling carries the instrument's canonical symbol (the store maps
        # rows through one resolved instrument). Re-stamp the incoming to canonical
        # too so the batch is symbol-homogeneous: this drops the spurious
        # "mixed symbols" pipeline warning and keeps the cross-strategy rolling
        # stats keyed by canonical (not per-alias) when an alias was ingested.
        # signal_id / external_signal_id are preserved by ``replace`` so identity,
        # dedup, and provenance selection are unchanged. No-op when already canonical.
        canonical_symbol = siblings[0].symbol
        if canonical_symbol and canonical_symbol != incoming.symbol:
            incoming = replace(incoming, symbol=canonical_symbol)
        return [incoming, *siblings]

    @staticmethod
    def _incoming_meta_scalars(
        meta_outputs: list[Any],
        incoming_signal_id: str,
        *,
        multi_strategy: bool,
        metadata: dict[str, Any],
    ) -> tuple[float, float, float]:
        """Return (meta_probability, score_local, s_raw) for the INCOMING signal.

        Selected by signal_id so provenance describes the incoming signal, not a
        sibling, regardless of batch order. Falls back to neutral defaults if the
        incoming output was filtered out, flagging that in metadata when siblings
        nonetheless contributed.
        """
        incoming_meta = next((m for m in meta_outputs if m.signal_id == incoming_signal_id), None)
        if incoming_meta is not None:
            return (
                incoming_meta.meta_probability,
                incoming_meta.score_local,
                incoming_meta.s_raw,
            )
        if multi_strategy:
            metadata["incoming_meta_absent"] = True
        return 0.5, 0.0, 0.0

    @staticmethod
    def _record_pipeline_abstention(
        metadata: dict[str, Any],
        pipeline_metadata: dict[str, Any],
    ) -> None:
        """Attach a fail-closed pipeline abstention to signal provenance."""
        if pipeline_metadata.get("abstained") is not True:
            return
        metadata["abstained"] = True
        metadata["abstain_reason"] = pipeline_metadata.get(
            "abstain_reason",
            "meta_inputs_unavailable",
        )

    def _resolve_hierarchy(
        self,
        signal: Signal,
        sector: str | None,
        industry: str | None,
        index: str | None,
    ) -> dict[str, str | None]:
        hierarchy = None
        if not sector or not industry or not index or not signal.asset_class:
            try:
                hierarchy = self.store.get_instrument(signal.symbol)
            except (SQLAlchemyError, AttributeError, KeyError, TypeError):
                logger.exception(
                    "Failed to get instrument hierarchy",
                    symbol=signal.symbol,
                )

        return {
            "sector": sector or (hierarchy.sector if hierarchy else None),
            "industry": industry or (hierarchy.industry if hierarchy else None),
            "index": index or (hierarchy.index if hierarchy else None),
            "asset_class": signal.asset_class or (hierarchy.asset_class if hierarchy else None),
            # Coerce to str (Signal.instrument_id is int | str | None) for a
            # consistent identifier in the str-typed context dict.
            "instrument_id": (
                str(signal.instrument_id) if signal.instrument_id is not None else None
            ),
        }

    def ingest_signal(
        self,
        signal: Signal,
        sector: str | None = None,
        industry: str | None = None,
        index: str | None = None,
        *,
        scoring_action_override: SignalAction | None = None,
    ) -> ScoreRecord:
        """
        Ingest a signal, store it, recompute score for the symbol, and persist the score.

        Args:
            signal: The canonical Signal to ingest.
            sector: Optional sector classification (e.g., "Technology", "Finance").
            industry: Optional industry classification (e.g., "Semiconductors", "Banking").
            index: Optional index membership (e.g., "SP500", "NASDAQ100", "NIFTY50").

        Returns:
            Latest ScoreRecord for the symbol.
        """
        # Keep a reference to the caller-visible metadata. The HTTP handler
        # dispatches this same Signal after scoring, so scoring provenance added
        # below must survive any dataclass ``replace`` used for enrichment.
        caller_metadata = signal.metadata
        enriched = self._enrich_signal(
            signal,
            sector=sector,
            industry=industry,
            index=index,
            caller_metadata=caller_metadata,
            scoring_action_override=scoring_action_override,
        )
        record, metadata, action, meta_scalars = self._build_signal_record(enriched)
        return self._persist_scoring_outputs(
            enriched,
            record=record,
            metadata=metadata,
            action=action,
            meta_scalars=meta_scalars,
            caller_metadata=caller_metadata,
        )

    def ingest_signals(
        self,
        signals: Iterable[Signal],
        *,
        scoring_action_overrides: Mapping[str, SignalAction] | None = None,
    ) -> list[ScoreRecord]:
        """Ingest an ordered batch through the exact single-signal helper.

        Transaction ownership remains with the caller. The optional override is
        scoring-only provenance: it never rewrites the canonical signal action.
        """

        ordered = list(signals)
        overrides = dict(scoring_action_overrides or {})
        known_ids = {signal.signal_id for signal in ordered}
        unknown_ids = set(overrides) - known_ids
        if unknown_ids:
            message = f"Scoring overrides reference unknown signals: {sorted(unknown_ids)!r}"
            raise ValueError(message)
        invalid = [
            action
            for action in overrides.values()
            if action not in {SignalAction.LONG, SignalAction.SHORT}
        ]
        if invalid:
            message = "Scoring action overrides must be LONG or SHORT"
            raise ValueError(message)
        return [
            self.ingest_signal(
                signal,
                scoring_action_override=overrides.get(signal.signal_id),
            )
            for signal in ordered
        ]

    def _enrich_signal(
        self,
        signal: Signal,
        *,
        sector: str | None,
        industry: str | None,
        index: str | None,
        caller_metadata: dict[str, Any],
        scoring_action_override: SignalAction | None,
    ) -> _EnrichedIngest:
        """Stage 1 of ``ingest_signal``: classify, stamp identity, run the pipeline."""
        classification = self._resolve_hierarchy(signal, sector, industry, index)
        run_id = ensure_run_id(signal.run_id)
        if signal.run_id != run_id:
            signal = replace(signal, run_id=run_id)

        if any(
            [
                classification.get("asset_class")
                and signal.asset_class != classification.get("asset_class"),
                classification.get("sector") and signal.sector != classification.get("sector"),
                classification.get("industry")
                and signal.industry != classification.get("industry"),
                classification.get("index") and signal.index != classification.get("index"),
                classification.get("instrument_id")
                and signal.instrument_id != classification.get("instrument_id"),
            ]
        ):
            signal = replace(
                signal,
                asset_class=classification.get("asset_class") or signal.asset_class,
                sector=classification.get("sector") or signal.sector,
                industry=classification.get("industry") or signal.industry,
                index=classification.get("index") or signal.index,
                instrument_id=classification.get("instrument_id") or signal.instrument_id,
            )

        trace_ctx = build_trace_context(
            run_id=run_id,
            signal_id=signal.signal_id,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
        )
        logger.info("Ingesting signal", **trace_ctx)

        batch = self._gather_cross_strategy_batch(signal)
        # A multi-strategy blend standardizes against (and updates) a SEPARATE
        # rolling-stats namespace so enabling the flag for an asset cannot poison
        # the single-strategy window used by the flag-off / lone-ingest path.
        # Keyed off batch[0] (canonicalized by the gather) so the namespace is
        # per-canonical-asset, not per-alias.
        stats_key = f"xstrat:{batch[0].symbol}" if len(batch) > 1 else None
        pipeline_result: PipelineResult = self.pipeline.process_signals(
            signals=batch,
            current_time=signal.timestamp,
            stats_key=stats_key,
            scoring_action_overrides=(
                {signal.signal_id: scoring_action_override}
                if scoring_action_override is not None
                else None
            ),
        )

        # Provenance + persistence describe the INCOMING signal, not a sibling:
        # select its enriched copy by signal_id (a lone batch -> element 0).
        enriched_signal = next(
            (s for s in pipeline_result.signals if s.signal_id == signal.signal_id),
            pipeline_result.signals[0] if pipeline_result.signals else signal,
        )
        scoring_view = build_scoring_view(
            enriched_signal,
            scoring_action_override=scoring_action_override,
        )
        caller_metadata["scoring_input_source"] = scoring_view.metadata["scoring_input_source"]
        enriched_signal.metadata["scoring_input_source"] = scoring_view.metadata[
            "scoring_input_source"
        ]
        if scoring_action_override is not None:
            caller_metadata["scoring_action_override"] = scoring_action_override.value
            enriched_signal.metadata["scoring_action_override"] = scoring_action_override.value
        return _EnrichedIngest(
            incoming=signal,
            enriched_signal=enriched_signal,
            scoring_view=scoring_view,
            pipeline_result=pipeline_result,
            batch=batch,
            run_id=run_id,
            trace_ctx=trace_ctx,
            scoring_action_override=scoring_action_override,
        )

    def _build_signal_record(
        self,
        enriched: _EnrichedIngest,
    ) -> tuple[SignalRecord, dict[str, Any], str, tuple[float, float, float]]:
        """Stage 2 of ``ingest_signal``: build the persisted record + provenance."""
        enriched_signal = enriched.enriched_signal
        scoring_view = enriched.scoring_view
        batch = enriched.batch
        meta_outputs = enriched.pipeline_result.meta_outputs
        run_id = enriched.run_id

        # Persist signal with enriched fields
        signal_confidence = float(enriched_signal.confidence)
        sector_val = enriched_signal.sector
        industry_val = enriched_signal.industry
        index_val = enriched_signal.index
        asset_class_val = enriched_signal.asset_class
        # Signal.instrument_id is Optional[int | str]; SignalRecord.instrument_id
        # (and the hierarchy dict) is Optional[str]. Coerce to a consistent string
        # identifier (matches the str(instrument_id) convention used elsewhere) so
        # an int id is not silently stored in a str field.
        instrument_id_val = (
            str(enriched_signal.instrument_id)
            if enriched_signal.instrument_id is not None
            else None
        )
        entry_price = enriched_signal.entry_price
        stop_loss = enriched_signal.stop_loss
        take_profit = enriched_signal.take_profit
        metadata = dict(enriched_signal.metadata or {})
        metadata["scoring_input_source"] = scoring_view.metadata["scoring_input_source"]
        self._record_pipeline_abstention(metadata, enriched.pipeline_result.metadata)
        metadata.update(
            {
                "asset_class": asset_class_val,
                "sector": sector_val,
                "industry": industry_val,
                "index": index_val,
                "instrument_id": instrument_id_val,
                "strategy_version": enriched_signal.strategy_version,
                "run_id": run_id,
                "source": enriched_signal.source,
                "confidence": signal_confidence,
            }
        )

        meta_probability, score_local, s_raw = self._incoming_meta_scalars(
            meta_outputs,
            enriched.incoming.signal_id,
            multi_strategy=len(batch) > 1,
            metadata=metadata,
        )
        metadata.update(
            {
                "meta_probability": meta_probability,
                "score_local": score_local,
                "s_raw": s_raw,
            }
        )
        # Record which siblings combined into the gating score (multi-strategy
        # only, so single-strategy provenance is unchanged).
        if len(batch) > 1:
            metadata["cross_strategy"] = {
                "batch_size": len(batch),
                "siblings": [s.strategy_id for s in batch[1:]],
            }

        # Use canonical normalization to preserve CLOSE→"flat" intent.
        # Direction-only mapping loses exit signals (CLOSE has direction 0).
        action = normalize_scoring_action(enriched_signal.action)
        # Fallback to direction-based if normalization yields "hold" but direction is clear
        if action == "hold" and enriched.scoring_action_override is None:
            if scoring_view.direction > 0:
                action = "long"
            elif scoring_view.direction < 0:
                action = "short"

        weight = self._weight_for(enriched_signal.strategy_id)
        record = SignalRecord(
            signal_id=enriched_signal.signal_id,
            strategy_id=enriched_signal.strategy_id,
            strategy_type=enriched_signal.strategy_type,
            symbol=enriched_signal.symbol,
            expected_return=scoring_view.expected_return,
            predicted_risk=scoring_view.predicted_risk,
            horizon_days=scoring_view.horizon_days,
            action=action,
            confidence=signal_confidence,
            timestamp=scoring_view.signal_timestamp,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata=metadata,
            sector=sector_val,
            industry=industry_val,
            index=index_val,
            asset_class=asset_class_val,
            instrument_id=instrument_id_val,
            strategy_version=metadata.get("strategy_version"),
            run_id=run_id,
            source=metadata.get("source"),
            external_signal_id=enriched_signal.external_signal_id,
            expires_at=enriched_signal.expires_at,
            score_value=score_local * weight,
        )
        return record, metadata, action, (meta_probability, score_local, s_raw)

    def _persist_scoring_outputs(
        self,
        enriched: _EnrichedIngest,
        *,
        record: SignalRecord,
        metadata: dict[str, Any],
        action: str,
        meta_scalars: tuple[float, float, float],
        caller_metadata: dict[str, Any],
    ) -> ScoreRecord:
        """Stage 3 of ``ingest_signal``: persist signal, scores, and provenance."""
        enriched_signal = enriched.enriched_signal
        scoring_view = enriched.scoring_view
        pipeline_result = enriched.pipeline_result
        global_score: GlobalScore = pipeline_result.global_score
        meta_outputs = pipeline_result.meta_outputs
        run_id = enriched.run_id
        meta_probability, score_local, s_raw = meta_scalars

        canonical_signal_id = self.store.add_signal(record)
        self._attach_canonical_signal_id(
            canonical_signal_id,
            caller_metadata=caller_metadata,
            enriched_metadata=enriched_signal.metadata,
            persisted_metadata=metadata,
        )

        # Build components from meta outputs
        components: list[ScoreComponent] = []
        for m in meta_outputs:
            w_i = self._weight_for(m.strategy_id)
            components.append(
                ScoreComponent(
                    strategy_id=m.strategy_id,
                    weight=w_i,
                    raw_value=m.score_local,
                    weighted_value=w_i * m.score_local,
                    confidence=m.meta_probability,
                    timestamp=m.computed_at,
                )
            )

        score_record = ScoreRecord(
            target=scoring_view.asset,
            scope="asset",
            score=global_score.score,
            components=components,
            metadata={
                "p_agg": global_score.p_agg,
                "alpha_core": global_score.alpha_core,
                "alpha_raw": global_score.alpha_raw,
                "news_sentiment": global_score.news_sentiment,
                "abstained": pipeline_result.metadata.get("abstained", False),
                **(
                    {"abstain_reason": pipeline_result.metadata["abstain_reason"]}
                    if pipeline_result.metadata.get("abstain_reason")
                    else {}
                ),
                # Multi-factor breakdown (Q3) when enabled; absent (no key) on the
                # default single-alpha path so the persisted _meta is unchanged.
                **(
                    {"factors": global_score.metadata["factors"]}
                    if global_score.metadata.get("factors")
                    else {}
                ),
            },
            # Idempotency key: a re-delivered signal updates its score row instead
            # of inserting a duplicate (SC-6).
            external_signal_id=enriched_signal.external_signal_id,
        )
        self.store.upsert_score(score_record)
        self._upsert_hierarchy_scores(
            record.sector,
            record.industry,
            record.index,
            record.asset_class,
        )
        logger.info(
            "Score computed",
            score=global_score.score,
            p_agg=global_score.p_agg,
            **enriched.trace_ctx,
        )
        self._persist_decision_context(
            enriched_signal=enriched_signal,
            run_id=run_id,
            action=action,
            global_score=global_score,
            score_record=score_record,
            meta_probability=meta_probability,
            score_local=score_local,
            s_raw=s_raw,
            metadata=metadata,
        )
        return score_record

    def _attach_canonical_signal_id(
        self,
        canonical_signal_id: int | None,
        *,
        caller_metadata: dict[str, Any],
        enriched_metadata: dict[str, Any],
        persisted_metadata: dict[str, Any],
    ) -> None:
        """Expose the persisted identity to the same Signal dispatched by the API."""
        if canonical_signal_id is None:
            if getattr(self.store, "supports_canonical_signals", False):
                msg = "Canonical signal persistence did not return its database identifier"
                raise RuntimeError(msg)
            return

        # Signal is frozen, but metadata is intentionally mutable. The HTTP
        # handler dispatches its original Signal to avoid a latest-row race.
        for target in (caller_metadata, enriched_metadata, persisted_metadata):
            target["canonical_signal_id"] = canonical_signal_id

    def _upsert_hierarchy_scores(
        self,
        sector_val: str | None,
        industry_val: str | None,
        index_val: str | None,
        asset_class_val: str | None,
    ) -> None:
        """Upsert sector/industry/index scores (when classified) + the market score."""
        if sector_val:
            self.store.upsert_score(self._compute_sector_score(sector_val))
        if industry_val:
            self.store.upsert_score(self._compute_industry_score(industry_val))
        if index_val:
            self.store.upsert_score(self._compute_index_score(index_val))
        if asset_class_val:
            market_score = self._compute_market_score(asset_class_val)
            if market_score is not None:
                self.store.upsert_score(market_score)
        else:
            logger.error("Signal has no asset_class; market score was not produced")

    def _persist_decision_context(
        self,
        *,
        enriched_signal: Signal,
        run_id: str,
        action: str,
        global_score: GlobalScore,
        score_record: ScoreRecord,
        meta_probability: float,
        score_local: float,
        s_raw: float,
        metadata: dict[str, Any],
    ) -> None:
        """Snapshot the decision context (provenance) on the active unit of work.

        Written atomically with the signal + scores so the decision is replayable
        and auditable against exactly the inputs that produced it. No-op on the
        in-memory store; gated by SCORING_PERSIST_DECISION_CONTEXT.
        """
        if not self._persist_decision_context_enabled:
            return
        self.store.persist_decision_context(
            signal_id=enriched_signal.signal_id,
            symbol=enriched_signal.symbol,
            strategy_id=enriched_signal.strategy_id,
            run_id=run_id,
            action=action,
            score_value=global_score.score,
            feature_snapshot={
                "score": global_score.score,
                "standardized_score": global_score.score,
                "p_agg": global_score.p_agg,
                "alpha_core": global_score.alpha_core,
                "alpha_raw": global_score.alpha_raw,
                "news_sentiment": global_score.news_sentiment,
                "meta_probability": meta_probability,
                "score_local": score_local,
                "s_raw": s_raw,
                "abstained": metadata.get("abstained", False),
                "abstain_reason": metadata.get("abstain_reason"),
                "components": score_record.to_dict()["components"],
            },
            signal_metadata=metadata,
        )

    # The score-axis compute methods below are thin shims around the
    # ``ScoreCalculator`` collaborator. The actual aggregation logic
    # lives in ``_score_calculator.py``; these wrappers exist so
    # ``ingest_signal`` and external callers keep their existing API.

    def _compute_sector_score(self, sector: str) -> ScoreRecord:
        """Aggregate signals across assets within a sector (using sector tag)."""
        return self._calculator.sector(sector)

    def _compute_industry_score(self, industry: str) -> ScoreRecord:
        """Aggregate signals across assets within an industry."""
        return self._calculator.industry(industry)

    def _compute_index_score(self, index_name: str) -> ScoreRecord:
        """Aggregate signals across assets within an index."""
        return self._calculator.index(index_name)

    def _compute_market_score(self, asset_class: str) -> ScoreRecord | None:
        """Aggregate one explicitly attributed asset-class market score."""
        return self._calculator.market(asset_class)

    def evaluate_bindings(
        self,
        score: ScoreRecord,
        bindings: Iterable[ScoringUserBinding] | None = None,
        sector_score: ScoreRecord | None = None,
        market_score: ScoreRecord | None = None,
        signal_strategy_id: str | None = None,
        signal_strategy_version: str | None = None,
        signal_action: object | None = None,
        promotion_model_context: PaperPromotionModelContext | None = None,
    ) -> list[TriggerDecision]:
        """Evaluate user bindings against scores using 3-tier threshold logic.

        Documented public surface — delegates to ``BindingEvaluator.evaluate_bindings``
        (see ``binding_evaluation.py``) with an identical signature and semantics.
        """
        return self._binding_evaluator.evaluate_bindings(
            score,
            bindings=bindings,
            sector_score=sector_score,
            market_score=market_score,
            signal_strategy_id=signal_strategy_id,
            signal_strategy_version=signal_strategy_version,
            signal_action=signal_action,
            promotion_model_context=promotion_model_context,
        )

    @property
    def paper_promotion_scope(self) -> PaperPromotionScope | None:
        """Return the immutable cloud-paper scope loaded at startup."""

        return self._paper_promotion_scope

    @property
    def paper_promotion_required(self) -> bool:
        """Return whether every execution decision requires that scope."""

        return self._paper_promotion_required

    def select_best_mode(
        self,
        asset: str,
        horizon: str = "1d",
        allowed_modes: list[str] | None = None,
        policy: str | None = None,
        account_id: int | None = None,
        strategy_id: str | None = None,
    ) -> str | None:
        """Select the best execution mode for an asset from mode-performance stats.

        Documented public surface — delegates to ``BindingEvaluator.select_best_mode``
        (see ``binding_evaluation.py``) with an identical signature and semantics.
        """
        return self._binding_evaluator.select_best_mode(
            asset,
            horizon=horizon,
            allowed_modes=allowed_modes,
            policy=policy,
            account_id=account_id,
            strategy_id=strategy_id,
        )

    @staticmethod
    def _rank_mode_performance(
        perf: list[ModePerformance],
        allowed_modes: list[str] | None = None,
        policy: str | None = None,
    ) -> str | None:
        """Delegate to the pure ranking helper (kept for the documented name)."""
        return BindingEvaluator._rank_mode_performance(
            perf,
            allowed_modes=allowed_modes,
            policy=policy,
        )
