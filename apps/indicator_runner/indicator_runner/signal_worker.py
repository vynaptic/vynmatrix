"""Signal Worker — DB-fed runtime for PureSignalStrategy instances.

This is the indicator runtime: each strategy is a pure ``core.py``
``PureSignalStrategy`` fed from the database.  It:

1. LISTENs for ``new_market_data`` PostgreSQL notifications
2. Maintains a per-symbol watermark so missed bars are caught up on restart
3. Bootstraps indicator history at startup from the prices table
4. Refuses to emit signals until ``warmup_complete()`` is True
5. Consolidates minute bars into the strategy's configured period
6. Atomically journals model state, source watermark, and signal envelopes
7. Relays committed envelopes to scoring with stable identities after commit

runner_kind = "signal_worker" in the strategy config activates this path.
"""

from __future__ import annotations

import argparse
import json
import signal as os_signal
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from indicator_runner.panel_binding import PanelRuntimeBinding, scoped_panel_worker_id
from indicator_runner.panel_runtime import SynchronizedPanelRuntime
from indicator_runner.runtime_journal import (
    BufferedSignalEmitter,
    DurableSignalRelay,
    SignalRelayReadiness,
    StrategyBarDecision,
    StrategyRuntimeIdentity,
    StrategyRuntimeStore,
)
from indicator_runner.runtime_params import StrategyRuntimeParams
from lib_application.db.session import (
    create_engine_for_env,
    dispose_engine,
    get_session_factory,
)
from lib_application.outbox import OutboxStore
from lib_application.services.price_ingestion_service import PriceIngestionService
from lib_common.asset_classes import normalize_asset_class
from lib_common.config_validation import (
    IndicatorWorkerConfig,
    load_indicator_log_level,
    load_indicator_worker_config,
)
from lib_common.logging import get_logger, setup_logging
from lib_common.metrics import counter
from lib_common.time_utils import now_utc
from lib_data.bars import Bar, ohlcv_invariant_error
from lib_data.consolidation import BarConsolidator
from lib_data.watermark import (
    HistoricalRebuildRequiredError,
    RebuildGenerationChangedError,
    Watermark,
    WatermarkState,
)
from lib_strategy.panels import SynchronizedPanelStrategy
from lib_strategy.signals.emitter import HttpSignalEmitter
from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import Signal
from lib_strategy.signals.utils import normalize_signal_api_base_url

logger = get_logger(__name__)

SessionFactory = Callable[[], Any]
HISTORICAL_REBUILD_EXIT_CODE = 75
_BARS_PROCESSED_METRIC = "vm_indicator_bars_processed_total"
_SIGNALS_EMITTED_METRIC = "vm_indicator_signals_emitted_total"


def _record_processed_bar(*, strategy_id: str, timeframe: str) -> None:
    """Count one strategy bar that completed without a processing error."""
    try:
        metric = counter(
            _BARS_PROCESSED_METRIC,
            "Closed strategy bars successfully processed by indicator workers",
            ("strategy_id", "timeframe"),
        )
        if metric is not None:
            metric.labels(strategy_id=strategy_id, timeframe=timeframe).inc()
    except (OSError, ValueError):
        logger.warning("Failed to record processed-bar metric", exc_info=True)


def _record_emitted_signal(signal: Signal) -> None:
    """Count one non-HOLD canonical signal after successful emitter delivery."""
    try:
        metric = counter(
            _SIGNALS_EMITTED_METRIC,
            "Canonical signals successfully emitted by indicator workers",
            ("strategy_id", "action"),
        )
        if metric is not None:
            metric.labels(
                strategy_id=signal.strategy_id,
                action=signal.action.value,
            ).inc()
    except (OSError, ValueError):
        logger.warning("Failed to record emitted-signal metric", exc_info=True)


class SignalWorker:
    """DB-fed strategy worker with LISTEN/NOTIFY, watermark, and warmup.

    Lifecycle:
        1. ``start()`` — bootstrap history, begin listening
        2. On each notification or catch-up poll: fetch new bars since watermark,
           consolidate, and feed to the strategy core
        3. ``stop()`` — clean shutdown

    Args:
        strategy: The PureSignalStrategy instance (e.g. SwingHighLowPMOCore)
        session_factory: SQLAlchemy session factory for DB access
        worker_id: Unique worker identifier (for watermark tracking)
        symbols: List of symbols to process
        consolidation_minutes: Bar consolidation period (0 = no consolidation)
        dsn: PostgreSQL DSN for LISTEN/NOTIFY
        notify_channel: NOTIFY channel name
        bootstrap_bars: Number of historical bars to fetch at startup
        signal_buffer: Transaction-local emitter capturing canonical signals
            until the strategy transaction commits (the strategy's emitter)
        runtime_store: Durable journal for model state, decisions, and outbox
        signal_relay: Fenced post-commit relay from ``signals.submit`` to scoring
    """

    def __init__(
        self,
        *,
        strategy: PureSignalStrategy,
        session_factory: SessionFactory,
        worker_id: str,
        symbols: list[str],
        consolidation_minutes: int = 15,
        dsn: str = "",
        notify_channel: str = "new_market_data",
        bootstrap_bars: int = 500,
        source: str = "coinbase_live",
        timeframe: str = "1m",
        min_bar_coverage: float = 0.5,
        catchup_batch_size: int = 5000,
        catchup_floor_seconds: int = 30,
        asset_class: str = "crypto",
        clock: Callable[[], datetime] | None = None,
        signal_buffer: BufferedSignalEmitter,
        runtime_store: StrategyRuntimeStore,
        signal_relay: DurableSignalRelay,
    ) -> None:
        self._strategy = strategy
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._symbols = [s.strip().upper() for s in symbols if s.strip()]
        self._consolidation_minutes = consolidation_minutes
        self._dsn = dsn
        self._notify_channel = notify_channel
        self._bootstrap_bars = bootstrap_bars
        self._source = source
        self._timeframe = timeframe
        self._asset_class = normalize_asset_class(
            asset_class,
            field_name="signal-worker asset_class",
        )
        self._clock = clock or now_utc
        self._signal_buffer = signal_buffer
        self._runtime_store = runtime_store
        self._signal_relay = signal_relay
        self._uncommitted_decisions: list[StrategyBarDecision] = []
        if catchup_batch_size < 1:
            msg = "catchup_batch_size must be positive"
            raise ValueError(msg)
        if catchup_floor_seconds < 1:
            msg = "catchup_floor_seconds must be positive"
            raise ValueError(msg)
        self._catchup_batch_size = catchup_batch_size
        self._catchup_floor_seconds = catchup_floor_seconds

        self._ingestion_service = PriceIngestionService(session_factory)
        # Signal acceptance and its checkpoint form one recoverable stream. A
        # missing/unwritable checkpoint table must fail the worker; silently
        # falling back to epoch makes every restart look like a cold start.
        self._watermark = Watermark(session_factory, worker_id, source=self._source)
        self._consolidators: dict[str, BarConsolidator] = {}
        # LISTEN callbacks and the main thread's periodic catch-up can overlap.
        # The strategy instance (including multi-asset portfolio state), its
        # consolidators, and checkpoints form one state machine, so serialize the
        # complete watermark -> fetch -> process -> acknowledge transaction across
        # every symbol. RLock permits the catch-up path to call _process_bar().
        self._processing_lock = threading.RLock()
        self._panel_runtime = (
            SynchronizedPanelRuntime(
                strategy=strategy,
                capability=strategy,
                session_factory=session_factory,
                signal_buffer=signal_buffer,
                runtime_store=runtime_store,
                processing_lock=self._processing_lock,
                relay_once=self.relay_once,
                mark_transition_failure=self._mark_panel_transition_failure,
                clock=self._clock,
            )
            if isinstance(strategy, SynchronizedPanelStrategy)
            else None
        )
        self._listener: Any | None = None
        self._listener_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._failure_event = threading.Event()
        self._rebuild_event = threading.Event()
        self._terminal_error: BaseException | None = None
        self._warmed_up = False
        try:
            self._instrument_map = self._ingestion_service.load_instrument_map(
                asset_class=self._asset_class
            )
        except Exception:
            # Boundary catch: instrument map preload talks to the DB; any
            # transient infra failure here must not abort worker construction.
            # The empty fallback is safe — symbols stay unmapped until the next
            # successful refresh.
            logger.warning("Failed to preload instrument map for signal worker", exc_info=True)
            self._instrument_map = {}

        # Create consolidators per symbol. min_bar_coverage drops a consolidated
        # bar whose source minutes were too gappy to trust as a full bar (IND-2),
        # so the strategy never computes indicators on an incomplete period.
        if consolidation_minutes > 0:
            for sym in self._symbols:
                self._consolidators[sym] = BarConsolidator(
                    period_minutes=consolidation_minutes,
                    on_bar=self._on_consolidated_bar,
                    min_coverage=min_bar_coverage,
                )

    def start(self) -> None:
        """Bootstrap history, start LISTEN loop."""
        if self._panel_runtime is not None:
            self._panel_runtime.start()
            self._panel_runtime.poll_input_revisions()
            self._warmed_up = True
        else:
            # Existing per-symbol strategies retain the exact bar/bootstrap
            # path. The synchronized capability is structural and opt-in.
            if not self._strategy._is_initialized:
                self._strategy.initialize()
                self._strategy._is_initialized = True
            self._bootstrap()

        # Recover every committed, undelivered signal before accepting a newer
        # market bar. The outbox claim commits before HTTP, so no strategy or
        # watermark lock is held across the network call.
        self.relay_once()

        # Start LISTEN thread
        if self._dsn and self._panel_runtime is None:
            self._start_listener()

        logger.info(
            "Signal worker started",
            worker_id=self._worker_id,
            symbols=self._symbols,
            warmed_up=self._warmed_up,
        )

    def stop(self) -> None:
        """Stop the worker gracefully."""
        self._stop_event.set()
        if self._listener is not None:
            self._listener.stop()
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=5.0)
        logger.info("Signal worker stopped", worker_id=self._worker_id)

    @property
    def failed(self) -> bool:
        """Whether a terminal signal-delivery failure requires process restart."""
        return self._failure_event.is_set() or self._rebuild_event.is_set()

    @property
    def rebuild_required(self) -> bool:
        """Whether a supervised fresh-core rebuild is the expected next action."""
        return self._rebuild_event.is_set()

    @property
    def terminal_error(self) -> BaseException | None:
        """First terminal worker error, retained for process-level observability."""
        return self._terminal_error

    @property
    def catchup_floor_seconds(self) -> int:
        """Maximum delay before the process-level recovery catch-up runs."""
        return self._catchup_floor_seconds

    def relay_once(self) -> tuple[int, int]:
        """Deliver one bounded durable signal batch."""
        return self._signal_relay.run_once()

    def submit_panel(self, panel_input: object) -> bool:
        """Submit one complete input to an explicitly panel-capable strategy."""

        if self._panel_runtime is None:
            message = f"Strategy {self._strategy.strategy_id!r} is not panel-capable"
            raise TypeError(message)
        return self._panel_runtime.submit(panel_input)

    def resume_prepared_panels(self) -> int:
        """Recover prepared inputs without affecting per-symbol workers."""

        if self._panel_runtime is None:
            return 0
        return self._panel_runtime.resume_prepared()

    def poll_panel_inputs(self) -> int:
        """Poll the durable data-plane boundary for complete panel revisions."""

        if self._panel_runtime is None:
            return 0
        return self._panel_runtime.poll_input_revisions()

    def relay_readiness(
        self,
        *,
        max_oldest_age_seconds: float = 300.0,
    ) -> SignalRelayReadiness:
        """Expose partition backlog readiness to the process health surface."""
        return self._signal_relay.readiness(
            max_oldest_age_seconds=max_oldest_age_seconds,
        )

    def _bootstrap(self) -> None:
        """Rebuild strategy state without acknowledging unprocessed bars.

        A first-ever worker start has no checkpoint, so recent history is fed
        through ``bootstrap_history`` (which suppresses historical entries) and
        the newest source bar becomes the initial watermark. On restart, the
        existing watermark is the last atomically journaled source bar. The
        history query is therefore capped at that timestamp and the watermark is
        left unchanged; later rows must traverse normal catch-up and journaling.

        This distinction is essential after a transaction failure. Feeding an
        uncommitted bar through bootstrap would mutate strategy
        duplicate/position state while suppressing its entry, then incorrectly
        acknowledge it and prevent recovery after restart.
        """
        raw_limit = self._bootstrap_bars
        if self._consolidation_minutes > 0 and self._timeframe == "1m":
            raw_limit = self._bootstrap_bars * self._consolidation_minutes

        instruments: dict[str, int] = {}
        states: dict[str, WatermarkState] = {}
        for symbol in self._symbols:
            instr_id = self._resolve_instrument(symbol)
            if instr_id is None:
                msg = f"Cannot bootstrap unknown configured instrument {symbol}"
                raise RuntimeError(msg)
            instruments[symbol] = instr_id
            state = self._watermark.get_state(symbol, self._timeframe)
            if state.instr_id is not None:
                self._watermark.validate_state(state, instr_id=instr_id)
            states[symbol] = state

        pending = [state for state in states.values() if state.rebuild_pending]
        if pending:
            try:
                # A strategy instance and its consolidators are shared across
                # symbols. Ensure every subscribed feed has a durable row, then
                # expand the earliest correction boundary to all of them.
                for symbol, state in list(states.items()):
                    if state.instr_id is None:
                        states[symbol] = self._watermark.ensure(
                            symbol,
                            self._timeframe,
                            instr_id=instruments[symbol],
                        )
                earliest = min(
                    state.rebuild_from_ts for state in pending if state.rebuild_from_ts is not None
                )
                rebuild_states = self._watermark.request_rebuilds(
                    [(symbol, self._timeframe, instruments[symbol]) for symbol in self._symbols],
                    rebuild_from=earliest,
                )
                self._bootstrap_historical_rebuild(
                    rebuild_states,
                    instruments=instruments,
                    raw_limit=raw_limit,
                )
            except HistoricalRebuildRequiredError as exc:
                self._mark_rebuild_required(exc)
                raise
        else:
            self._bootstrap_standard(
                states,
                instruments=instruments,
                raw_limit=raw_limit,
            )

        self._warmed_up = self._strategy.warmup_complete()
        if not self._warmed_up:
            logger.warning(
                "Strategy not fully warmed up after bootstrap; "
                "signals will be suppressed until warmup completes"
            )

    def _bootstrap_standard(
        self,
        states: dict[str, WatermarkState],
        *,
        instruments: dict[str, int],
        raw_limit: int,
    ) -> None:
        """Run the existing bounded startup path when no revision is pending."""
        had_durable_checkpoint = any(
            not self._watermark.is_initial(state.last_ts) for state in states.values()
        )
        latest_source_bar: Bar | None = None
        cold_start_targets: dict[str, datetime] = {}
        for symbol in self._symbols:
            state = states[symbol]
            is_cold_start = self._watermark.is_initial(state.last_ts)
            bars = self._fetch_historical_bars(
                symbol,
                limit=raw_limit,
                through=None if is_cold_start else state.last_ts,
            )
            if bars:
                candidate = bars[-1]
                if latest_source_bar is None or (
                    _coerce_timestamp(candidate.timestamp),
                    candidate.symbol,
                ) > (
                    _coerce_timestamp(latest_source_bar.timestamp),
                    latest_source_bar.symbol,
                ):
                    latest_source_bar = candidate
                strategy_bars = self._prepare_bootstrap_bars(symbol, bars)
                market_states = [self._bar_to_market_state(bar) for bar in strategy_bars]
                # Durable restarts restore the exact shared-model snapshot
                # after reconstructing indicators. Historical CLOSE signals
                # from that reconstruction were already decided before the
                # checkpoint and must not enter the new transaction buffer.
                with self._strategy.suppress_all_emissions():
                    self._strategy.bootstrap_history(market_states)
                # Only a true cold start may establish a checkpoint from
                # bootstrap. A restart retains its last acknowledged bar so
                # everything newer traverses normal signal delivery.
                if is_cold_start:
                    cold_start_targets[symbol] = bars[-1].timestamp
                logger.info(
                    "Bootstrapped %d bars for %s (through: %s, cold_start: %s)",
                    len(strategy_bars),
                    symbol,
                    bars[-1].timestamp,
                    is_cold_start,
                )
            elif state.instr_id is None:
                # Persist an explicit empty checkpoint. The first later
                # historical batch then receives a rebuild marker instead of
                # being streamed through the live path as months of new bars.
                cold_start_targets[symbol] = state.last_ts

        self._restore_or_initialize_runtime(
            states=states,
            instruments=instruments,
            cold_start_targets=cold_start_targets,
            latest_source_bar=latest_source_bar,
            had_durable_checkpoint=had_durable_checkpoint,
        )

    def _restore_or_initialize_runtime(
        self,
        *,
        states: dict[str, WatermarkState],
        instruments: dict[str, int],
        cold_start_targets: dict[str, datetime],
        latest_source_bar: Bar | None,
        had_durable_checkpoint: bool,
    ) -> None:
        """Restore a checkpoint or atomically establish the first snapshot."""
        if had_durable_checkpoint:
            restored = self._runtime_store.restore(
                self._strategy,
                checkpoint_ts_by_symbol={symbol: state.last_ts for symbol, state in states.items()},
            )
            if not restored:
                msg = f"Missing durable model state for acknowledged worker {self._worker_id!r}"
                raise RuntimeError(msg)
            return

        initialized_states: list[WatermarkState] = []
        with self._session_factory() as session:
            for symbol in sorted(cold_start_targets):
                initialized_states.append(
                    self._watermark.initialize_on_session(
                        session,
                        symbol,
                        self._timeframe,
                        cold_start_targets[symbol],
                        instr_id=instruments[symbol],
                    )
                )
            self._runtime_store.persist_initial_on_session(
                session,
                strategy=self._strategy,
                source_bar=latest_source_bar,
            )
            session.commit()
        for initialized in initialized_states:
            self._watermark.remember(initialized)

    def _bootstrap_historical_rebuild(
        self,
        states: list[WatermarkState],
        *,
        instruments: dict[str, int],
        raw_limit: int,
    ) -> None:
        """Replay authoritative history into this fresh core, then CAS-ack it."""
        by_symbol = {state.symbol: state for state in states}
        earliest_changed = min(
            state.rebuild_from_ts for state in states if state.rebuild_from_ts is not None
        )

        target_checkpoints: dict[tuple[str, str], datetime] = {}
        prefix_starts: list[datetime] = []
        for symbol in self._symbols:
            state = by_symbol[symbol]
            target = state.last_ts
            if self._watermark.is_initial(target):
                latest = self._ingestion_service.latest_candle_ts(
                    instruments[symbol],
                    source=self._source,
                    timeframe=self._timeframe,
                )
                if latest is not None:
                    target = _coerce_timestamp(latest)
            target_checkpoints[(symbol, self._timeframe)] = target
            prefix_through = min(earliest_changed, target)
            prefix = self._fetch_historical_bars(
                symbol,
                limit=raw_limit,
                through=prefix_through,
            )
            if prefix:
                prefix_starts.append(prefix[0].timestamp)

        replay_from = min(prefix_starts) if prefix_starts else earliest_changed
        replay_bars: list[Bar] = []
        latest_source_bar: Bar | None = None
        for symbol in self._symbols:
            target = target_checkpoints[(symbol, self._timeframe)]
            if self._watermark.is_initial(target):
                continue
            raw_bars = self._fetch_historical_range(
                symbol,
                start=replay_from,
                through=target,
            )
            if not raw_bars:
                msg = (
                    f"Historical rebuild found no authoritative bars for {symbol} "
                    f"through acknowledged checkpoint {target}"
                )
                raise HistoricalRebuildRequiredError(msg)
            candidate = raw_bars[-1]
            if latest_source_bar is None or (
                _coerce_timestamp(candidate.timestamp),
                candidate.symbol,
            ) > (
                _coerce_timestamp(latest_source_bar.timestamp),
                latest_source_bar.symbol,
            ):
                latest_source_bar = candidate
            replay_bars.extend(self._prepare_bootstrap_bars(symbol, raw_bars))

        replay_bars.sort(
            key=lambda bar: (
                _coerce_timestamp(bar.timestamp),
                bar.symbol,
                str(bar.source or ""),
            )
        )
        market_states = [
            self._bar_to_market_state(
                bar,
                processing_timestamp=bar.timestamp,
            )
            for bar in replay_bars
        ]
        # Historical correction replay is a candidate state reconstruction.
        # Neither entries nor reduce-only exits may reach scoring before the
        # captured generation is acknowledged.
        self._strategy.rebuild_model_history(market_states)
        with self._session_factory() as session:
            self._runtime_store.persist_rebuild_on_session(
                session,
                strategy=self._strategy,
                source_bar=latest_source_bar,
            )
            acknowledged = self._watermark.acknowledge_rebuilds_on_session(
                session,
                states,
                target_last_ts=target_checkpoints,
            )
            session.commit()
        for state in acknowledged:
            self._watermark.remember(state)
        for state in acknowledged:
            current = self._watermark.get_state(state.symbol, state.timeframe)
            if current.rebuild_pending:
                msg = (
                    f"Historical mutation arrived while rebuilding {state.symbol}/{state.timeframe}"
                )
                raise RebuildGenerationChangedError(msg)
        logger.warning(
            "Historical price rebuild completed",
            worker_id=self._worker_id,
            replay_from=replay_from,
            replay_through={
                symbol: target_checkpoints[(symbol, self._timeframe)].isoformat()
                for symbol in self._symbols
            },
            strategy_bars=len(replay_bars),
        )

    def _fetch_historical_range(
        self,
        symbol: str,
        *,
        start: datetime,
        through: datetime,
    ) -> list[Bar]:
        """Fetch an authoritative inclusive replay range; DB errors are fatal."""
        with self._session_factory() as session:
            instr = self._resolve_instrument(symbol)
            if instr is None:
                msg = f"No instrument found for historical rebuild symbol {symbol}"
                raise HistoricalRebuildRequiredError(msg)
            rows = self._ingestion_service.fetch_range(
                session,
                instr_id=instr,
                source=self._source,
                timeframe=self._timeframe,
                start=start,
                through=through,
            )
            return [
                _database_row_to_bar(
                    row,
                    symbol=symbol,
                    timeframe=self._timeframe,
                    source=self._source,
                )
                for row in rows
            ]

    def _mark_rebuild_required(self, exc: BaseException) -> None:
        if self._terminal_error is None:
            self._terminal_error = exc
        self._rebuild_event.set()
        logger.warning(
            "Historical price revision requires a supervised fresh-core rebuild",
            worker_id=self._worker_id,
            error=str(exc),
        )

    def _fetch_historical_bars(
        self,
        symbol: str,
        limit: int,
        *,
        through: datetime | None = None,
    ) -> list[Bar]:
        """Fetch recent bars, optionally capped at an acknowledged timestamp."""
        try:
            with self._session_factory() as session:
                instr = self._resolve_instrument(symbol)
                if instr is None:
                    logger.warning("No instrument found for %s", symbol)
                    return []

                rows = self._ingestion_service.fetch_recent_bars(
                    session,
                    instr_id=instr,
                    source=self._source,
                    timeframe=self._timeframe,
                    limit=limit,
                    through=through,
                )
                return [
                    _database_row_to_bar(
                        row,
                        symbol=symbol,
                        timeframe=self._timeframe,
                        source=self._source,
                    )
                    for row in rows  # already oldest first
                ]
        except (SQLAlchemyError, OSError):
            logger.exception("Failed to fetch historical bars for %s", symbol)
            return []

    def _start_listener(self) -> None:
        """Start LISTEN/NOTIFY thread."""
        from lib_data.pg_notify import PgNotifyListener  # noqa: PLC0415

        self._listener = PgNotifyListener(
            dsn=self._dsn,
            channel=self._notify_channel,
            callback=self._on_notify,
        )
        self._listener_thread = threading.Thread(
            target=self._listener.run_forever,
            daemon=True,
            name=f"signal-worker-listener-{self._worker_id}",
        )
        self._listener_thread.start()

    def _on_notify(self, _channel: str, payload: dict[str, Any]) -> None:
        """Handle a NOTIFY event — catch up on new bars since watermark."""
        if payload.get("type") == "catchup" or not payload.get("symbol"):
            # Periodic catch-up: check all symbols
            for symbol in self._symbols:
                self._catchup_symbol(symbol)
        else:
            symbol = payload.get("symbol", "").upper()
            if symbol in self._symbols:
                self._catchup_symbol(symbol)

    def _catchup_symbol(self, symbol: str) -> None:
        """Serialize the worker's fetch/process/acknowledge state transition."""
        symbol = symbol.strip().upper()
        if symbol not in self._symbols:
            logger.warning("Ignoring catch-up for unsubscribed symbol %s", symbol)
            return

        with self._processing_lock:
            # Another catch-up may have been waiting while a delivery exhausted
            # retries. Do not mutate the already-failed strategy again while the
            # process-level supervisor is preparing to restart it.
            if self.failed:
                return
            self._catchup_symbol_locked(symbol)

    def _catchup_symbol_locked(self, symbol: str) -> None:
        """Fetch and process bars while the caller holds the symbol lock."""
        instr_id = self._resolve_instrument(symbol)
        if instr_id is None:
            msg = f"Cannot catch up unknown configured instrument {symbol}"
            raise RuntimeError(msg)
        try:
            state = self._watermark.get_state(symbol, self._timeframe)
            self._watermark.validate_state(state, instr_id=instr_id)
            if state.rebuild_pending:
                msg = (
                    f"Historical rebuild pending for {symbol}/{self._timeframe} "
                    f"from {state.rebuild_from_ts} generation={state.rebuild_generation}"
                )
                raise HistoricalRebuildRequiredError(msg)  # noqa: TRY301
            new_bars = self._fetch_bars_since(symbol, state.last_ts)
            if not new_bars:
                return

            for bar in new_bars:
                self._process_and_ack_source_bar(
                    symbol,
                    bar,
                    instr_id=instr_id,
                )
                # A different symbol may have exhausted delivery retries while
                # this thread waited for the shared strategy lock. Never
                # acknowledge work after terminal state.
                if self.failed:
                    return
        except HistoricalRebuildRequiredError as exc:
            self._mark_rebuild_required(exc)
            raise

    def _process_and_ack_source_bar(
        self,
        symbol: str,
        bar: Bar,
        *,
        instr_id: int,
    ) -> None:
        """Fence one live source bar against concurrent persisted corrections."""
        price_id = int(bar.metadata.get("price_id") or 0)
        expected_revision = int(bar.metadata.get("content_revision") or 0)
        if price_id <= 0 or expected_revision <= 0:
            msg = f"Source bar {symbol}/{bar.timestamp} has no durable content revision"
            raise HistoricalRebuildRequiredError(msg)

        state_mutated = False
        if self._signal_buffer.pending() or self._uncommitted_decisions:
            msg = "Durable strategy transaction began with uncommitted process state"
            raise RuntimeError(msg)
        try:
            with self._session_factory() as session:
                if not _lock_current_source_bar(
                    session,
                    ingestion=self._ingestion_service,
                    price_id=price_id,
                    instr_id=instr_id,
                    timestamp=bar.timestamp,
                    timeframe=self._timeframe,
                    source=self._source,
                    content_revision=expected_revision,
                ):
                    msg = f"Source bar {price_id} changed before strategy processing"
                    raise HistoricalRebuildRequiredError(msg)  # noqa: TRY301

                checkpoint = self._watermark.lock_for_processing(
                    session,
                    symbol,
                    self._timeframe,
                    instr_id=instr_id,
                )
                # From this point onward the in-memory core or consolidator may
                # diverge from the durable checkpoint. Any subsequent fence or
                # commit failure must retire this process; retrying in-place
                # would apply the same source bar to already-mutated state.
                state_mutated = True
                if self._consolidation_minutes > 0 and symbol in self._consolidators:
                    self._consolidators[symbol].update(bar)
                else:
                    self._process_bar(bar)
                if self.failed:
                    return

                self._runtime_store.persist_transition_on_session(
                    session,
                    strategy=self._strategy,
                    source_bar=bar,
                    decisions=tuple(self._uncommitted_decisions),
                )
                advanced = self._watermark.advance_locked(
                    session,
                    checkpoint,
                    bar.timestamp,
                )
                session.commit()
        except Exception as exc:
            if state_mutated and not self.failed:
                self._mark_state_transition_failure(exc, symbol=symbol, bar=bar)
            raise
        self._signal_buffer.clear()
        self._uncommitted_decisions.clear()
        self._watermark.remember(advanced)
        self.relay_once()

    def _mark_state_transition_failure(
        self,
        exc: BaseException,
        *,
        symbol: str,
        bar: Bar,
    ) -> None:
        """Retire a core whose mutation could not be durably acknowledged."""
        if self._terminal_error is None:
            self._terminal_error = exc
        self._failure_event.set()
        logger.error(
            "Signal worker state mutated but checkpoint commit failed; "
            "fresh-core process restart required",
            worker_id=self._worker_id,
            symbol=symbol,
            bar_timestamp=_coerce_timestamp(bar.timestamp).isoformat(),
            error=str(exc),
        )

    def _mark_panel_transition_failure(
        self,
        exc: BaseException,
        decision_key: str,
    ) -> None:
        """Retire a core mutated by a panel transaction that did not commit."""

        if self._terminal_error is None:
            self._terminal_error = exc
        self._failure_event.set()
        logger.error(
            "Panel model state mutated but its journal transaction failed; "
            "fresh-core process restart required",
            worker_id=self._worker_id,
            panel_decision_key=decision_key,
            error=str(exc),
        )

    def catchup_all(self) -> None:
        """Catch up every symbol from its watermark, independent of NOTIFY.

        PostgreSQL ``NOTIFY`` is best-effort — it is not delivered to a
        disconnected listener — so a missed/dropped notification would otherwise
        leave new ``prices`` rows unprocessed until the next one arrives,
        silently stalling the strategy. The worker main loop calls this on a
        periodic floor so a missed NOTIFY only delays a bar by the poll interval
        instead of indefinitely; NOTIFY becomes a latency optimization, not the
        sole trigger. Transient DB/OS failures remain per-symbol; malformed price
        data propagates so the process supervisor restarts a fail-closed worker.
        """
        if self._panel_runtime is not None:
            self._panel_runtime.poll_input_revisions()
            return
        for symbol in self._symbols:
            try:
                self._catchup_symbol(symbol)
            except (SQLAlchemyError, OSError):
                logger.exception("Periodic catch-up failed for %s", symbol)

    def _fetch_bars_since(self, symbol: str, since: datetime) -> list[Bar]:
        """Fetch bars newer than the given timestamp."""
        try:
            with self._session_factory() as session:
                instr = self._resolve_instrument(symbol)
                if instr is None:
                    return []

                rows = self._ingestion_service.fetch_bars_since(
                    session,
                    instr_id=instr,
                    source=self._source,
                    timeframe=self._timeframe,
                    since=since,
                    limit=self._catchup_batch_size,
                )
                return [
                    _database_row_to_bar(
                        row,
                        symbol=symbol,
                        timeframe=self._timeframe,
                        source=self._source,
                    )
                    for row in rows
                ]
        except (SQLAlchemyError, OSError):
            logger.exception("Failed to fetch bars since %s for %s", since, symbol)
            return []

    def _on_consolidated_bar(self, bar: Bar) -> None:
        """Handle a consolidated bar from BarConsolidator."""
        self._process_bar(bar)

    def _process_bar(self, bar: Bar) -> None:
        """Feed a bar to the strategy and let the strategy gate emission.

        Signals land in the transaction-local ``BufferedSignalEmitter``; the
        caller then commits the post-bar model snapshot, source watermark,
        decision record, and signal envelopes together before the relay
        performs HTTP. A strategy exception propagates so the caller retires
        the mutated core instead of retrying the bar in place.
        """
        with self._processing_lock:
            # Re-check under the shared lock: another symbol can fail while this
            # one is queued behind it. Its bar remains unacknowledged for restart.
            if self.failed:
                return
            self._process_bar_locked(bar)

    def _process_bar_locked(self, bar: Bar) -> None:
        """Mutate the shared strategy while the caller holds its worker lock."""
        state = self._bar_to_market_state(bar, processing_timestamp=self._clock())
        self._strategy.record_bar(state.symbol)
        signal_offset = self._signal_buffer.count()
        self._strategy.on_data(state)
        self._strategy.flush()
        emitted = self._signal_buffer.since(signal_offset)
        self._uncommitted_decisions.append(
            StrategyBarDecision(
                symbol=state.symbol,
                strategy_bar_ts=state.timestamp,
                signals=emitted,
            )
        )
        self._warmed_up = self._strategy.warmup_complete()
        _record_processed_bar(
            strategy_id=self._strategy.strategy_id,
            timeframe=bar.timeframe,
        )

    def _bar_to_market_state(
        self,
        bar: Bar,
        *,
        processing_timestamp: datetime | None = None,
    ) -> MarketState:
        metadata: dict[str, Any] = dict(bar.metadata or {})
        metadata.update(
            {
                "timeframe": bar.timeframe,
                "source": bar.source,
                # Signals and durable paper orders must reference the exact
                # persisted source feed, not a derived strategy-bar interval
                # that has no corresponding row in ``prices``.
                "price_timeframe": self._timeframe,
                "price_source": self._source,
                "source_price_ts": metadata.get("source_price_ts")
                or _coerce_timestamp(bar.timestamp).isoformat(),
                "asset_class": self._asset_class,
            }
        )
        if processing_timestamp is not None:
            metadata["processing_timestamp"] = _coerce_timestamp(processing_timestamp)
        return MarketState(
            symbol=bar.symbol,
            timestamp=bar.timestamp,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            metadata=metadata,
        )

    def _prepare_bootstrap_bars(self, symbol: str, bars: list[Bar]) -> list[Bar]:
        """Prepare bootstrap bars while preserving live consolidator state."""
        if self._consolidation_minutes <= 0 or symbol not in self._consolidators:
            return bars

        consolidated: list[Bar] = []
        consolidator = self._consolidators[symbol]
        original_callback = consolidator.replace_callback(None)
        try:
            for bar in bars:
                emitted = consolidator.update(bar)
                if emitted is not None:
                    consolidated.append(emitted)
        finally:
            consolidator.replace_callback(original_callback)
        logger.debug(
            "Prepared bootstrap bars",
            worker_id=self._worker_id,
            symbol=symbol,
            raw_bars=len(bars),
            strategy_bars=len(consolidated),
        )
        return consolidated

    def _resolve_instrument(self, symbol: str) -> int | None:
        canonical = symbol.strip().upper().replace("-", "").replace("/", "")
        cached = self._instrument_map.get(canonical)
        if cached is not None:
            return int(cached)
        resolved = self._ingestion_service.resolve_instrument(
            symbol,
            asset_class=self._asset_class,
        )
        if resolved is not None:
            self._instrument_map[canonical] = int(resolved)
            return int(resolved)
        return None


def _load_strategy_config(strategy_path: Path) -> dict[str, Any]:
    config_path = strategy_path / "config.json"
    with config_path.open() as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        return payload
    msg = f"Expected object in strategy config: {config_path}"
    raise ValueError(msg)


def parse_strategy_symbols(raw_universe: Any) -> list[str]:
    """Normalize a schema-valid string or array universe."""
    candidates: tuple[Any, ...]
    if isinstance(raw_universe, str):
        candidates = tuple(raw_universe.split(","))
    elif isinstance(raw_universe, (list, tuple)):
        candidates = tuple(raw_universe)
    else:
        msg = "strategy parameters.universe must be a string or array of strings"
        raise TypeError(msg)
    symbols = list(
        dict.fromkeys(str(symbol).strip().upper() for symbol in candidates if str(symbol).strip())
    )
    if not symbols:
        msg = "strategy parameters.universe must contain at least one symbol"
        raise ValueError(msg)
    return symbols


def _coerce_timestamp(value: Any) -> datetime:
    timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    # ``prices.ts`` is stored as a timezone-naive PostgreSQL timestamp but its
    # contract is UTC.  Never leak a naive datetime into session gates,
    # deterministic signal IDs, or the HTTP payload.
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _lock_current_source_bar(
    session: Any,
    *,
    ingestion: PriceIngestionService,
    price_id: int,
    instr_id: int,
    timestamp: datetime,
    timeframe: str,
    source: str,
    content_revision: int,
) -> bool:
    """Fence one source revision without granting the indicator price mutation."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        return bool(
            session.execute(
                text(
                    """
                    SELECT public.vm_indicator_lock_source_price(
                        :price_id,
                        :instr_id,
                        :timestamp,
                        :timeframe,
                        :source,
                        :content_revision
                    )
                    """
                ),
                {
                    "price_id": price_id,
                    "instr_id": instr_id,
                    "timestamp": _coerce_timestamp(timestamp).replace(tzinfo=None),
                    "timeframe": timeframe,
                    "source": source,
                    "content_revision": content_revision,
                },
            ).scalar_one()
        )

    current = ingestion.get_bar_identity(session, price_id)
    return current is not None and (
        int(current.instr_id) == instr_id
        and str(current.source) == source
        and str(current.timeframe) == timeframe
        and _coerce_timestamp(current.ts) == _coerce_timestamp(timestamp)
        and int(current.content_revision) == content_revision
    )


def _database_row_to_bar(row: Any, *, symbol: str, timeframe: str, source: str) -> Bar:
    raw_values = (row.open, row.high, row.low, row.close, row.volume)
    if any(value is None for value in raw_values):
        msg = f"Stored candle symbol={symbol} ts={row.ts} is missing required OHLCV"
        raise ValueError(msg)
    open_price, high, low, close, volume = (float(value) for value in raw_values)
    invariant_error = ohlcv_invariant_error(
        open_price=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )
    if invariant_error is not None:
        msg = f"Stored candle symbol={symbol} ts={row.ts} {invariant_error}"
        raise ValueError(msg)
    return Bar(
        symbol=symbol,
        timestamp=_coerce_timestamp(row.ts),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        timeframe=timeframe,
        source=source,
        metadata={
            "price_id": int(row.price_id),
            "content_revision": int(row.content_revision),
        },
    )


def _resolve_strategy_id(config: dict[str, Any], *, name: str = "") -> str:
    """Resolve the required canonical strategy id from top-level config."""
    strategy_id = config.get("strategy_id")
    if not strategy_id:
        msg = f"strategy_id missing in config.json for {name or 'strategy'}"
        raise ValueError(msg)
    return str(strategy_id)


def _build_worker(
    strategy_path: Path,
    worker_id: str | None = None,
    *,
    startup_config: IndicatorWorkerConfig | None = None,
) -> tuple[SignalWorker, Engine]:
    config = _load_strategy_config(strategy_path)
    params = dict(config.get("parameters") or {})
    market_data = dict(config.get("market_data") or {})
    feedback_loop = dict(config.get("feedback_loop") or {})
    startup = startup_config or load_indicator_worker_config()

    strategy_id = _resolve_strategy_id(config, name=strategy_path.name)
    strategy_version = config.get("strategy_version")
    if not strategy_version:
        msg = f"strategy_version missing in config.json for {strategy_path.name}"
        raise ValueError(msg)
    # The top-level value is the single source of truth; strategy cores receive
    # an immutable copy through their runtime parameter mapping.
    params["strategy_version"] = str(strategy_version)
    if config.get("trade_direction_mode"):
        params["trade_direction_mode"] = config["trade_direction_mode"]
    if feedback_loop.get("evaluation_horizon"):
        # The emitted signal is the authoritative evaluation-horizon contract.
        # Copy the top-level operational setting into the core parameters so
        # runtime signals cannot silently disagree with their config.
        params["evaluation_horizon"] = feedback_loop["evaluation_horizon"]
    # Signal.source describes the runtime environment, not the market-data
    # vendor.  The latter remains in metadata.price_source.  Supplying this at
    # the runner boundary also leaves direct historical/backtest construction
    # free to use its own attribution.
    params["signal_source"] = startup.run_mode.value

    signal_api_url = (
        startup.signal_api_url or params.get("signal_api_url") or "http://scoring-engine:8001"
    )
    # Canonical shared key is API_KEY (matches the scoring/execution servers and
    # the outbox relay); the runner injects it into every strategy subprocess.
    delivery_emitter = HttpSignalEmitter(
        base_url=normalize_signal_api_base_url(signal_api_url),
        api_key=startup.api_key,
        strategy_type="indicator",
        max_retries=startup.signal_http_max_retries,
        retry_base_delay=startup.signal_http_retry_base_delay_seconds,
        retry_max_delay=startup.signal_http_retry_max_delay_seconds,
        retry_jitter=startup.signal_http_retry_jitter,
        on_emitted=_record_emitted_signal,
    )
    signal_buffer = BufferedSignalEmitter()

    core_cls = load_pure_strategy_core(strategy_path)
    strategy = core_cls(
        strategy_id=strategy_id,
        strategy_type="indicator",
        config=params,
        emitter=signal_buffer,
    )

    db_url = startup.database_url
    engine = create_engine_for_env(db_url=db_url)
    session_factory = get_session_factory(engine=engine)

    runtime_params = StrategyRuntimeParams.from_config(params, market_data)
    panel_capable = isinstance(strategy, SynchronizedPanelStrategy)
    symbols = [] if panel_capable else parse_strategy_symbols(runtime_params.universe)
    base_worker_id = worker_id or f"{strategy_path.name}:{startup.run_mode.value}"
    panel_binding: PanelRuntimeBinding | None = None
    resolved_worker_id = base_worker_id
    if panel_capable:
        configured_binding = startup.panel_runtime
        if configured_binding is None:
            msg = (
                "Synchronized panel strategy requires INDICATOR_PANEL_DATA_USE_SCOPE, "
                "INDICATOR_PANEL_ENTITLEMENT_OWNER_USER_ID (or "
                "SP500_RESEARCH_OWNER_USER_ID), and INDICATOR_PANEL_ACTIVATION_CUTOFF"
            )
            raise ValueError(msg)
        if startup.run_mode.value != "paper":
            msg = "The owner-scoped synchronized panel runtime is paper-only"
            raise ValueError(msg)
        panel_binding = PanelRuntimeBinding(
            environment=configured_binding.environment,
            data_use_scope=configured_binding.data_use_scope,
            entitlement_owner_user_id=configured_binding.entitlement_owner_user_id,
            activation_cutoff=configured_binding.activation_cutoff,
        )
        resolved_worker_id = scoped_panel_worker_id(base_worker_id, panel_binding)
    runtime_identity = StrategyRuntimeIdentity.from_strategy(
        worker_id=resolved_worker_id,
        strategy=strategy,
        symbols=symbols,
        source=runtime_params.source,
        timeframe=runtime_params.timeframe,
        consolidation_minutes=runtime_params.consolidation_minutes,
        panel_capable=panel_capable,
        panel_binding=panel_binding,
    )
    runtime_store = StrategyRuntimeStore(session_factory, runtime_identity)
    signal_relay = DurableSignalRelay(
        outbox=OutboxStore(session_factory),
        emitter=delivery_emitter,
        worker_id=resolved_worker_id,
        strategy_id=strategy_id,
        ordering_key=runtime_identity.ordering_key,
    )

    worker = SignalWorker(
        strategy=strategy,
        session_factory=session_factory,
        worker_id=resolved_worker_id,
        symbols=symbols,
        consolidation_minutes=runtime_params.consolidation_minutes,
        dsn=db_url,
        notify_channel=startup.notify_channel,
        bootstrap_bars=runtime_params.bootstrap_bars,
        source=runtime_params.source,
        timeframe=runtime_params.timeframe,
        min_bar_coverage=startup.min_bar_coverage,
        catchup_batch_size=startup.catchup_batch_size,
        catchup_floor_seconds=startup.catchup_floor_seconds,
        asset_class=runtime_params.asset_class,
        signal_buffer=signal_buffer,
        runtime_store=runtime_store,
        signal_relay=signal_relay,
    )
    return worker, engine


def main(argv: list[str] | None = None) -> int:
    # Each strategy runs as its own signal_worker subprocess; without configuring
    # logging here the subprocess root logger defaults to WARNING and drops every
    # per-strategy INFO line (bootstrap, warmup, "Posted signal ..."). That is why
    # the soak's indicator-runner stdout was a single line despite 866 emissions
    # (O4). setup_logging wires the stdout handler at LOG_LEVEL (default INFO),
    # and the process_manager already streams this to the container's docker logs.
    setup_logging(load_indicator_log_level())

    parser = argparse.ArgumentParser(description="Run a PureSignalStrategy signal worker")
    parser.add_argument("--strategy-path", required=True, help="Path to the strategy directory")
    parser.add_argument("--worker-id", default=None, help="Optional worker identifier override")
    args = parser.parse_args(argv)

    strategy_path = Path(args.strategy_path).resolve()
    worker, db_engine = _build_worker(strategy_path, worker_id=args.worker_id)
    stop_event = threading.Event()

    def _shutdown(signum: int, _frame: object) -> None:
        logger.info("Stopping signal worker after signal %s", signum, worker_id=args.worker_id)
        worker.stop()
        stop_event.set()

    os_signal.signal(os_signal.SIGTERM, _shutdown)
    os_signal.signal(os_signal.SIGINT, _shutdown)

    # Self-healing floor: catch up every symbol on this cadence regardless of
    # NOTIFY, so a missed/dropped notification delays a bar by at most this many
    # seconds instead of stalling the strategy indefinitely.
    catchup_floor_sec = worker.catchup_floor_seconds
    exit_code = 0
    try:
        worker.start()
        last_catchup = time.monotonic()
        while not stop_event.is_set():
            if worker.failed:
                if worker.rebuild_required:
                    logger.warning(
                        "Signal worker exiting for supervised historical rebuild",
                        worker_id=args.worker_id,
                        error=str(worker.terminal_error),
                    )
                    exit_code = HISTORICAL_REBUILD_EXIT_CODE
                else:
                    logger.error(
                        "Signal worker exiting after terminal delivery failure",
                        worker_id=args.worker_id,
                        error=str(worker.terminal_error),
                    )
                    exit_code = 1
                break
            if (
                not stop_event.is_set()
                and worker._listener_thread
                and not worker._listener_thread.is_alive()
                and worker._dsn
            ):
                logger.error("Signal worker listener thread stopped unexpectedly")
                exit_code = 1
                break
            if stop_event.wait(1.0):
                break
            worker.relay_once()
            if time.monotonic() - last_catchup >= catchup_floor_sec:
                worker.catchup_all()
                last_catchup = time.monotonic()
    except HistoricalRebuildRequiredError as exc:
        worker._mark_rebuild_required(exc)
        exit_code = HISTORICAL_REBUILD_EXIT_CODE
    finally:
        worker.stop()
        dispose_engine(db_engine)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
