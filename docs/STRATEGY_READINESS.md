# Strategy readiness

This is the current source-configuration and authority inventory. It is not a
paper soak, broker certification, deployment, or performance record.

| Strategy | Source state | Role |
| --- | --- | --- |
| [SwingHighLowPMO](../strategies/indicator/SwingHighLowPMO/README.md) | enabled in dev, E2E pipeline canary, 1.1.0 | Development pipeline canary |
| [USQualityCompounder](../strategies/indicator/USQualityCompounder/README.md) | disabled in dev | Equity portfolio research |
| [_template](../strategies/indicator/_template/config.json) | disabled in dev | Development scaffold |

Current boundaries:

- Swing requires exact maintenance activation, owner/account/binding authority,
  an explicit selector, and fresh real warm-up data; it is permanently excluded
  from paper promotion and live trading.
- USQuality requires prospective owner-scoped data/panel/catalogue/session
  evidence, account reconciliation, and separate paper authority.
- The template is not an execution or performance candidate.

The shared platform image shipping a strategy is not authority. STRATEGY_LIST,
strategy/version state, owner, binding, exact account, instrument route, session
coverage, entitlement, and current execution checks are independent. Bootstrap
registers inactive references; it creates no broker account, binding, or route.

The maintenance canary action applies only to the exact eligible Swing release
with paper mode and the live gate false. It enables no account/binding and does
not generalize to another candidate. Use
[E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md) for its evidence and
[DATABASE.md](DATABASE.md) for registration/activation boundaries.

## Backtrader migration

### Selection and design

Selection is for engineering suitability, not a demonstrated profitability ranking.
The read-only source catalogue is `Trading/backtrader_strategies/{Codes,Manuals}`:
260 Python files; 247 parseable files declare a direct Backtrader Strategy subclass,
three are harness-only, and ten contain syntax errors. Strategy-bearing optimization,
live, aggregation, and stress-test harnesses are excluded from candidate selection.

Apply these criteria in order: causal and auditable rules; distinct decision mechanism;
available price/volume inputs; reproducible state and affordable dependencies; native
framework fit; credible out-of-sample evidence. No stored performance results were
found in the source tree, so that last criterion remains unproven for every selection.
Near-duplicates compete within their family; an extra filter alone does not justify
an eleventh port. Existing SwingHighLowPMO and USQualityCompounder are retained.

| Native strategy | Source code / manual section | Mechanism and reason selected | Overlap excluded / material correction |
| --- | --- | --- | --- |
| ATRBreakout | ATRBreakoutStrategy.py / 30 | Prior close-channel breakout, ATR expansion, bounded holding period | Donchian/high-low and other ATR breakout variants; source `max_hold_days` counts bars |
| BBSqueezeBreakout | BBSqueezeBreakout.py / 36 | Prior bandwidth-rank squeeze followed by band breakout; optional volume confirmation | BB/Keltner squeeze variants; percentage threshold must match rank units |
| WilliamsPullback | WilliamsPullbackStrategy.py / 242 | Williams %R recovery in EMA trend, confirmed by MACD histogram | RSI/channel pullback variants |
| VortexTrendCapture | VortexTrendCaptureStrategy.py / 233 | Directional movement crossover with SMA and ATR-percent gates | Other Vortex, ADX directional crossover variants |
| TimeDecayAdaptiveEMA | TimeDecayAdaptiveEMAStrategy.py / 190 | Volatility-dependent exponential smoothing and price crossover | Adaptive moving-average wrappers; update filter while a position is open |
| ZScoreMeanReversion | ZScoreMeanReversionStrategy.py / 245 | Price z-score extremes in an ADX-defined range; exit at mean | OU/Bollinger reversion variants; remove stop-fill/order recursion |
| EngulfingPattern | EngulfingPatternStrategy.py / 64 | Strict body engulfing confirmed by RSI extremes | Candlestick/volatility-spike wrappers; remove stop-fill/order recursion |
| AdaptiveVWAPMeanReversion | AdaptiveVWAPMeanReversionStrategy.py / 12 | Rolling volume-weighted mean, RSI/ADX gates, tightening trail | VWAP reversion variants; restore intended initial protection and nested warm-up |
| VolatilityClusteringReversion | VolatilityClusteringReversion.py / 211 | Fade a consecutive volatility cluster with trend alignment | Volatility-spike/divergence variants; test prior stop before current-bar ratchet |
| HurstRegime | HurstRegimeStrategy.py / 90 | Hurst-gated MACD trend or stochastic reversion | Regime/ML wrappers; explicit estimator and valid warm-up |

KamaDynamicMomentum is excluded: its positive-ratio oscillator is compared to zero,
and the short exit is unfinished. OBVReversion uses a history-origin-dependent
threshold and reads a previous close on the first bar. Entropy's default threshold
exceeds the maximum entropy of its bins. Session and gap candidates need authoritative
venue/session inputs absent from their hard-coded rules. ML candidates have no
reproducible trained artifacts, leakage-safe evaluation, or measured incremental value.

Ports retain executable numeric defaults; documented intent takes precedence over
demonstrated code defects, with deviations recorded beside each strategy’s rules. Native strategies contain no Backtrader runtime or broker
API calls. Shared calculations belong in `lib_indicators`; shared bar/model lifecycle
belongs in `lib_strategy`. Each strategy owns `core.py`, disabled `config.json`,
rule tests, and its concise rule/data contract. No second registry or runtime is added.

Price-only rules are candidates for catalogued FX, equities, ETFs, crypto, futures,
and perpetual contracts when feed, sessions, contract/FX terms, and shorting authority
are explicitly available. This is applicability, not validation. VWAP and enabled OBV
confirmation additionally require comparable venue-traded volume; OTC FX tick volume
is not interchangeable. Futures rolls, funding, borrow costs, and adjusted-equity
prices require explicit datasets and evaluation conventions.

### Implementation and evidence checklist

- [x] Inspect source rules/manuals, target contracts, and overlapping families; select ten.
- [x] Add independently checked shared indicator and bar/model lifecycle tests, then implementations.
- [x] Port all ten cores, disabled configurations, initial rule tests, and source/default/deviation contracts.
- [x] Compare source/native decisions on the same recorded real bars; test replay and isolation.
- [x] Evaluate comparable retrospective out-of-sample windows with costs and overlap diagnostics.
- [x] Verify discovery/packaging, affected pipeline suites, formatting, typing, and strict audit; retain the unrelated fixture failures below.
- [x] Complete authorized historical/paper evidence; record unsupported markets and external prerequisites.

The checked-in Coinbase BTC-USD minute fixture contains 1,501 real bars. It
supports component and decision-parity checks. The daily research evidence below
adds multi-year comparisons; neither evidence set is a production attestation.
New strategies remain disabled and independently unapproved.

### Implementation verification

Verified in the normal vynmatrix checkout after transferring the ten ports:

- Ten native cores/configurations load with the worker constructor contract. All ten
  reproduce uninterrupted signals, identities, warm-up and state after JSON restoration
  with no bootstrap, both during warm-up and after 700 recorded bars.
- Explicit versioned checkpoints retain bounded indicator windows, recursive values,
  per-symbol accepted counts and timestamps. Invalid configuration, missing fields,
  nonfinite values, inconsistent composite state and missing protection fail before
  replacing live state. Consolidator snapshots retain partial OHLCV, coverage and
  constituent provenance without emitting during restore.
- `vmdev test all` passes 3,907 tests with 125 opt-in skips and 74.16% coverage.
  The separately configured native PostgreSQL gate passes three tests, including
  owner UI reads through the backend service role. CI provisions its own fresh
  database and runs this gate explicitly. Formatting and lint pass for 61 changed
  Python files; scoring/feedback typing passes for 39 files; strict audit scans
  1,068 files without findings. Additional opt-in PostgreSQL results and broker
  credential prerequisites are recorded below.
- Native workers atomically persist core state, raw inception, original bootstrap
  boundary, acknowledged row identity/revision, and partial consolidation. Restart
  restores before querying recent history and checks every configured feed, including
  empty feeds. Correction replay starts at the original inception and preserves the
  raw bootstrap/decision boundary with emissions suppressed. Recorded-bar regressions
  cover partial buckets, gap-triggered emission, missing anchors/streams/buckets,
  failed commits, database read failures, and rebuild-generation races. Existing
  Swing and panel-worker tests pass.
- Library/strategy wheels and the validation environment were rebuilt with the
  checkpoint and five-day signal-horizon implementations before the final comparisons. Backtrader 1.9.78.123
  and [TA-Lib 0.6.8](https://pypi.org/project/TA-Lib/0.6.8/) are validation-only pins.
  The source Bollinger squeeze uses TA-Lib OBV even when volume confirmation is
  disabled; its default enables that filter.

### Source comparison evidence

The validation-only capture extracts hash-bound strategy/indicator classes without
executing source imports, download/plot harnesses, or custom feed classes. It uses
Backtrader's original `runonce=True` mode and pins loaded package-code digests,
configuration, input, source, and capture identities. Source market submissions and
native signals are compared as decisions; protective orders and individual fills
are recorded separately. Zero capture costs isolate behavior and are not performance
assumptions. A repeated capture produced identical bytes before witness export.

| Strategy | Source/native decisions | Observed agreement or first material difference |
| --- | --- | --- |
| ATRBreakout | 28 / 28 | Every entry matches; holding age begins at native signal, not next-bar source fill |
| BBSqueezeBreakout | 102 / 36 | Correct 0–1 percentile/percentage units; all 1,443 common OBV/SMA outputs match exactly |
| WilliamsPullback | 77 / 28 | Manual histogram gate rejects source MACD-line short at bar 50 |
| VortexTrendCapture | 83 / 83 | Every entry matches; signal/fill holding-age boundary differs |
| TimeDecayAdaptiveEMA | 1 / 143 | Source entry-order latch blocks all later management; native full-window filter and protection continue |
| ZScoreMeanReversion | 242 / 236 | Every entry matches; source recursively creates stops after exits, including 17 fills at bar 817 |
| EngulfingPattern | 0 / 0 | Flat path only; this fixture supplies no entry evidence |
| AdaptiveVWAPMeanReversion | 197 / 264 | Source stop fill permits same-bar re-entry; native emits CLOSE before a later entry |
| VolatilityClusteringReversion | 28 / 28 | Every entry matches; native tests prior protection before ratcheting the current bar |
| HurstRegime | 71 / 73 | Decisions match before bar 554; signal-bar versus fill-bar protection changes the next exit |

The frozen [source witnesses](../tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10_backtrader_ports_reference.json)
and [comparison tests](../tests/test_migrated_strategy_source_reference.py) cover these
observations.
Full captures remain under `.artifacts/research/strategy-validation/`. New captures
and witness exports use content-addressed `migration-source-references/` and
`migration-source-witnesses/` manifests, preserving earlier inputs. Reproduce with
the prepared validation environment; review the export's `manifest` body before
updating a checked-in witness and its test hash:

```bash
PYTHONPATH=tools/dev_cli EXECUTION_MODE=paper EXECUTION_ENGINE_ALLOW_LIVE=false \
  build/venvs/strategy-validation/bin/python -m dev_cli.validation.backtrader_reference \
  --repo-root . --source-dir /path/to/backtrader_strategies/Codes \
  --input tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json
```

The [daily Engulfing fixture](../tests/fixtures/market_data/coinbase_btc_usdc_1d_2019-10-01_2022-06-26.json)
retains the first 1,000 contiguous provider days and original indicator inception.
Its [source witnesses](../tests/fixtures/market_data/coinbase_btc_usdc_1d_2019-10-01_2022-06-26_engulfing_reference.json)
show SHORT at bar 461 and LONG at 992. A source protective fill flattens at 463,
then recursively creates a stop that reopens short at 465 without a pattern. Another
recursive short at 988 suppresses the native LONG. Native SHORT/CLOSE and LONG/CLOSE
cycles, including JSON restart while each position is open, now have recorded-data
coverage. All 43 source, retrospective-orchestration and benchmark tests pass.

### Retrospective daily comparison

A complete 2,284-bar public dataset covers 2019-10-01 through 2025-12-31.
Original provider strings and current BTC-USDC → BTC-USD alias metadata are retained
in `.artifacts/research/strategy-validation/coinbase-btc-usdc-daily-4174577365a17ae2e9574567a8e04d4ed001a92f06b4ede600fb65b595cd0e38.json`;
the candle-array SHA-256 is
`1d3fd7c34a0b3471801a282a93e96eed1b309cda58a11bc4e493f4e1df5fc729`.
Accounting uses hypothetical canonical-book price units, with no historical
USDC/USD valuation or conversion. Direct daily volume does not qualify the
configured minute-consolidated production VWAP/OBV feed.

The design freezes numeric defaults, 2020–2021 descriptive history and separate
2022–2025 annual holdouts before evaluation. Each window warms from the same
inception and starts flat. Spot economics apply only a `long_only` override;
unchanged bidirectional defaults have separate signal-only diagnostics because
borrow, margin, funding and derivative contract costs are unavailable.

The existing reference engine sizes at 95% of 100,000 initial units, decides at
completed close and fills at the next open. One frozen decision ledger is replayed
through gross, expected and stressed scenarios. Expected/stressed commission is
120/180 bps per side, plus the existing protocol's spread/impact assumptions;
stress also delays fills by one full daily bar. These are backward-applied
sensitivity assumptions from the 2026-07-21 template, not period-matched historical
fees/order books. Venue rounding/minimums and cash interest are not modeled.

| Strategy | Expected closed trades, 2022–25 | Gross pooled return | Expected pooled return | Stressed pooled return |
| --- | ---: | ---: | ---: | ---: |
| ATRBreakout | 13 | +69.99% | +26.56% | +1.20% |
| BBSqueezeBreakout | 14 | −0.89% | −28.36% | −34.23% |
| EngulfingPattern | 5 | −6.87% | −17.10% | −1.13% |
| VolatilityClusteringReversion | 3 | +26.92% | +18.69% | +25.23% |
| WilliamsPullback | 5 | −19.42% | −28.40% | −36.24% |
| ZScoreMeanReversion | 49 | −13.10% | −72.05% | −57.65% |
| AdaptiveVWAPMeanReversion | 51 | — | — | — |
| HurstRegime | 19 | — | — | — |
| TimeDecayAdaptiveEMA | 37 | — | — | — |
| VortexTrendCapture | 19 | — | — | — |

Pooled returns compound independently reset annual portfolios, not one continuously
traded account. A dash means unresolved terminal exposure prevents pooling; the
full reports retain marked equity, open positions and per-window results. Stressed
VWAP and Vortex also expose model/execution position-lineage blockers. Delayed fills
can cancel short-lived signals or improve entry prices, so stress returns need not
be worse. Closed-trade statistics are explicitly distinguished from whole-window
equity, costs and turnover, including the unliquidated buy-and-hold benchmark.

All 50 bidirectional window/prefix comparisons preserve earlier decisions when
future bars are removed. The run produces 150 strategy reports and 30 cash/buy-and-hold
benchmark reports. Of 45 strategy pairs, 15 have eligible aligned return correlation
(range −0.007 to 0.290); four blocked strategies remain outside that calculation.
Maximum raw long-entry Jaccard overlap is 0.084 (VWAP/ZScore). Sparse trades and
retrospective, single-book coverage do not establish diversification or profitability;
no parameters or selection were retuned from these outcomes.

The reviewed run is bound to design
`migration-designs/081a5cc4acee59e43639f285d20ac1fcc30bff9314bef1f257fcef4afc46c023.json`
and results
`migration-results/d4565c5af3758d40ff142faa2d4c71a37965f96fca475b23b6054c52bc8d4750.json`
under the research artifact directory. Final daily source/native capture
`migration-source-references/3ae3c94b797fdf8af5661638ef4e56e5275c2bc19e9a9b85c69757f6cab88920.json`
preserves every previously observed decision. Reproduce with:

```bash
PYTHONPATH=tools/dev_cli EXECUTION_MODE=paper EXECUTION_ENGINE_ALLOW_LIVE=false \
  build/venvs/strategy-validation/bin/python -m dev_cli.validation.migration_evaluation \
  --repo-root . \
  --dataset .artifacts/research/strategy-validation/coinbase-btc-usdc-daily-4174577365a17ae2e9574567a8e04d4ed001a92f06b4ede600fb65b595cd0e38.json
```

### Historical paper acceptance

The [native PostgreSQL gate](../tests/test_migrated_strategy_pipeline_postgres_integration.py)
runs actual cores on the [recorded BTC-USD input](../tests/fixtures/market_data/coinbase_btcusd_native_ports_pipeline.json):
1,000 daily bars and 304 minute bars covering the first LONG/CLOSE pair of each
strategy. Product metadata explicitly identifies USD settlement. The only core
overrides select BTC-USD and long-only spot operation; numeric rules retain defaults.
Fixture SHA-256: `e5c5033c4b323662eb9464a7a829eeb185cef2420b68712c6b6c3eb71db16fde`.

The successful run uses isolated database `vm_ports_pipeline_test_d57f4c2dcae5`
(OID 50005, revision `0107_backend_ui_read`) on the existing declared PostgreSQL
server. Shared role attributes and memberships are unchanged. Test-only owner,
account, strategy/version and binding authority never modifies operational authority.
The local `ports-pipeline-database.json` and `ports-pipeline-test-result.json`
artifacts retain database identity, checks and implementation/input hashes.
The normal-checkout rerun also passes in fresh database
`vm_ports_pipeline_test_main_20260919`, with six runtime roles verified unchanged.

- Duplicate HTTP ingestion yields 20 canonical signals, 20 positive scoring decisions
  and 20 transactional commands. Canonical horizons remain 432,000 seconds, and
  original run/envelope identities survive redelivery and later account routing.
- Ordinary delivery rejects all ten historical entries specifically as stale and
  produces no fills. Explicit historical replay then completes nine entry/exit
  pairs: 18 distinct fills at recorded next-15-minute opens with configured slippage
  and fees, exact owner/account/signal lineage, and no remaining positions.
- BBSqueeze's entry rejects specifically `stop_loss_required`; its CLOSE has no
  position to flatten. This faithfully exposes its source's missing protective stop;
  the port is not executable under the tested policy. No protective value is invented.
- Repeating all ten replays returns explicit deduplication with unchanged decisions,
  orders, executions, risk breaches and execution-log identities.
- The least-privilege feedback login writes ten one-week evaluations with matching
  recorded close provenance and fractional returns; nine have execution evidence.
  Repeating evaluation writes none. Scoring uses only completed-bar closes; session
  daily bars without authoritative close times fail closed.
- The backend login reads all ten strategies, their CLOSE signals and bindings,
  nine realized-P&L projections and eighteen fills through the owner UI API in
  read-only transactions. Browser verification covers unlock, Dashboard,
  Strategies, Profit and loss, the eighteen-fill table, and Lock clearing the view.
  These are isolated test bindings; source configurations remain disabled.

This is bounded historical integration through in-process production services and
PostgreSQL, not a Docker soak, minute-to-daily feed qualification, investment
certification, or authorization for other markets. Source configurations and runtime
selectors remain unchanged. Broker sandbox checks still need explicit credentials.

Additional local PostgreSQL checks pass for durable crash recovery, twenty-worker
redelivery/connection limits, LISTEN/NOTIFY, feedback concurrency, execution claims,
replica/account serialization, and repeated scoring ingestion (nine tests).
`ports-runtime-test-result.json` records the separate run; it does not replace the
native-strategy result.

Three unchanged fixture setups fail before modified scoring code is reached:

| Existing PostgreSQL check | Verified fixture defect |
| --- | --- |
| `test_postgres_same_session_later_cutoff_race_is_worker_serialized` | Owner seeded in a translated schema; production owner authority explicitly reads `public.users` |
| `test_postgres_concurrent_submission_replay_correction_and_restart_acceptance` | Same owner-schema mismatch |
| `test_postgres_batch_rollback_replay_tenant_fence_and_outbox_ordering` | Connection timezone options override fixture search-path options; binding supplies instrument ID `8802` where the production trigger requires canonical `LINEAGE` |

These model-fixture defects remain outside the migration. Repair requires isolated,
fully migrated public-schema fixtures with explicit owner/catalogue authority, not
weaker production checks. Their setup failure is not strategy acceptance evidence.
