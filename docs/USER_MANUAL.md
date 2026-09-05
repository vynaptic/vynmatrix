# vynmatrix User Manual

> **Purpose**: Architecture and code walkthrough for contributors
> **Scope**: Independent local migration; no inherited deployment or certification
> **Repository**: vynmatrix (hosting owner not yet configured)

The project is not yet open-source: [LICENSE](../LICENSE) remains unchanged and
a license/rights decision is pending. Examples describe local paper development;
none authorize publishing, deployment, or changing a live-order gate.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Architecture Overview](#2-architecture-overview)
3. [Quick Navigation](#3-quick-navigation)
4. [Root Configuration Files](#4-root-configuration-files)
5. [Libraries (libs/)](#5-libraries-libs)
6. [Trading Strategies (strategies/)](#6-trading-strategies-strategies)
7. [Applications (apps/)](#7-applications-apps)
8. [Developer CLI (tools/)](#8-developer-cli-tools)
9. [Configuration (config/)](#9-configuration-config)
10. [Scripts (scripts/)](#10-scripts-scripts)
11. [Docker Infrastructure (docker/)](#11-docker-infrastructure-docker)
12. [Database Schema](#12-database-schema)
13. [Dependency Graph](#13-dependency-graph)
14. [End-to-End Flow Verification](#14-end-to-end-flow-verification)

---

## 1. Executive Summary

**vynmatrix** contains a **signal-only** strategy architecture that:

- **Generates trading signals** from the reviewed indicator fleet
- **Scores signals** through three stages: canonical scoring view, meta-labeling, and ensemble aggregation
- **Routes execution** through a centralized execution engine to multiple brokers (Coinbase, Deribit, IBKR, Saxo, Delta, Zerodha)
- **Closes the loop** with a feedback loop that evaluates signal performance and suggests parameter refinements

### Key Metrics

| Metric | Value (approx) |
|--------|----------------|
| Libraries | 6 shared wheels |
| Strategies | Indicator signal cores |
| Applications | Runners + scoring engine + execution engine + support services |
| Database | PostgreSQL in all runtime environments (SQLite only for unit tests) |
| Brokers | Coinbase, Deribit, IBKR, Saxo, Delta, Zerodha (adapters) |

### Technology Stack

```
┌─────────────────────────────────────────────────────────────┐
│                      PRESENTATION LAYER                      │
│    FastAPI REST APIs  │  Health Endpoints  │  CLI (vmdev)   │
├─────────────────────────────────────────────────────────────┤
│                      APPLICATION LAYER                       │
│  Scoring Engine  │  Execution Engine  │  Feedback Loop      │
├─────────────────────────────────────────────────────────────┤
│                       STRATEGY LAYER                         │
│                    Indicator (SignalWorker)                    │
├─────────────────────────────────────────────────────────────┤
│                      DOMAIN LAYER                            │
│  Signals  │  Scores  │  Decisions  │  Orders  │  Positions │
├─────────────────────────────────────────────────────────────┤
│                    INFRASTRUCTURE LAYER                      │
│  PostgreSQL  │  Broker Adapters  │  Docker Compose            │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Architecture Overview

### Signal-Only Architecture

All strategies operate in **signal-only mode**—they emit standardized signals and never execute orders directly. This separation enables:

1. **Centralized scoring** – Three stages (`Signal`/`ScoringSignalView` → `MetaLabelOutput` → `GlobalScore`)
2. **Flexible execution** – Binding evaluation selects the permitted mode; the execution engine builds and sizes the concrete order
3. **Audit trail** – Signals/scores persisted in Postgres (`canonical_signals`, `asset_scores`, `sector_scores`, `market_scores`)
4. **A/B testing** – Swap execution modes or weights without changing strategy code

Cross-sectional portfolio strategies use the same separation with a batch
contract: one synchronized panel produces an auditable model rebalance, scoring
materializes authorized account plans, and execution processes exits/reductions
before entries. A strategy still cannot place or route an order directly.

**Recent foundations**

- **Canonical Signal**: `lib_strategy.signals.Signal` carries expected_return, predicted_risk, horizon_days when available; scoring engine derives a scoring view (direction + μ/σ/horizon defaults) via `lib_strategy.signals.adapters` and persists to `canonical_signals`.
- **Pipeline-aware scoring engine**: `/api/v1/signals` accepts the canonical
  strategy insight payload, derives the scoring view adapter, runs meta-label +
  ensemble, and persists scores with components.
- **PostgreSQL-first**: Dev via Docker (`vmdev db start`), migrations in `scripts/db/alembic`, consolidated doc in `docs/DATABASE.md`.
- **Feedback loop**: The feedback loop hooks to signal performance, tracks consecutive-wrong predictions, and emits parameter-adjustment suggestions. Approval is an audit decision; accepted changes are promoted through source control, validation, image build, and deployment.

### Data Flow

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Strategy   │────▶│   Scoring    │────▶│  Execution   │────▶│   Broker     │
│   Runners    │     │   Engine     │     │   Engine     │     │   Adapters   │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
       │                    │                    │                    │
       ▼                    ▼                    ▼                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                           PostgreSQL Database                             │
│  canonical_signals │ asset_scores  │ orders │ executions │ positions     │
└──────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                            ┌──────────────────┐
                            │  Feedback Loop   │
                            │     Engine       │
                            └──────────────────┘
```

### Signal Client Architecture

Strategy cores build canonical signal envelopes without performing network I/O.
The indicator runtime commits each envelope with model state, source watermark,
and bar decision before `DurableSignalRelay` submits the scoring wire payload to
`/api/v1/signals`. `HttpSignalEmitter` is the relay's HTTP transport; the
scoring API's `InsightSignalPayload` owns validation at the receiving boundary.

```json
{
  "ts": "2024-01-15T10:30:00Z",
  "strategy_id": "rsi_ema_v3",
  "symbol": "SPY",
  "insight": {
    "direction": "Up",
    "magnitude": 0.012,
    "confidence": 0.73,
    "horizon": "1D"
  },
  "context": {
    "regime": "risk_on"
  }
}
```

| Context | Client | Module |
|---------|--------|--------|
| Pure / DB-fed indicator strategies | `BufferedSignalEmitter` + `StrategyRuntimeStore` | `apps/indicator_runner/indicator_runner/runtime_journal.py` |
| Durable strategy-to-scoring delivery | `DurableSignalRelay` using `HttpSignalEmitter` | `apps/indicator_runner/indicator_runner/runtime_journal.py` |
| Receiver schema | `InsightSignalPayload` | `apps/scoring_engine/scoring_engine/schemas.py` |

**Canonical sender**: a strategy emits into `BufferedSignalEmitter`; the
`SignalWorker` atomically commits its versioned model state, source watermark,
bar decision, and stable signal envelope. `DurableSignalRelay` is the only
deployable HTTP sender and uses `HttpSignalEmitter` after commit (with
`LogSignalEmitter` reserved for capture/test). The legacy
`AsyncSignalClient` / `SyncSignalClient` / `SignalClientFactory` were removed.

### End-to-End Sequence Diagram (Signal → Scoring → DB → Execution → Feedback)

The sequence below follows `strategies/indicator/SwingHighLowPMO/core.py` in
signal-only mode through both transactional outbox boundaries and the durable
local-paper order lifecycle.

```mermaid
sequenceDiagram
    autonumber
    participant Market as prices + NOTIFY
    participant Worker as SignalWorker
    participant Strategy as SwingHighLowPMO core
    participant Journal as Strategy journal/outbox
    participant SignalRelay as DurableSignalRelay
    participant ScoringAPI as Scoring Engine API
    participant ScoreEngine as ScoreEngine
    participant DB as PostgreSQL
    participant CommandRelay as Scoring outbox relay
    participant ExecEngine as ExecutionEngine
    participant Broker as Broker Adapter
    participant PaperLifecycle as Paper order lifecycle
    participant Feedback as FeedbackLoopEngine

    Market->>Worker: Committed real source bar/revision
    Worker->>Strategy: Deterministic consolidated MarketState
    Strategy-->>Worker: Zero or more canonical Signals
    Worker->>Journal: Atomic state + watermark + decision + signal envelope
    SignalRelay->>Journal: Fenced claim of committed envelope
    SignalRelay->>ScoringAPI: POST /api/v1/signals with stable external ID
    ScoringAPI->>ScoreEngine: Normalize, score, evaluate exact user bindings
    ScoreEngine->>DB: Atomic signal + scores + decisions + command outbox
    ScoringAPI-->>SignalRelay: Stable identity acknowledged
    CommandRelay->>DB: Fenced claim execution.commands
    CommandRelay->>ExecEngine: Account-scoped ExecutionCommandEvent
    ExecEngine->>DB: Claim decision; revalidate current authority; persist intent/client ID
    ExecEngine->>Broker: Place authorized order
    alt immediately filled
        Broker-->>ExecEngine: Exact broker fill
        ExecEngine->>DB: Canonical order/fill + cash/position/P&L/NAV/event
    else durable local-paper working order
        Broker-->>DB: Persist working stop/limit/OCO state
        Market->>PaperLifecycle: Later committed real source bar
        PaperLifecycle->>DB: Exact idempotent fill + OCO/accounting projections
    end
    Feedback->>DB: Join signal → decision/account → intent/order → fills
    Feedback->>DB: Upsert exact signal/horizon evaluation + scoped mode performance
```

**Key guarantees for user-specific execution**

- **Bindings first**: Execution only occurs when asset/sector/market thresholds pass.
- **Mode-aware routing**: Mode selection and execution profile are resolved per user.
- **Audit-first persistence**: model decisions, signals, scores, user
  decisions, commands, orders, fills, and feedback lineage are DB-backed.
- **Effectively once**: stable external signal, command, client-order, trade,
  and evaluation identities make retries observable and idempotent.
- **Closed loop**: Feedback loop reads DB state and writes performance metrics for future decisions.

---

## 3. Quick Navigation

### By Role

| Role | Start Here |
|------|------------|
| **New Developer** | [SETUP_MAC_LINUX.md](../SETUP_MAC_LINUX.md) or [SETUP_WINDOWS.md](../SETUP_WINDOWS.md) → [Section 5: Libraries](#5-libraries-libs) |
| **Quant/Strategy Developer** | [Section 6: Strategies](#6-trading-strategies-strategies) |
| **DevOps/Platform** | [Section 11: Docker](#11-docker-infrastructure-docker) |
| **Board Member** | [Section 1](#1-executive-summary) → [Section 14: E2E Flow](#14-end-to-end-flow-verification) |

### By Task

| Task | Location |
|------|----------|
| Add new indicator strategy | [strategies/indicator/_template/](../strategies/indicator/_template/) |
| Build all components | `vmdev build libs && vmdev build strategies && vmdev build docker --from-config --tag latest` ([Section 8](#8-developer-cli-tools)) |
| Validate a strategy | Run the `signal_worker` against historical/live `prices` in paper mode and inspect emitted signals + scores in Postgres ([Section 6.1](#61-indicator-strategies-signalworker)) |
| View database schema | [Section 12](#12-database-schema) |
| Inspect local Docker and future release boundaries | [docs/DEPLOYMENT.md](DEPLOYMENT.md) |

### Canonical symbol index

Search here before writing a new class, helper, or service — this repository has been
consolidated repeatedly and the implementation usually already exists.

| Concern | Symbol | Lives in |
|---|---|---|
| Canonical signal types | `Signal`, `SignalAction` | `libs/python/lib_strategy/lib_strategy/signals/signal.py` |
| Signal helpers | `ensure_utc`, `compute_stable_signal_id`, `compute_execution_dedup_key` | `libs/python/lib_strategy/lib_strategy/signals/utils.py` |
| Action normalization | `normalize_signal_action`, `normalize_scoring_action` | `libs/python/lib_strategy/lib_strategy/signals/normalization.py` |
| Strategy base class | `PureSignalStrategy` | `libs/python/lib_strategy/lib_strategy/signals/pure_strategy.py` |
| Scoring view adapter | `build_scoring_view()` | `libs/python/lib_strategy/lib_strategy/signals/adapters/scoring.py` |
| Binding thresholds | `evaluate_binding_thresholds()` | `libs/python/lib_strategy/lib_strategy/scoring/binding_evaluator.py` |
| Option spreads | `build_spread()` | `libs/python/lib_strategy/lib_strategy/spreads/option_spreads.py` |
| Asset-class taxonomy | `CANONICAL_ASSET_CLASSES`, `TRADABLE_ASSET_CLASSES`, `SESSION_BASED_ASSET_CLASSES` | `libs/python/lib_common/lib_common/asset_classes.py` |
| Transactional outbox | `OutboxStore` | `libs/python/lib_application/lib_application/outbox.py` |
| Event contracts | `PlatformEvent` and subclasses | `libs/python/lib_common/lib_common/internal_events.py` |
| DB sessions | `get_session_factory()`, `dispose_engine()` | `libs/python/lib_application/lib_application/db/session.py` |
| ORM models | all application tables | `libs/python/lib_application/lib_application/db/models/` |
| Bar consolidation | `BarConsolidator` | `libs/python/lib_data/lib_data/consolidation.py` |
| Logging | `get_logger()` | `libs/python/lib_common/lib_common/logging.py` |
| Env parsing | `parse_bool_env`, `parse_int_env`, `parse_float_env`, `parse_list_env` | `libs/python/lib_common/lib_common/env_utils.py` |
| HTTP errors | `APIError`, `raise_for_status`, `is_retryable_status` | `libs/python/lib_common/lib_common/http_exceptions.py` |
| Retries | `retry_async`, `retry_sync` | `libs/python/lib_common/lib_common/retries.py` |
| Config loading | `ConfigManager` | `libs/python/lib_common/lib_common/app/config.py` |
| Service/worker bases | `create_service_app`, `ApplicationManager`, `HealthCheckResult` | `libs/python/lib_common/lib_common/app/` |
| Scoring engine | `ScoreEngine` | `apps/scoring_engine/scoring_engine/engine.py` |
| Outbox relay | `OutboxRelayWorker` | `apps/scoring_engine/scoring_engine/outbox_relay.py` |
| Execution engine | `ExecutionEngine`, `OrderBuilder`, `OptionsOrderBuilder` | `apps/execution_engine/execution_engine/` |
| Feedback loop | `FeedbackLoopEngine`, `SignalEvaluator`, `ParameterOptimizer` | `apps/feedback_loop_engine/feedback_loop_engine/` |
| Strategy runtime | `SignalWorker`, `StrategyRuntimeStore`, `DurableSignalRelay` | `apps/indicator_runner/indicator_runner/` |

**Repository ports:** `IExecutionRepository` → `SQLAlchemyExecutionRepository`;
`ISignalPerformanceRepository` → `SQLAlchemySignalPerformanceRepository`.

There is **no central strategy registry**. New strategies subclass `PureSignalStrategy` and
are wired into the `SignalWorker` through the strategy's `config.json`. The legacy
`BaseStrategy` chain, `StrategyRegistry`, `UserBindingsService`, `ModeSelector`, and
`ModeOptimizer` were all removed; do not go looking for them.

### Internal event contracts

All inter-service events are strict Pydantic models in
`libs/python/lib_common/lib_common/internal_events.py`.

| Event class | Topic | Emitted when |
|---|---|---|
| `CanonicalSignalEvent` | `signals.ingested` | after signal persistence |
| `ScoredSignalEvent` | `signals.scored` | after score computation |
| `ExecutionCommandEvent` | `execution.commands` | queued for execution (via outbox) |
| `ExecutionResultEvent` | `execution.results` | execution outcome |
| `FeedbackEvaluationEvent` | `feedback.ready` | signal ready for performance evaluation |

Every event carries `run_id`, `correlation_id`, and `causation_id` for end-to-end tracing.

### Cross-layer impact matrix

A contract change has to account for its real producers and consumers:

| If you change… | Also check… |
|---|---|
| Signal fields | normalization, scoring adapter, DB schema, event contracts, execution |
| `SignalAction` enum | normalization, DB constraints, event contracts, execution routing |
| Scoring algorithm | user-binding evaluation, outbox events, execution decisions |
| User-binding thresholds | execution frequency, position sizing |
| Execution logic | metrics collection, feedback loop, `ExecutionResultEvent` |
| Feedback metrics | mode selection, binding optimization |
| Event contracts | all downstream consumers (outbox relay, execution engine) |
| Outbox/relay | scoring-engine storage, execution-engine API |

### Documentation Map

The canonical index of every repository document lives in
[README.md § Documentation](../README.md#documentation). It is not repeated here.

**Note**: This manual consolidates the previous developer, architecture, and strategy guides.

---

## 4. Root Configuration Files

These files configure the entire repository behavior.

### Python & Linting

| File | Purpose | Key Settings |
|------|---------|--------------|
| [pyproject.toml](../pyproject.toml) | Python project metadata | Python 3.11, pytest, mypy, and Ruff lint/format |
| [.pre-commit-config.yaml](../.pre-commit-config.yaml) | Git hooks | Enforces: mypy, Ruff lint/format/import ordering, forbid-direct-orders, vmdev audit |

### Build & Environment

| File | Purpose | Key Settings |
|------|---------|--------------|
| [Makefile](../Makefile) | Build shortcuts | `make setup`, `make build`, `make test` |
| [.env.example](../.env.example) | Environment template | DATABASE_URL, API keys, broker credentials |

### Documentation

| File | Purpose | Audience |
|------|---------|----------|
| [README.md](../README.md) | Quick overview | Everyone |
| [SETUP_MAC_LINUX.md](../SETUP_MAC_LINUX.md) | Developer onboarding (macOS/Linux) | New developers |
| [SETUP_WINDOWS.md](../SETUP_WINDOWS.md) | Developer onboarding (Windows) | New developers |
| [CLAUDE.md](../CLAUDE.md) | Production guidelines | AI assistants, developers |

---

## 5. Libraries (libs/)

Libraries are the foundation - shared code distributed as Python wheels. **Build order matters** due to dependencies.

### Dependency Hierarchy

```
Level 0: lib_common (no dependencies)
           │
Level 1: ──┼── lib_data
           │      │
Level 2: ──┼── lib_indicators
           │      │
Level 3: ──┴── lib_strategy ◀── (core abstraction)
                   │
Level 4: ───── lib_application
                   │
Level 5: ───── lib_infrastructure
```

### 5.1 lib_common (Core Utilities)

> **Path**: [libs/python/lib_common/](../libs/python/lib_common/)
> **Owner**: Platform Team
> **Dependencies**: None

The foundation library providing logging, configuration, and application lifecycle.

| File | Purpose |
|------|---------|
| `lib_common/__init__.py` | Package exports |
| `lib_common/logging.py` | Structured logging (JSON format) |
| `lib_common/exceptions.py` | Custom exception hierarchy |
| `lib_common/env_utils.py` | Strict environment parsing helpers |

#### Application Framework (lib_common/app/)

| File | Purpose |
|------|---------|
| `app/__init__.py` | ApplicationManager export |
| `app/lifecycle.py` | **ApplicationManager** - base class for all apps |
| `app/config.py` | Environment-aware config loading |
| `app/health.py` | Health check server (/health, /status, /live, /ready) |
| `app/secrets.py` | Canonical environment-backed application secret loading |
| `app/protocols.py` | Type protocols for dependency injection |

#### Production Utilities

| File | Purpose |
|------|---------|
| `lib_common/config_validation.py` | **Pydantic config models** - validates configs at startup |
| `lib_common/shutdown.py` | **GracefulShutdown** - SIGTERM/SIGINT handlers, operation tracking |

**Usage Example**:

```python
from lib_common.app import ApplicationManager

class MyRunner(ApplicationManager):
    def initialize(self) -> None:
        # Called after config/secrets loaded
        pass

    def run(self) -> None:
        # Main execution loop
        while not self.shutdown_requested:
            self.process_signals()
```

---

### 5.2 lib_data (Data Fetching)

> **Path**: [libs/python/lib_data/](../libs/python/lib_data/)
> **Owner**: Platform Team
> **Dependencies**: lib_common

Provides provider-neutral bar, dataset, consolidation, notification, and
watermark primitives. Venue API clients live in `lib_infrastructure.market_data`.

| File | Purpose |
|------|---------|
| `lib_data/__init__.py` | Package exports |
| `lib_data/bars.py` | Canonical bar representation |
| `lib_data/consolidation.py` | Deterministic bar consolidation |
| `lib_data/dataset.py` | Dataset validation and loading |
| `lib_data/market_data.py` | Market-data interfaces and value objects |
| `lib_data/pg_notify.py` | PostgreSQL market-data notification listener |
| `lib_data/watermark.py` | Durable processing watermarks |

---

### 5.3 lib_indicators (Technical Indicators)

> **Path**: [libs/python/lib_indicators/](../libs/python/lib_indicators/)
> **Owner**: Quant Team
> **Dependencies**: lib_common

Streaming indicators used by production strategies or retained parity gates.

| File | Purpose | Formula/Description |
|------|---------|---------------------|
| `lib_indicators/atr.py` | Average True Range | Wilder ATR |
| `lib_indicators/ema.py` | Exponential moving average | Streaming exponential mean |
| `lib_indicators/pmo.py` | Price Momentum Oscillator | Double-smoothed ROC |
| `lib_indicators/sma.py` | Simple moving average | Rolling arithmetic mean |
| `lib_indicators/swing_high_low.py` | Swing detection | Local maxima/minima |
| `lib_indicators/vortex.py` | Vortex indicator | Backtrader-compatible VI+/VI− parity |

---

### 5.4 lib_strategy (Strategy Framework)

> **Path**: [libs/python/lib_strategy/](../libs/python/lib_strategy/)
> **Owner**: Platform Team
> **Dependencies**: lib_common, lib_data
> **Version**: 0.2.0 (Clean Architecture)

The core strategy abstraction layer - all strategies inherit from classes defined here.

#### Core Classes

| File | Class | Purpose |
|------|-------|---------|
| `lib_strategy/signals/pure_strategy.py` | **PureSignalStrategy** | Framework-agnostic base for all strategies (`initialize()`, `on_data()`, `emit_long/short/close()`) |

#### Signal Handling

| File | Purpose |
|------|---------|
| `lib_strategy/signals/__init__.py` | Signal types and routing |
| `lib_strategy/signals/signal.py` | **Signal** dataclass |
| `lib_strategy/signals/adapters/scoring.py` | Canonical `Signal` to `ScoringSignalView` boundary |

#### Clean Architecture (Domain-Driven Design)

| File | Purpose |
|------|---------|
| `lib_strategy/domain/entities.py` | ExecutionLog domain entity |
| `lib_strategy/ports/execution_port.py` | Execution interface |
| `lib_strategy/ports/signal_performance_port.py` | Signal-performance persistence interface |

#### Option Spreads

| File | Purpose |
|------|---------|
| `lib_strategy/spreads/option_spreads.py` | **build_spread()** — canonical Black-Scholes multi-leg builder; requires the selected broker contract's observed `contract_multiplier` |

**Signal Dataclass**:

```python
@dataclass
class Signal:
    signal_id: str
    strategy_id: str
    strategy_type: str  # indicator
    symbol: str
    action: SignalAction  # LONG, SHORT, CLOSE, HOLD (from lib_strategy.signals.signal)
    confidence: float     # 0.0 to 1.0
    timestamp: datetime
    instrument_id: Optional[int | str]
    sector_id: Optional[int]
    expected_return: Optional[float]
    predicted_risk: Optional[float]
    horizon_days: Optional[float]
    entry_price: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    raw_score: Optional[float]
    expires_at: Optional[datetime]
    features: Dict[str, Any]
    metadata: Dict[str, Any]
```

Note: `apps/scoring_engine` derives a `ScoringSignalView` from canonical `Signal` objects
for the first stage of the three-stage scoring pipeline (adds direction and derives
the required μ/σ/horizon values).

---

### 5.5 lib_application (Application Logic)

> **Path**: [libs/python/lib_application/](../libs/python/lib_application/)
> **Owner**: Platform Team
> **Dependencies**: lib_strategy

Application-layer logic including database models and use cases.

#### Database Models

| File | Purpose |
|------|---------|
| `lib_application/db/__init__.py` | Database exports |
| `lib_application/db/models/` | App-owned SQLAlchemy schema, kept in zero drift with Alembic |
| `lib_application/db/session.py` | Session factory |

**Key Model Groups**:

- **Users**: `orgs`, `users`, `user_roles`, `user_plan_subscriptions`, `user_consents`
- **Strategies**: `strategies`, `strategy_versions`, `user_strategy_configs`
- **Signals**: `canonical_signals`, `signal_performance`
- **Scoring**: `asset_scores`, `sector_scores`, `market_scores`, `user_strategy_bindings`, `mode_performance`
- **Execution**: `order_intents`, `orders`, `executions`, `pending_orders`, `execution_logs`, `positions`, `daily_nav`
- **Brokers**: `brokers`, `broker_environments`, `linked_broker_accounts`, `broker_credentials`, `managed_secrets`
- **Market sessions**: `market_calendars`, `market_sessions`, plus each
  instrument's explicit continuous/scheduled policy. Scheduled exposure
  requires fresh complete official coverage; no static holiday schedule is
  inferred.

#### Services

| File | Purpose |
|------|---------|
| `services/instrument_resolution.py` | Canonical symbol/instrument resolution |
| `services/market_calendars.py` | Atomic official-session coverage replacement and exact instrument assignment |
| `services/price_ingestion_service.py` | Validated OHLCV persistence |
| `services/strategy_feedback.py` | Feedback persistence port implementation |

User binding writes are owned by `apps/backend/backend/api.py`; scoring reads and
evaluates the canonical `user_strategy_bindings` rows.

#### Use Cases (Clean Architecture)

Legacy config use cases were removed. Use the dedicated apps instead:

- Signal processing: `apps/scoring_engine/`
- Order execution: `apps/execution_engine/`

---

### 5.6 lib_infrastructure (Broker Adapters)

> **Path**: [libs/python/lib_infrastructure/](../libs/python/lib_infrastructure/)
> **Owner**: Platform Team
> **Dependencies**: lib_strategy

Infrastructure layer with broker adapters and persistence.

#### Broker Adapters

| File | Broker | Asset Classes |
|------|--------|---------------|
| `brokers/adapters/coinbase.py` | Coinbase Advanced Trade | Crypto spot |
| `brokers/adapters/deribit.py` | Deribit | Crypto derivatives |
| `brokers/adapters/delta.py` | Delta Exchange | Crypto derivatives |
| `brokers/adapters/ibkr.py` | Interactive Brokers | Equities, options |
| `brokers/adapters/saxo.py` | Saxo OpenAPI | Equities, FX, futures, single-leg options |
| `brokers/adapters/zerodha.py` | Zerodha | Indian equities |

#### Broker Framework

| File | Purpose |
|------|---------|
| `brokers/base.py` | **BaseBroker** abstract class |
| `brokers/factory.py` | Broker factory (creates adapters) |
| `brokers/capabilities.py` | Broker capability matrix |
| `brokers/ports.py` | Provider-neutral broker contracts |
| `brokers/secrets.py` | Broker credential reference resolution |

#### Persistence (Repository Pattern)

| File | Purpose |
|------|---------|
| `persistence/sqlalchemy/repositories/execution_repo.py` | Execution data access |
| `persistence/sqlalchemy/repositories/signal_performance_repo.py` | Feedback performance data access |

---

## 6. Trading Strategies (strategies/)

### 6.1 Indicator Strategies (SignalWorker)

> **Path**: [strategies/indicator/](../strategies/indicator/)
> **Runtime**: SignalWorker (DB-fed `PureSignalStrategy` cores)
> **Mode**: Signal-only (no direct execution)

Selected indicator strategies run as processes inside the single
`indicator-runner` image (`vynmatrix/indicator-runner`), governed jointly
by `STRATEGY_LIST`, `enabled`, and `environments`. Ordinary bar-driven
strategies are fed from the `prices` table:
`market_data_ingestor` polls the explicitly configured venue/canonical mapping
→ `prices` table with immutable source provenance → Postgres
`NOTIFY new_market_data` → `SignalWorker` (LISTEN) → `BarConsolidator` →
`core.on_data()` → `BufferedSignalEmitter` → atomic runtime journal and
`signals.submit` outbox commit → `DurableSignalRelay` → `scoring_engine`
`/api/v1/signals`.

Historical inserts and provider corrections are not injected into a live core.
The price mutation and rebuild generation commit atomically; the supervisor
replaces the process, replays the complete subscribed universe in deterministic
order with all emissions suppressed, and resumes live bars only after the
generation-fenced replay is acknowledged.

The disabled `USQualityCompounder` portfolio core is the deliberate
exception: it consumes one revision-pinned synchronized point-in-time factor
panel, not independent ticker bars. Its panel runtime still emits canonical
signals and rebalance events through the same scoring, outbox, execution, and
feedback services.

#### Strategy List

This is a runtime inventory summary. The authoritative decisions, readiness
states, blockers, and promotion gates are maintained in
[STRATEGY_READINESS.md](STRATEGY_READINESS.md).

| Strategy | Description | Key Indicators |
|----------|-------------|----------------|
| [SwingHighLowPMO](../strategies/indicator/SwingHighLowPMO/) | Development benchmark/E2E fixture | SwingHighLow, PMO |
| [USQualityCompounder](../strategies/indicator/USQualityCompounder/) | Disabled, dev-only synchronized S&P 500 portfolio core; diagnostic validation only | Quality, fundamental growth, valuation, momentum, breadth/trend/volatility gates |

#### Strategy Structure

Each strategy folder contains:

```
StrategyName/
├── core.py              # PureSignalStrategy subclass (signal-only)
└── config.json          # runner_kind: signal_worker + market_data config
```

Contract and lifecycle tests may live in the repository-level `tests/` suite or
the owning application suite; they are not required to be nested in the strategy
directory. Strategy-specific operational documentation belongs in the existing
canonical topic document rather than a duplicate per-directory README.

The `config.json` selects the SignalWorker runtime and its market-data feed:

```json
{
    "runner_kind": "signal_worker",
    "market_data": {
        "source": "coinbase_live",
        "timeframe": "1m",
        "consolidation_minutes": 15,
        "bootstrap_bars": 500
    }
}
```

#### Signal-Only Pattern

All indicator strategies follow this pattern (enforced by the `vmdev audit`
`indicator-signal-only` gate — emit canonical signals, never place broker orders):

```python
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy


class MyStrategyCore(PureSignalStrategy):
    @property
    def warmup_bars_needed(self) -> int:
        return 50

    def initialize(self) -> None:
        self._position = 0  # 0 = flat, 1 = long, -1 = short

    def on_data(self, state: MarketState) -> None:
        # Emit canonical signals; never place broker orders directly.
        if self.should_buy(state):
            self.emit_long(symbol=state.symbol, entry_price=state.close, timestamp=state.timestamp)
            self._position = 1
        elif self.should_close(state):
            self.emit_close(symbol=state.symbol, exit_price=state.close, timestamp=state.timestamp)
            self._position = 0
```

---

### 6.3 Adding or removing a strategy — keep every surface in lockstep

Indicator source is copied into the single `indicator-runner` image, so **being present on
disk does not authorize execution**: the runtime `STRATEGY_LIST` is explicit and fail-closed,
and the database strategy/version/binding gates stay authoritative. When a strategy is added
or removed, update these in the same change:

- [ ] **Strategy code** — `strategies/indicator/<Name>/`: `core.py` (a `PureSignalStrategy`
      subclass, plus any helper modules), `config.json` with `runner_kind: signal_worker`,
      and `tests/`. There is no `main.py`.
- [ ] **Container manifest** — `config/containers.yaml`, only if image inputs change;
      align the explicit local `STRATEGY_LIST` with the reviewed strategy config.
- [ ] **Build deps** — `config/build.yaml`, only if the strategy adds dependencies.
- [ ] **CI build** — `.github/workflows/build-and-push.yml` validates images;
      publication is separately manual and opt-in. No deployment is included.
- [ ] **Local stack** — `docker/docker-compose.stack.yml` defaults `STRATEGY_LIST` to empty
      and fail-closed; it is injected at runtime, never baked into the image.
- [ ] **Test discovery** — `pyproject.toml` `[tool.pytest.ini_options] testpaths`, if the
      strategy ships a standalone `tests/` directory.
- [ ] **Docs and tooling** — `scripts/README.md`, any strategy-specific scripts, and the
      indicator inventory referenced elsewhere in this manual.
- [ ] **Changelog** — an entry under `## [Unreleased]` in `CHANGELOG.md`.

---

## 7. Applications (apps/)

Microservices run locally through the declared Docker Compose stack. No cloud
runtime is configured or implied by this migration. See
[docs/DEPLOYMENT.md](DEPLOYMENT.md) for image ownership and the release boundary.

### 7.1 Strategy Runners

#### indicator_runner

> **Path**: [apps/indicator_runner/](../apps/indicator_runner/)
> **Purpose**: Run indicator strategies via the SignalWorker (DB-fed `core.py`)

| File | Purpose |
|------|---------|
| `indicator_runner/main.py` | ApplicationManager entry point |
| `indicator_runner/signal_worker.py` | **SignalWorker** — bootstrap → LISTEN → consolidate → feed `core.on_data()` |
| `indicator_runner/process_manager.py` | Manage per-strategy SignalWorker processes |
| `indicator_runner/runtime_journal.py` | Versioned state/decision journal, buffered emitter, progress status, and durable signal relay |
| `indicator_runner/panel_runtime.py` | Registered synchronized-panel admission, atomic panel decision/rank/state/outbox completion, correction and replay fencing |
| `indicator_runner/model_rebalance_projection.py` | Lossless completed-panel audit to model-rebalance event projection |

Shared runner utilities: `lib_common/runner_utils.py` (strategy selection, config validation, run mode resolution)

**Smoke Run Checklist (Health/Status)**:

- `curl -sS http://<service-host>:<port>/health` → `{"status":"healthy"}` (or `degraded` if some processes crashed)
- `curl -sS http://<service-host>:<port>/status` → JSON with `running`, `crashed`, `disabled`, and per-strategy entries

---

### 7.2 Core Engines

#### scoring_engine

> **Path**: [apps/scoring_engine/](../apps/scoring_engine/)
> **Purpose**: Three-stage signal scoring (scoring view → meta-label → ensemble)

##### Core Files

| File | Purpose |
|------|---------|
| `scoring_engine/main.py` | FastAPI application entry |
| `scoring_engine/api.py` | Signal ingestion, score reads, and admin-only diagnostics |
| `scoring_engine/engine.py` | **ScoreEngine** - orchestrates scoring with pipeline |
| `scoring_engine/pipeline.py` | **ScoringPipeline** - three-stage scoring orchestrator |
| `scoring_engine/storage.py` | Score persistence |
| `scoring_engine/models.py` | Pydantic data models |
| `scoring_engine/alias_provider.py` | Symbol alias resolution |
| `scoring_engine/rebalance_store.py` | Completed panel/rank lineage verification and atomic model/account plan persistence |
| `scoring_engine/services/rebalance_service.py` | Paper-forward batch binding, authority, and account-plan evaluation |

##### Execution Context Providers

- `scoring_engine/providers_db.py` emits canonical profile fields used by the execution engine (broker/spot/options selections, allowed_brokers, per-broker account metadata).
- `scoring_engine/engine.py` evaluates each active binding and resolves its
  permitted execution mode from the binding policy and account-scoped mode
  performance.

##### Scoring Stages

| File | Stage | Models |
|------|-------|--------|
| `lib_strategy/signals/adapters/scoring.py` | Input | **ScoringSignalView** (direction, μ, σ, horizon) |
| `domain/layer2_meta_label.py` | Meta-label | **MetaLabelOutput**, MarketContext |
| `domain/layer3_global_score.py` | Ensemble | **GlobalScore**, EnsembleConfig |

##### Scoring Services

| File | Stage | Purpose |
|------|-------|---------|
| `services/meta_label_service.py` | Meta-label | Build the scoring view; apply observed context, age decay, probability tilt, and local scoring |
| `services/ensemble_service.py` | Ensemble | Alpha aggregation, logit stacking, and score gating |

**Key Endpoints**:

- Threshold checks use absolute score values so long/short signals are symmetric.
- `POST /api/v1/signals` - Provider-neutral insight payload submitted by `DurableSignalRelay` through `HttpSignalEmitter`
- `POST /api/v1/model-rebalances` - Internal synchronized batch input; accepts
  only a completed, lineage-valid `paper_forward` model rebalance and creates
  frozen account plans transactionally
- `GET /scores` - Get current score for asset
- `GET /bindings` - Admin-only binding diagnostics; configuration writes use `apps/backend`
- `GET /instruments/{symbol}` - Admin-only instrument diagnostics; the source-controlled catalogue owns writes
- `GET /health` - Health check

**Three-Stage Scoring Pipeline**:

```text
Stage 1: Signal → ScoringSignalView
    │  direction, expected_return (μ), predicted_risk (σ), horizon_days
    ▼
Stage 2: MetaLabelService → MetaLabelOutput
    │  • Age decay: w_age = max(0, 1 - age/H)
    │  • Sharpe raw: S_raw = d × (μ/σ)
    │  • Probability tilt: tilt = 2p - 1
    │  • Local score: score_local = S_raw × tilt × w_age
    ▼
Stage 3: EnsembleService → GlobalScore
    │  • Core alpha: α_core = Σ w_i × score_local_i
    │  • News adjustment: α_raw = α_core + β_news × N_t
    │  • Standardized Score: Score = (α_raw - m_α) / s_α
    │  • Logit stacking: P_agg = sigmoid(Σ w̃_i × logit(p_i))
    └  • Gating: |Score| ≥ τ_min AND P_agg ≥ p_min
```

The scoring pipeline ends at `GlobalScore`. `ScoreEngine.evaluate_bindings()`
owns per-user threshold checks and mode selection. Executable decisions then
cross the outbox boundary; `OrderBuilder` and `PositionSizer` in the execution
engine own concrete instrument construction, account-aware sizing, and risk
limits. There is no scoring-layer portfolio optimizer or instrument router.

**Mathematical Formulas**:

```python
# Stage 2: Age-weighted local score
w_age = max(0, 1 - age_seconds / (horizon_days * 86400))
sharpe_raw = direction * (expected_return / predicted_risk)
probability_tilt = 2 * meta_probability - 1
score_local = sharpe_raw * probability_tilt * w_age

# Stage 3: Global ensemble
alpha_core = sum(weight_i * score_local_i for each strategy)
alpha_raw = alpha_core + beta_news * news_sentiment
score = (alpha_raw - rolling_mean) / rolling_std  # Standardized

# Logit stacking for probability aggregation
z = sum(weight_i * logit(p_i)) + gamma_news * news_sentiment
p_agg = 1 / (1 + exp(-z))  # Sigmoid

# Gating rule
passed_gate = abs(score) >= tau_min and p_agg >= p_min
```

#### execution_engine

> **Path**: [apps/execution_engine/](../apps/execution_engine/)
> **Purpose**: Execute orders through broker adapters

| File | Purpose |
|------|---------|
| `execution_engine/main.py` | FastAPI application entry with graceful shutdown |
| `execution_engine/api.py` | Canonical outbox-consumer endpoint (`/execute-command`) |
| `execution_engine/engine.py` | Order routing logic with mode enforcement |
| `execution_engine/broker_bridge.py` | Bridge to `lib_infrastructure` broker adapters |
| `execution_engine/order_builder.py` | Build orders from signals |
| `execution_engine/options_builder.py` | Option spread orders |
| `execution_engine/position_sizer.py` | Kelly criterion sizing |
| `execution_engine/deduplication.py` | **ExecutionDeduplicator** - signal idempotency |
| `execution_engine/risk_guard.py` | Pre-trade risk gating and short blocking |
| `execution_engine/reconciliation.py` | Durable account/order/position reconciliation, including client-ID resolution of ambiguous submissions |
| `execution_engine/reconciliation_tracker.py` | Startup partition discovery and initial-reconciliation readiness |
| `execution_engine/pending_orders.py` | Recoverable broker/local-paper order state |
| `execution_engine/paper_order_lifecycle.py` | Fenced committed-candle progression for durable stop/limit/OCO paper orders |
| `execution_engine/brokers/paper.py` | Immediate local-paper broker boundary; resting economics live in the durable lifecycle |
| `execution_engine/rebalance_store.py` | Durable plan leases, generation fences, phased leg status, audit, and restart recovery |
| `execution_engine/rebalance_orchestrator.py` | Exit/reduction-first paper portfolio execution and dependency blocking |
| `execution_engine/rebalance_execution_adapter.py` | Frozen plan leg to canonical target-order boundary |

##### Performance Metrics Services

| File | Purpose |
|------|---------|
| `metrics/__init__.py` | Metrics package exports |
| `metrics/pnl_service.py` | **PnLService** - FIFO P&L computation |
| `metrics/strategy_metrics.py` | **StrategyMetricsService** - win rate, Sharpe, profit factor |
| `metrics/drawdown_service.py` | **DrawdownService** - max drawdown, duration tracking |

**Key Endpoints**:

- `POST /execute-command` - Consume a canonical, account-scoped execution command
- `POST /execute-rebalance-command` - Consume a frozen paper-forward account
  rebalance plan; never accepts historical-validation or live-forward scope
- `GET /admin/rebalance-readiness` - Inspect stale and unresolved failed account plans
- `GET /admin/rebalance-plans/{account_plan_id}` - Inspect full plan, order/fill, transition, and failure-resolution lineage
- `POST /admin/rebalance-plans/{account_plan_id}/resolve-failure` - With service/admin keys and `X-Admin-User`, append an idempotent operator disposition for a terminal failed plan without mutating it
- `GET /health` - Health check
- `GET /ready` - Database, current reconciliation, paper rehydration, unknown-submission, and paper-order progress gate
- `GET /metrics` - Authenticated pool, order-lifecycle, reconciliation, and execution telemetry

**Mode Enforcement**:

The execution engine enforces strict mode separation via environment variables:
- `EXECUTION_MODE`: `backtest`, `paper`, or `live` (default: `paper`)
- `EXECUTION_ENGINE_ALLOW_LIVE`: Must be `true` for live mode
- `EXECUTION_PAPER_ORDER_MAX_LAG_SECONDS`: Maximum committed market-time lag
  for eligible durable local-paper orders (default: `300`)
- `EXECUTION_RECONCILIATION_INTERVAL_SEC`: Background broker reconciliation interval (`300` paper, `60` live default)
- `EXECUTION_CIRCUIT_BREAKER_THRESHOLD`: Reject/reconciliation failures before breaker opens (default: `3`)
- `EXECUTION_CIRCUIT_BREAKER_WINDOW_SEC`: Failure window in seconds (default: `300`)
- `EXECUTION_CIRCUIT_BREAKER_COOLDOWN_SEC`: Breaker cooldown in seconds (default: `900`)
- `EXECUTION_SANDBOX_CERTIFICATION_MARKER`: JSON marker file that must exist and contain `status="passed"` before live mode is allowed
- `EXECUTION_ALERTS_ENABLED` plus `ALERT_WEBHOOK_URL`, `ALERT_TELEGRAM_*`, or
  `ALERT_EMAIL_*`: breaker and reconciliation alert delivery
- `FX_RATE_CURRENCIES`, `FX_RATE_HISTORY_DAYS`, `FX_RATE_POLL_INTERVAL_SEC`: Independent observed-FX ingestion from ECB and Coinbase USDC-EUR
- `EXECUTION_FX_MAX_AGE_SECONDS`: Maximum age of an eligible point-in-time conversion observation; stale or missing legs block cross-currency accounting

```python
# Mode validation per signal (returns failed result, does not raise)
if mode == "live" and not allow_live:
    return ExecutionResult(
        success=False,
        execution_mode="blocked",
        error_message="Live mode requested but EXECUTION_ENGINE_ALLOW_LIVE is false"
    )
```

**SwingHighLowPMO evidence canary**:

SwingHighLowPMO `1.0.1` on BTC-USDC is the first approved canary route, but the
checked-in strategy is still development-only and `READY_FOR_BACKTEST`.
Historical replay helpers are diagnostics and cannot satisfy service-transport
promotion. Use only the declared Compose topology, real Coinbase one-minute
history, the durable signal and execution workers, one dedicated local-paper
account, and the evidence contract in
[E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md). The retired scalper
witnesses were deleted (2026-07-27); the PostgreSQL pipeline gate now runs on
the test-owned deterministic exerciser under `tests/fixtures/strategies/`.

For Coinbase deployment readiness, use the environments this way:
- `sandbox smoke`: Advanced Trade sandbox (`api-sandbox.coinbase.com`) for auth/request-shape checks only
- `paper soak`: local paper broker plus live Coinbase market data and reconciliation
- `live`: Coinbase Advanced Trade production endpoints after the certification marker is present

The certification marker is written with:

```bash
python scripts/write_sandbox_certification_marker.py \
  --commit "$(git rev-parse HEAD)" \
  --symbols BTC-USDC,ETH-USDC,SOL-USDC \
  --paper-window-days 14 \
  --duplicate-submission-count 0 \
  --operator your.name \
  --acceptance-report .artifacts/coinbase/soak-acceptance.json \
  --sandbox-smoke-evidence .artifacts/coinbase/sandbox-smoke.json \
  --paper-soak-evidence .artifacts/coinbase/paper-soak-summary.json \
  --reconciliation-summary .artifacts/coinbase/reconciliation-summary.json
```

Recommended rollout sequence after certification:
- run a live smoke on `BTC-USDC` only
- add `ETH-USDC` after a manual checkpoint on fills, reconciliation, and NAV/P&L
- add `SOL-USDC` only after the second checkpoint is clean

#### feedback_loop_engine

> **Path**: [apps/feedback_loop_engine/](../apps/feedback_loop_engine/)
> **Purpose**: Capture outcomes and produce reviewable optimization suggestions

| File | Purpose |
|------|---------|
| `feedback_loop_engine/main.py` | FastAPI application entry |
| `feedback_loop_engine/evaluator.py` | **SignalEvaluator** - compare prediction vs outcome |
| `feedback_loop_engine/price_provider.py` | Fetch realized prices |
| `feedback_loop_engine/optimizer.py` | Generate bounded changes from exact version defaults |
| `feedback_loop_engine/suggestion_review.py` | List, approve, and reject persisted suggestions |
| `feedback_loop_engine/models.py` | Evaluation data models |

**Evaluation Flow**:

1. Fetch a canonical signal only for its declared evaluation horizon.
2. Read the exact completed price observation at that horizon.
3. Insert `signal_performance` with native conflict handling on
   `(signal_id, evaluation_horizon)`; replay does not advance the wrong-signal
   tracker twice.
4. Join canonical signal → execution decision/account → order intent → order →
   exact fills for realized mode-performance attribution. A run ID or another
   user's execution is never used as a substitute.
5. Upsert the exact account/strategy/instrument/sector/asset scope using its
   database uniqueness contract.
6. Load the exact active strategy version's persisted `default_params`.
7. Persist a reviewable suggestion only when at least one supported parameter
   changes.

Approval is an audit decision only. The feedback runtime never writes strategy
configuration; accepted changes are promoted through source control, validation,
image build, and deployment. A missing/mismatched version, malformed parameter
snapshot, or no-op adjustment fails closed without creating a feedback row.

Indicator strategies run through the SignalWorker against historical/live
`prices` data. Validate them in paper mode and inspect emitted signals, scores,
execution handoff, P&L, and feedback records in Postgres (see
[Section 6.1](#61-indicator-strategies-signalworker)).

---

## 8. Developer CLI (tools/)

### vmdev Commands

> **Path**: [tools/dev_cli/](../tools/dev_cli/)
> **Entry**: `vmdev` (after `make setup`)

| Command | Purpose | Example |
|---------|---------|---------|
| `vmdev build libs` | Build library wheels | `vmdev build libs --component=lib_common` |
| `vmdev build strategies` | Build strategy wheels | |
| `vmdev build venvs` | Create virtual environments, including strategy validation | `vmdev build venvs --app=indicator_runner` |
| `vmdev build venvs --validation` | Rebuild only the strategy-validation venv | |
| `vmdev build docker --from-config --tag latest` | Build config-declared service images | |
| `vmdev test all` | Run all tests | |
| `vmdev test lib --name=X` | Test specific library | `vmdev test lib --name=lib_strategy` |
| `vmdev test team --team=X` | Test by team | `vmdev test team --team=quant` |
| `vmdev format` | Apply Ruff lint fixes and canonical formatting | |
| `vmdev clean --all` | Clean repository build artifacts; never prune global Docker state | |
| `vmdev clean --docker` | Explicitly remove local `vynmatrix/*` images only | |
| `vmdev run app --name=X` | Run app locally | `vmdev run app --name=scoring_engine` |
| `vmdev db start` | Start PostgreSQL | |
| `vmdev db init` | Run migrations | |
| `vmdev db status` | Show migration status | |
| `vmdev strategy attest-correctness NAME` | Freeze exact reviewed source/package/fixture bytes and typed pre-outcome findings | |
| `vmdev strategy attest NAME` | Pin installed wheels, strategy payloads, interpreter, and image digests | |
| `vmdev strategy measure-data-parity NAME` | Hash-attest registered Coinbase public 1m-to-1d parity windows | |
| `vmdev strategy measure-costs NAME` | Hash-attest current Coinbase fees, spread, and depth | |
| `vmdev strategy validate NAME` | Freeze or resume a registered real-data campaign | |

### Daily Workflow (macOS/Linux + Windows)

Activate `.venv-dev` with `source .venv-dev/bin/activate` on macOS/Linux or
`.\.venv-dev\Scripts\Activate.ps1` in PowerShell. Then run the same CLI commands:

```bash
vmdev test lib --name=lib_common
vmdev format
vmdev audit --strict
vmdev build libs
vmdev build strategies
```

Choose focused tests for the actual changed component. Rebuild affected venvs or
Docker images after source changes. Use the OS setup guide for local Compose
startup, explicit strategy selection, and private environment configuration;
starting one app alone does not establish the complete pipeline.

### Python Environments (Tooling, Runtime, and Docker)

- **Tooling/test venv** (`.venv-dev`): install the CI-matched pinned requirements,
  `vmdev`, pytest, and quality tools using the OS setup guide. `vmdev test` uses
  this interpreter. Keep system Python free of project packages.
- **Project venvs** (`build/venvs/app-*`, `build/venvs/strategy-*`): built by `vmdev build venvs`, contain repo wheels + runtime deps.
- **Validation venv** (`build/venvs/strategy-validation`): the only environment
  authorized to freeze or execute an installed-artifact strategy campaign. It
  contains `lib_common`, `lib_data`, `lib_indicators`, `lib_strategy`,
  `lib_infrastructure`, `lib_application`, the
  `vynmatrix_indicator` strategy wheel, and an installed copy of
  `tools/dev_cli`, all resolved under `docker/constraints.txt`.
- **Docker images**: config-declared runtime service images. The indicator image
  contains the verified indicator wheel, while the fail-closed runtime list
  selects the reviewed processes. Docker refuses stale local wheels, installs
  the shared locked closure once in `svc-base`, and copies each service's
  wheel-only environment from an isolated builder into its runtime stage.

### Reproducible Strategy Validation Environment

Registered campaigns run only from `build/venvs/strategy-validation`; campaign
orchestration and research statistics are not installed in production service
wheels. The strategy must have an owner-reviewed `validation_protocol.json`.
The required order is:

```bash
vmdev build libs
vmdev build strategies --group indicator
vmdev build venvs --validation
vmdev build docker --from-config --tag latest
source build/venvs/strategy-validation/bin/activate
vmdev strategy measure-data-parity STRATEGY
vmdev strategy measure-costs STRATEGY
vmdev strategy attest-correctness STRATEGY --file ID=PATH
vmdev strategy attest STRATEGY \
  --container-image indicator-runner=vynmatrix/indicator-runner:latest
vmdev strategy validate STRATEGY --freeze-only [REGISTERED_EVIDENCE_OPTIONS]
```

Every command is fail-closed. Initial freeze binds exact source/config/protocol,
real provider data, current measured cost scenarios, upstream-selection trials,
installed wheel bytes, interpreter, and image identity into an immutable
manifest. Historical order books are unavailable, so current measured costs are
applied backward only as registered sensitivity scenarios. Resume uses embedded
evidence by manifest digest and cannot replace it.

The reference executor models one directional net position with next-tradable-
bar fills and registered costs; it is not market-making queue or two-sided-quote
evidence. A completed campaign can reject, retain, or require redesign, but it
cannot itself grant paper/live authority. Use each command's `--help` for the
registered evidence options; the strategy protocol is the campaign's single
machine-readable contract.

### CLI Structure

| File | Purpose |
|------|---------|
| `dev_cli/main.py` | Entry point |
| `dev_cli/commands/build.py` | Build commands |
| `dev_cli/commands/test.py` | Test commands |
| `dev_cli/commands/format.py` | Format commands |
| `dev_cli/commands/db.py` | Database commands |
| `dev_cli/core/builder.py` | Build orchestration |
| `dev_cli/core/docker_builder.py` | Docker image building |
| `dev_cli/core/venv_manager.py` | Virtual environment management |

---

## 9. Configuration (config/)

### 9.1 Build Configuration

| File | Purpose |
|------|---------|
| [config/build.yaml](../config/build.yaml) | Component definitions, dependencies, versions, and build-owner grouping |
| [config/containers.yaml](../config/containers.yaml) | Docker container grouping |

### 9.2 Runtime Configuration

| File | Purpose |
|------|---------|
| [config/instruments.yaml](../config/instruments.yaml) | Tradeable instruments |
| [config/deployment/dev.yaml](../config/deployment/dev.yaml) | Development settings |
| [config/deployment/staging.yaml](../config/deployment/staging.yaml) | Staging settings |
| [config/deployment/production.yaml](../config/deployment/production.yaml) | Production settings |

### build.yaml Structure

```yaml
global:
  python_version: "3.11"
  output_dir: "build"

libs:
  build_type: "wheel"
  components:
    - name: "lib_common"
      path: "libs/python/lib_common"
      version: "0.1.0"
      dependencies: []
      owner_team: "platform"

strategies:
  build_type: venv_docker
  base_dependencies:
    - lib_common
    - lib_data
    - lib_strategy
  groups:
    - name: indicator
      path: strategies/indicator
      version: 0.1.0
      dependencies:
        - lib_indicators
      owner_team: quant
      docker: false  # source ships in the indicator-runner service image

apps:
  build_type: venv_docker
  components:
    - name: indicator_runner
      path: apps/indicator_runner
      dependencies:
        - lib_common
        - lib_data
        - lib_strategy
        - lib_application
        - lib_infrastructure
      docker: true
```

---

## 10. Scripts (scripts/)

### Production Scripts

| Script | Purpose | Usage |
|--------|---------|-------|
| [replay_canonical_signals.py](../scripts/replay_canonical_signals.py) | Explicitly replay an exact published decision/command against real `prices` through local-paper execution/accounting | `docker compose --env-file .env -f docker/docker-compose.stack.yml run --rm --no-deps execution-engine python /app/scripts/replay_canonical_signals.py --user-id user_001 --broker-account-id 1 --strategy-id swing_high_low_pmo_v1 --symbols BTC-USDC --timeframe 15m` |
| [write_paper_promotion_manifest.py](../scripts/write_paper_promotion_manifest.py) | Hash one exact single-instrument or synchronized-portfolio config, image, model/instrument scope, account/binding/broker, and durable runtime evidence set consumed by both paper gates | The single-instrument config/scope/broker remain inferred by default. Synchronized portfolios add the pre-start model configuration digest and one reviewed instrument allowlist artifact. Output always denies live authority. |
| [write_sandbox_certification_marker.py](../scripts/write_sandbox_certification_marker.py) | Write the evidence-backed certification marker required before live mode | See the complete, current command in [RUNBOOK.md](RUNBOOK.md#coinbase-paper-soak-certification); `--status passed` alone is invalid. |

### Strategy Validation & Debugging

- Validate strategies by running the `signal_worker` against historical/live `prices` data in
  paper mode, then inspect emitted signals + scores in Postgres (`canonical_signals`,
  `asset_scores`, etc.).
- Trace any signal by stable signal/decision/account identity plus correlation
  IDs (see [Section 14](#14-end-to-end-flow-verification)).
- Research backtests run through the production `vmdev strategy` validation
  workflow and persist their registered evidence in the validation result store.

### Database Scripts

| Script | Purpose |
|--------|---------|
| [scripts/db/alembic/](../scripts/db/alembic/) | Alembic migrations |
| [scripts/db/bootstrap_scoring.py](../scripts/db/bootstrap_scoring.py) | Seed instruments into an Alembic-managed schema |

Notes:
- `vmdev db init` applies migrations only. `vmdev db reset` additionally loads
  the source-controlled instrument catalogue through `bootstrap_scoring.py`; it
  does not create schema objects, users, broker accounts, credentials,
  strategies, or signals. Catalogue loading fails unless Alembic has already
  established the required tables.
- Asset-class policy and filters use the canonical values `crypto`, `equity`,
  `etf`, `index`, `futures`, `options`, `fx`, and `commodities`. Cash-index
  instruments such as `NIFTY50` and `BANKNIFTY` may feed strategies and scoring
  but cannot be submitted to a broker. Select a separately catalogued futures
  or options contract when execution is intended.
- Trusted schedule ingestion uses the admin-authenticated backend
  `PUT /market-calendars/{code}`. Omitting an instrument from a replacement
  detaches it so new exposure fails closed; see
  [Database Reference](DATABASE.md#authoritative-market-sessions).
- The supervised IBKR, Saxo-live, and Zerodha/NSE writers are opt-in Compose
  profiles (`calendar-ibkr`, `calendar-saxo`, `calendar-zerodha`). Their selector
  variables contain canonical symbols; exact venue identity is loaded from the
  broker catalogue. Enable only one writer per instrument.

### Cross-Platform Script Parity

All critical shell scripts have PowerShell equivalents:

| Task | macOS/Linux | Windows |
|------|-------------|---------|
| Initial setup | `make setup` | `.\scripts\setup_windows.ps1` |
| Create dev venv | `./scripts/venv/create_dev_venv.sh` | `.\scripts\venv\create_dev_venv.ps1` |
| Activate dev venv | `source .venv-dev/bin/activate` | `.\.venv-dev\Scripts\Activate.ps1` |
| Diagnose env | `./scripts/diagnose_environments.sh` | `.\scripts\diagnose_environments.ps1` |
| DB lifecycle | `./scripts/db/manage_db.sh <cmd>` | `.\scripts\db\manage_db.ps1 <cmd>` |

#### Migration Files

| Migration | Purpose |
|-----------|---------|
| `0002_core_app_schema.py` | Core app schema (orgs, users, strategies, brokers, etc.) |
| `0003_aliases_and_entry_price.py` | Canonical signals, instrument aliases |
| `0005_remaining_tables.py` | Remaining 39 tables (OMS, risk, feedback loop, etc.) |
| `0006_bigints_and_fks.py` | BigInteger for high-volume tables, FK type alignment |
| `0007_add_prices_table.py` | Historical prices table |
| `0008_backtest_experiments.py` | Backtest experiment tracking |
| `0010_execution_metrics.py` | Execution metrics snapshots |
| `0011_backtest_experiment_types.py` | Add stress/Monte Carlo experiment types |
| `0012_exec_metrics_window.py` | Execution metrics time-window + exposure/leverage fields |

---

## 11. Docker Infrastructure (docker/)

### Docker Files

| File | Purpose |
|------|---------|
| [docker/scoring_engine.Dockerfile](../docker/scoring_engine.Dockerfile) | Scoring engine image |
| [docker/execution_engine.Dockerfile](../docker/execution_engine.Dockerfile) | Execution engine image |
| [docker/seed/02_seed_data.sql](../docker/seed/02_seed_data.sql) | Seed strategies, instruments, aliases (applied post-schema by the `db-seed` stack service) |

### Service Images (Config-Driven)

- **Indicator strategies** all run in the single `indicator-runner` image
  (`vynmatrix/indicator-runner`); the deployable set is governed by
  `STRATEGY_LIST`.
- All five service images derive from the constraints-pinned, multi-stage
  `vynmatrix/svc-base`. Service builder stages add only their exact local
  wheels and declared extras, verify the closure with `pip check`, then copy it
  into a clean runtime stage.
- The service inventory remains `config/containers.yaml`. `vmdev` rejects a
  Docker build when a configured wheel is missing or stale relative to source
  or packaging metadata.

**Build flow:**

```bash
vmdev build libs
vmdev build strategies
vmdev build docker --from-config --tag latest
```

---

## 12. Database Schema

- **Canonical doc**: [docs/DATABASE.md](DATABASE.md)
- **Models**: [libs/python/lib_application/lib_application/db/models/](../libs/python/lib_application/lib_application/db/models/)
- **Migrations**: `scripts/db/alembic` (run with `vmdev db init` or `alembic upgrade head`)
- **Local DB**: `vmdev db start` + `vmdev db init`. In the full stack
  (`docker-compose.stack.yml`), `docker/init-db/` runs at Postgres boot for
  **extensions only**. The `db-seed` one-shot waits for `db-migrate` to complete
  successfully, then applies `docker/seed/*.sql`; it does not depend on scoring
  engine health.

Key domains (see DATABASE.md for table lists):
- Tenancy/users, brokers/credentials
- Instruments + aliases
- Strategies/versions/coverage
- Signals & scoring (`canonical_signals`, `asset_scores`, `sector_scores`, `market_scores`)
- Point-in-time equity evidence, synchronized panel decisions/ranks, model
  rebalances, and tenant/account plans
- Execution (orders/executions/positions, `execution_logs`, `execution_metrics`)
- Feedback/backtests (signal performance, parameter feedback, backtest_results)

### 12.1 Table Dictionary (App Schema)

Each table below is defined in `libs/python/lib_application/lib_application/db/models/` and used by the
scoring + execution pipelines.

#### A) Core Tenancy & Users
- `orgs`: Tenants/organizations (B2B or multi-org support).
- `users`: End users (retail customers).
- `user_roles`: User roles for access control.
- `plans`: Commercial plans/entitlements.
- `user_plan_subscriptions`: User plan subscriptions.

#### B) Legal, Consents & Suitability
- `user_consents`: User consents and disclosures.
- `suitability_questionnaires`: Suitability questionnaire definitions.
- `user_suitability_responses`: User suitability assessment responses.

#### C) Broker Connectors & User-Linked Accounts
- `brokers`: Supported brokers and their capabilities.
- `broker_environments`: Broker endpoints by environment/region.
- `linked_broker_accounts`: User's external brokerage account link.
- `broker_credentials`: Broker credential references (not raw keys).

#### D) Instruments & Mapping
- `instruments`: Canonical instrument definitions.
- `instrument_broker_symbols`: Exact broker symbol plus optional typed venue ID/type;
  execution resolves it through the selected linked account and never infers numeric IDs.
  IBKR requires a persisted conid, Zerodha an instrument token, and Saxo a
  UIC/AssetType pair.
- `instrument_aliases`: Aliases for instruments (e.g., BTCUSD -> BTC/USD).
- `prices`: Time-series price data for instruments.

#### E) Strategies, Signals & Scoring (Live Pipeline)
- `strategies`: Strategy definitions.
- `strategy_versions`: Immutable strategy release image, source revision, and
  validated parameter snapshots for the production indicator runtime.
- `strategy_runtime_states`: Latest compatible shared-model snapshot and exact
  processed source watermark for one indicator worker partition.
- `strategy_decisions`: Append-only deterministic per-bar decisions and stable
  signal envelopes; empty envelopes record no-signal bars.
- `strategy_panel_input_revisions`: Immutable provider-authorized input
  registration for one synchronized cross-sectional panel.
- `strategy_panel_decisions`: Atomic prepared/completed panel evaluation,
  portfolio state, rank/audit, signal envelopes, and correction/replay record.
- `equity_rank_snapshots` / `equity_rank_snapshot_rows`: Immutable complete
  ranking plus every member's factor and disposition lineage.
- `model_rebalances` / `model_rebalance_legs`: Tenant-neutral immutable
  portfolio target and ordered exit/target legs.
- `account_rebalance_plans` / `account_rebalance_plan_legs`: Frozen
  user/binding/broker-account paper plan with durable phased execution state,
  one-way terminal targets, and the sealed completion generation/digest.
- `account_execution_generations`: Monotonic user/account execution-writer
  ownership audit used with the crash-released PostgreSQL advisory lock.
- `user_strategy_configs`: User-specific strategy execution configuration.
- `canonical_signals`: **Primary signal table** — normalized indicator signals with run_id correlation, μ/σ/horizon, entry_price.
- `asset_scores`: Per-instrument aggregated scores (weighted average with decay).
- `sector_scores`: Sector-level aggregated scores.
- `market_scores`: Market-level aggregated scores.
- `user_strategy_bindings`: User binding config — thresholds, execution mode, autopilot, strategy scoping, allowed brokers.
- `execution_decision_logs`: **Audit trail** of binding evaluation decisions
  (executed + skipped), with stable idempotency plus exact canonical-signal and
  broker-account lineage.
- `outbox_events`: Transactional cross-service messages with ordered claims,
  transient/permanent failure class, dead-letter state, and audited fenced
  redrive.
- `scoring_rules`: DB-driven scoring configuration (optional; in-memory defaults used when empty).
- `mode_performance`: Account-scoped, realized performance per execution mode
  for best-mode selection. Each sample is net FIFO P&L divided by deployed
  entry capital and is linked through exact entry/exit fills to the evaluated
  opening signal; hypothetical signal price movement is not execution return.

#### F) User Control Plane & Preferences
- `user_trading_policies`: User trading preferences per asset class and horizon.
- `user_budget_buckets`: Budget buckets per asset class and horizon.

#### G) Options Strategy Templates
- `option_strategy_templates`: Predefined option strategy templates.
- `option_strategy_template_legs`: Legs for option strategy templates.
- `user_option_presets`: User-specific option strategy presets.

#### H) OMS & Execution
- `order_intents`: Canonical order requests (pre-broker translation).
- `orders`: Broker orders.
- `child_orders`: Child orders for slicing/iceberg strategies.
- `option_order_legs`: Option legs for multi-leg orders.
- `executions`: Trade executions/fills.
- `pending_orders`: Durable recoverable/submission-unknown/working order state,
  including trigger, purpose, TIF, reduce-only, parent/OCO, cumulative fill,
  source-bar watermark, trigger-policy version, and the exact canonical
  execution awaiting idempotent metrics/position/NAV projection.
- `daily_nav`: Account-native daily NAV snapshots, unique per owned broker
  account and date.
- `execution_logs`: Log of execution outcomes for signals.
- `positions`: Account-scoped broker position snapshots with explicit asset,
  contract, or quote-notional quantity semantics and broker-valued gross
  notional. For local paper accounts this is a rebuildable projection of
  canonical fills, not restart accounting truth. Option legs remain attached
  to canonical order intents and fills; the retired fixed two-leg tracker is
  not a second source of truth.
- `execution_metrics`: Per-execution observability snapshot for user/strategy
  metrics (equity, drawdown + duration, exposure/leverage, performance
  metadata). Delayed paper fills use deterministic execution-derived metric
  identities, and realized-contribution metadata names only the exact current
  exit fill. It is not synthesized or used to seed local-paper cash on restart.

Linear futures and perpetual paper orders retain their exact contract
multiplier, leverage, contract type, and fill-time FX provenance on the
canonical order/fill ledger. Restart recovery therefore rebuilds contract
quantity, variation P&L, cash, equity, and gross-notional/leverage margin with
the same account-currency economics as the original fill. Missing or
inconsistent terms and non-linear (inverse or quanto) contracts fail closed.
The generic paper model does not synthesize exchange-specific funding charges
or liquidation prices.

#### I) Risk, Mandates, Audit & Notifications
- `risk_mandates`: Global or relationally user-owned risk policy envelopes.
- `risk_breaches`: Relationally user-owned risk rule breaches, linked to the
  exact broker account whenever the execution context is account-attributable.
- `api_audit_logs`: Immutable audit trail.
- `user_notifications`: User notifications.
- `outbound_webhooks`: Outbound webhook configurations.
- `feature_flags`: Feature flags for experiments.
- `user_feature_flags`: User-specific feature flag assignments.

#### J) Classification & Scoring
- `sectors`: Sector and industry hierarchy for grouping instruments.
- `instrument_sectors`: Many-to-many mapping of instruments to sectors with weights.
- `canonical_signals`: Normalized indicator signals with provenance.
- `asset_scores`: Aggregated scores for individual assets/instruments.
- `sector_scores`: Aggregated scores for sectors (groups of instruments).
- `market_scores`: Aggregated scores for entire markets/asset classes.
- `sizing_profiles`: Reusable position sizing profiles for users.
- `user_strategy_bindings`: User's binding to strategies with thresholds and execution preferences.
  New bindings are inactive/manual with no entry or exit authority. Entry
  execution requires `is_active`, `autopilot`, and `entries_enabled`; a
  close-only binding uses `is_active=true`, `entries_enabled=false`, and
  `exits_enabled=true`. The database rejects authority on inactive bindings and
  rejects a second active strategy for the same account/instrument.
  - `asset_score_threshold`: Minimum asset-level score to trigger execution (required, default 0.6)
  - `sector_score_threshold`: Optional minimum sector-level score (AND logic with asset)
  - `market_score_threshold`: Optional minimum market-level score (AND logic with asset and sector)
- `mode_performance`: Historical performance metrics per execution mode for optimization.
- `scoring_rules`: Configurable scoring rules for signal aggregation.
- `execution_decision_logs`: Audit log of execution decisions made by the scoring engine.

#### K) Feedback Loop & Backtests
- `signal_performance`: Track prediction accuracy for each signal to evaluate strategy performance.
- `strategy_parameter_feedback`: Store parameter optimization suggestions and review decisions; no runtime apply state or config-file write.
- `backtest_experiments`: Group backtest runs for parameter sweeps and walk-forward experiments.
- `backtest_results`: Persist backtest results for strategies.
- `strategy_consecutive_wrong_tracker`: Track consecutive wrong predictions per strategy per instrument.

### Local Full-Stack Compose

For end-to-end validation, use the production-parity compose stack:

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml up -d
```

By default, this starts the database bootstrap chain and the runtime control/data
plane. The wheel-installed indicator worker is started explicitly with the
`indicator` profile and an exact `STRATEGY_LIST`.

It launches:
- PostgreSQL plus migration, service-role, and source-controlled seed one-shots
- scoring, execution, feedback, primary market-data, backend, and the independent
  observed-FX process (the latter reuses the market-data image on internal port
  `8004`)
- `indicator-runner` only when the `indicator` profile is selected

The default operational bounds are
`SCORING_OUTBOX_MAX_AGE_SECONDS=300`,
`INDICATOR_MAX_SIGNAL_BACKLOG_AGE_SECONDS=300`,
`INDICATOR_MAX_STRATEGY_LAG_SECONDS=300`, and
`EXECUTION_PAPER_ORDER_MAX_LAG_SECONDS=300`. A service may be alive while
`/ready` is false because a durable partition has stopped advancing; inspect
the corresponding metrics before restarting it.

**Mode Configuration**:

The stack supports three execution modes via environment variables:

```bash
# Paper mode (default - safe for development)
EXECUTION_MODE=paper docker compose --env-file .env -f docker/docker-compose.stack.yml up -d

# Backtest mode (signals from historical data only)
EXECUTION_MODE=backtest docker compose --env-file .env -f docker/docker-compose.stack.yml up -d

# Live execution remains disabled for this migration and local verification.
```

**Start Specific Strategy Services (Profiles)**:

```bash
# Start the indicator-runner with an explicit benchmark allowlist
STRATEGY_LIST=SwingHighLowPMO \
docker compose --env-file .env -f docker/docker-compose.stack.yml --profile indicator up -d
```

Strategy runner overrides (strategy containers fall back to `EXECUTION_MODE` when `RUN_MODE` is unset):

```bash
# Override strategy run mode without changing execution engine mode
RUN_MODE=backtest docker compose --env-file .env -f docker/docker-compose.stack.yml up -d

```

Service images are built via `vmdev build docker --from-config --tag latest`;
the compose stack uses the corresponding `vynmatrix/*:latest` images.

---

## 13. Dependency Graph

### Visual Dependency Flow

```
lib_common
├── lib_data
│   ├── lib_strategy
│   │   └── lib_application
│   │       └── lib_infrastructure
│   └── lib_application
└── lib_indicators

lib_strategy + lib_indicators
└── indicator strategies
    └── indicator_runner
```

### Build Order (Critical)

```bash
# 1. Libraries (in order)
vmdev build libs --component=lib_common
vmdev build libs --component=lib_data
vmdev build libs --component=lib_indicators
vmdev build libs --component=lib_strategy
vmdev build libs --component=lib_application
vmdev build libs --component=lib_infrastructure

# 2. Strategies
vmdev build strategies

# 3. Virtual environments
vmdev build venvs

# 4. Config-declared application Docker images
vmdev build docker --from-config --tag latest
```

---

## 14. End-to-End Flow Verification

> **Canonical guide**: [E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md) —
> declared-Compose verification with real market data, bounded authority,
> durable restart evidence, exact persistence lineage, and progress readiness.

This is a composite proof. Cold-start and rebuild history advances strategy
state with emissions suppressed. An old real signal sent through normal
scoring/outbox delivery must create no order at the execution freshness gate;
the explicitly authorized `replay_canonical_signals.py` path separately proves
historical paper fills, P&L, and restart recovery. Only a current-time signal
during the paper soak can prove the complete normal economic path.

For the synchronized equity branch, replace the per-symbol insight path with a
registered complete panel and require this persisted chain before counting an
economic action:

```text
equity source lineage + observations + factor snapshots
  → registered strategy_panel_input_revision
  → completed strategy_panel_decision + rank + state + audit
  → signals.rebalances.submit
  → immutable model_rebalance
  → tenant/account_rebalance_plan + execution.rebalance.commands
  → exit/reduce phases confirmed before entry phase
  → canonical orders/fills/positions/P&L/NAV/feedback
```

A recorded `USQualityCompounder` diagnostic is documented in its
[strategy README](../strategies/indicator/USQualityCompounder/README.md). It
failed the paper-promotion bar and grants no account or order authority. The
strategy remains disabled by default, while its separately gated prospective
exact-owner EODHD/SEC producer can create a qualified `paper_forward` panel in
an isolated paper environment. Historical-validation artifacts cannot satisfy
that scope. Do not manufacture a panel, loosen scope/entitlement checks, or
report unit/integration tests as this E2E result.

### Pipeline Summary

```
Strategy (SignalWorker, indicator-runner)
  → atomic model state + source watermark + strategy decision
    → signals.submit outbox
      → DurableSignalRelay via HttpSignalEmitter to scoring /api/v1/signals
    → canonical_signals persisted
    → asset_scores + market_scores computed
    → user_strategy_bindings evaluated (threshold + strategy scope + autopilot)
    → execution_decision_logs written with exact signal/account lineage
      → execution.commands outbox
        → current authority revalidated immediately before broker I/O
          → canonical order/pending-order/fill state persisted
          → position, P&L, NAV, and execution-metric projections updated
        → observed FX normalizes P&L/NAV to account/user base currency
  → scheduled Feedback loop
    → exact-lineage signal_performance written once per horizon
    → strategy_consecutive_wrong_tracker updated
    → strategy_parameter_feedback suggestion created when threshold reached
      → approval is review-only; source/config promotion is a separate change
```

### Quick Verification (after a paper run)

```sql
-- Pipeline health check (all counts > 0 except sector_scores)
SELECT 'canonical_signals' AS tbl, count(*) FROM canonical_signals
UNION ALL SELECT 'asset_scores', count(*) FROM asset_scores
UNION ALL SELECT 'execution_decision_logs', count(*) FROM execution_decision_logs
UNION ALL SELECT 'execution_logs', count(*) FROM execution_logs
UNION ALL SELECT 'signal_performance', count(*) FROM signal_performance
ORDER BY 1;

-- Decision status breakdown
SELECT should_execute, status, count(*)
FROM execution_decision_logs GROUP BY should_execute, status;

-- Feedback accuracy
SELECT is_correct, count(*) FROM signal_performance GROUP BY is_correct;
```

### Cross-Container Tracing

Every signal carries a `run_id` UUID through the entire pipeline. To trace a single
signal from strategy to feedback:

```bash
# Find a run_id
docker compose --env-file .env -f docker/docker-compose.stack.yml exec -T postgres psql -U trader -d vm_trading -t -A -c \
  "SELECT run_id FROM canonical_signals LIMIT 1"

# Search all engine logs
docker compose --env-file .env -f docker/docker-compose.stack.yml logs scoring-engine 2>&1 | grep "<run_id>"
docker compose --env-file .env -f docker/docker-compose.stack.yml logs execution-engine 2>&1 | grep "<run_id>"
```

For the full step-by-step guide with real data examples, see **[E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md)**.

**Local scope**: Use this migration's isolated Compose endpoints and database. No existing cloud or personal runtime is a verification target.

---

## Document History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 2.0 | July 2026 | Codex | Aligned the manual with the three-stage scoring pipeline and current source tree; removed the stale generated file inventory |
| 1.1 | December 2025 | Codex | Consolidated docs; added workflow/env parity, bundle notes, verification steps |
| 1.0 | December 2025 | Claude | Initial comprehensive manual |
| 1.1 | December 2025 | User + Claude | Added: Recent foundations (canonical signals, alias resolver, Postgres-first, feedback loop, backtest persistence); Migrations & Local DB section; Simplified E2E flow for signal-only contract; Feedback loop tables |
| 1.2 | December 2025 | Claude | Added scoring domain, service, and pipeline documentation; updated the migration inventory |
| 1.3 | December 2025 | Claude | Added migration 0006 (BigInteger for high-volume tables); historical alignment note now superseded by current CLAUDE.md inventory |
| 1.4 | January 2026 | Claude | Production readiness: Added mode enforcement (backtest/paper/live), performance metrics services (PnL, drawdown, strategy metrics), idempotency/deduplication, graceful shutdown, config validation, broker stubs for Coinbase/Deribit/IBKR |
