# US Quality Compounder

This strategy documentation is inherited design/research context. vynmatrix has
no transferred paper approval, live certification, account, or deployment. See
[the source readiness inventory](../../../docs/STRATEGY_READINESS.md).

A quarterly, long-only US equity strategy for a concentrated portfolio of durable businesses.
The design translates quality, growth, valuation, capital discipline, and margin-of-safety ideas
into measurable rules. It does not use discretionary intuition or produce current stock tips.

## Status

| Item | Current state |
|---|---|
| Strategy ID / current version | `us_quality_compounder_v1` / `0.2.0` |
| Phase-one universe | Point-in-time S&P 500 membership |
| Benchmark | SPY total return |
| Portfolio | Target 15 stocks; 1–25 allowed by policy; unused capacity is cash |
| Decision cadence | Final official XNYS session of each calendar quarter |
| Code status | v0.2 factor, panel, selection, historical replay, and paper-execution boundaries implemented |
| Runtime status | Disabled, development-only, signal mode |
| Evidence status | Sequential recorded-data experiments and v0.2 smoke backtest completed; not accepted for paper promotion |

Version `0.1.0` is retained as deprecated immutable lineage. Version `0.2.0` is the active
candidate version only so new factor evidence can reference it. The strategy catalogue row,
source config, and account bindings remain inactive. Live trading is out of scope.

### Recorded-data smoke result — 2026-08-16

A replacement EODHD v4 snapshot was acquired for 2023-11-24 through 2026-01-06 and admitted only
for `historical_validation/diagnostic` use. Its immutable identity is
`958d282be4ffc5927ab1749fad41f40743bee500d81963f8e31c0a35af69fce2`. The snapshot remains
explicitly incomplete and non-confirmatory because registered membership, permanent-identity, and
split-volume limitations remain; it grants no paper-promotion authority.

The original v0.1 replay exposed an evidence-design failure: only 18–156 of roughly 504 members
were factor complete, with strong quarter-to-quarter seasonality. We therefore changed the model
sequentially and accepted a change only when its pre-declared coverage and correlation checks
passed:

| Experiment | Isolated change | Result | Decision |
|---|---|---|---|
| EXP-00 | Exact leave-one-component-out diagnosis | Filing drift alone recovered only about 35–38%; operating-company gross profitability was the major co-blocker | Reject a one-factor patch |
| EXP-01A | Replace gross profitability with operating profitability at the same 25% quality-component weight | Quality coverage 74.8–77.4%; whole-model complete count 35–266 | Accept into candidate |
| EXP-01B | Remove filing drift; use 50% revenue growth plus 50% issuer-appropriate fundamental growth | Whole-model complete count 311–325; coverage 61.7–64.4%; no named-sector coverage failure; growth/momentum median absolute Spearman 0.157 | Accept as v0.2 |

The content-addressed experiment manifest identities are EXP-00
`7692d512d6051bcbe8de36d7312e981d61f218bb306912359eed0a9e780ec7c5`, EXP-01A
`ee99b7bf32a7818e1bff73ea12ca96566d054f43fde4d34792113ba2b5fc8827`, and EXP-01B
`57b90240eb746bcb55faadf9855b32921070cd27abf9ffc138dd3b2268f39f71`. They are diagnostic
evidence, not deployable inputs.

The v0.2 configuration keeps the outer 35/30/20/15 weights unchanged. Its exact immutable policy
identity is `950fa8d190f9793224289c74c54e946eaf305d369bf4746194666d134f00ff74`.
The verified v0.2 panel manifest is
`cf1b77cdb96f97bace588e8de81c342feda7efeea3e214fae376ac53aab838d4`.
The prior v0.1 panel manifest `adf49eee01e01d53cebd7d72df6352f043e2d44502056dcb4f335bc00b07f2d8`
remains historical evidence and is not relabelled.

| Decision -> execution | v0.2 factor-complete / members | Market-eligible | Target book after rebalance |
|---|---:|---:|---|
| 2024-12-31 -> 2025-01-02 | 311 / 504 | 137 | TROW, PEG, FICO, RSG, MSFT, IT, JPM |
| 2025-03-31 -> 2025-04-01 | 323 / 504 | 192 | TROW, FICO, RSG, MSFT, JPM, BX, OKE, UAL |
| 2025-06-30 -> 2025-07-01 | 325 / 505 | 175 | TROW, JPM, BX, MSFT, UAL, GOOG |
| 2025-09-30 -> 2025-10-01 | 322 / 504 | 147 | GOOG, MSFT, VST |
| 2025-12-31 -> 2026-01-02 | 323 / 504 | 153 | RMD, MU, LRCX, OKE |

Every selected name in this diagnostic was large-cap. No capitalization quota was filled, and no
sector used more than three equal-weight slots (20% target weight), below the 25% sector cap.

Both synchronized-panel backtests ran from 2024-12-31 through 2026-01-06 with USD 1 million,
next-session execution, recorded splits/dividends, SPY total return, and explicit commissions,
spread, slippage, impact, and sell fees. v0.2 was evaluated as a labelled comparison portfolio
because the immutable source snapshot is owned by the earlier v0.1 acquisition.

| Metric | v0.2 candidate | v0.1 baseline | SPY where applicable |
|---|---:|---:|---:|
| Total return | 11.45% | 8.48% | 18.99% |
| CAGR | 11.26% | 8.34% | — |
| Annualized volatility | 11.24% | 5.46% | — |
| Maximum drawdown | 9.93% | 2.73% | 18.76% |
| Sharpe / Sortino | 1.02 / 1.54 | 1.51 / 2.59 | — |
| Two-way turnover | 1.92x/year | 0.84x/year | — |
| Average / peak gross exposure | 40.40% / 55.37% | 12.56% / 31.45% | — |
| Closed trades / hit rate | 24 / 62.5% | 7 / 100% | — |
| Execution-cost drag | 0.093 percentage points | 0.044 percentage points | — |

v0.2 is the best supported current design because it passed the sequential evidence gates, not
because its one-year return was higher. It materially improves coverage, diversification, and
capital deployment, but also increases volatility, drawdown, and turnover and still trails SPY.
Its 61.7–64.4% factor-complete coverage remains below the provisional 80% paper-promotion gate.
The sample has only five decisions, 24 closed trades, and four unliquidated terminal positions.
It cannot establish long-term robustness or justify production selection.

The 1.0 bps round-trip commission input used while compiling the panels is explicitly recorded as
an unregistered diagnostic assumption. The snapshot is diagnostic-only and the result lacks the
attribution/capacity evidence required for a headline claim. Content-addressed evidence is
retained under `.artifacts`; all one-off experiment and replay code is deleted after verification.

## What We Are Building

The strategy ranks qualified S&P 500 share classes using four reproducible factors, applies
market and group risk gates, and holds a low-turnover target book. A stock is never purchased to
fill a capitalization or holding quota. Missing essential evidence, an inferior candidate, or a
closed market gate leaves cash.

Phase one deliberately uses the platform's existing point-in-time S&P 500 contract. It is a
narrow launch universe, not a claim that large caps are always superior. Mid- and small-cap names
are eligible when they occur in the qualified universe; broader US coverage is a later change.

Operating assumptions already supplied:

- Personal IBKR paper account with EUR base currency and US stock/API permission.
- US equities settle in USD. EUR cash, buying power, margin, or inferred FX never substitutes for
  observed settled USD.
- Netherlands tax domicile for eventual after-tax validation.
- EODHD All-in-One data is used only under the exact recorded personal entitlement owner.
- Retail Client Portal is the likely IBKR route. Its operator reauthentication requirement means
  indefinite unattended authentication is not claimed.

## Signal Model

Only data that can be reconstructed prospectively is enabled. Analyst revisions and news
sentiment are not assigned neutral values; they stay outside the MVP until raw point-in-time
payloads have been archived.

| Factor | Weight | Implemented definition | Required evidence |
|---|---:|---|---|
| Quality | 35% | Issuer-type profitability, cash return/conversion, accrual quality, and balance-sheet safety components | SEC facts from admissible 10-K/10-K-A accessions |
| Fundamental growth | 30% | 50% comparable revenue growth plus 50% operating-profit growth for operating companies, net-income growth for banks/insurers, or operating-cash-flow growth for REITs | Two comparable accepted annual filings |
| Valuation | 20% | Equal-weight issuer-type cash-flow/operating-income yield and book-to-market components | SEC facts plus cutoff-safe market capitalization |
| Momentum | 15% | Mean of 126-session and 252-session total returns, both ending 21 sessions before the decision date | Complete locally adjusted daily EOD history |

For stock `i`:

```text
score_i = 0.35 z(quality_i)
        + 0.30 z(fundamental_growth_i)
        + 0.20 z(valuation_i)
        + 0.15 z(momentum_i)
```

For operating companies and REITs, quality equally weights operating income / average assets,
operating cash flow / average assets, `(operating cash flow - net income) / average assets`, and
equity / assets. Banks and insurers instead equally weight return on assets, return on equity,
and equity / assets. Valuation uses the registered issuer-type combination of earnings,
cash-flow, operating-income, and book-to-market yields; no metric is silently substituted.

The shared ranker uses median/MAD normalization, population-standard-deviation fallback when MAD
is zero, and winsorization at ±3.0. Peers fall back from industry to sector to the full universe;
at least five observations are required. Enabled weights are never redistributed when a factor
is missing.

Sector and industry are separate gates to avoid counting the same momentum and growth evidence
twice. Each group score is:

```text
group_score = 0.40 z(median price momentum)
            + 0.30 z(positive-trend breadth)
            + 0.30 z(median fundamental-growth score)
```

At least five members per sector, three per industry, and five eligible groups are required.

## Exact Defaults

| Rule | Default |
|---|---:|
| Minimum market capitalization | USD 300 million |
| Small / mid / large buckets | `<2bn` / `2–10bn` / `>=10bn` |
| Minimum reference price | USD 5 |
| Minimum 126-session median dollar volume | USD 50 million |
| Maximum modeled round-trip entry cost | 40 bps |
| Maximum downside volatility / worst gap | 60% annualized / -15% |
| Market entry gate | SPY trend positive; breadth `>=0.50`; coverage `>=0.95`; realized volatility `<=0.35` |
| Entry | Top 10%, composite `>=0.50`, quality `>=0.25`, growth `>=0.00`, group scores `>=0.00` |
| Hold | Top 20%, composite `>=0.00`, quality `>=0.25`, growth `>=0.00`, group scores `>=-0.50` |
| Challenger replacement gap | 0.35 score units |
| Target holdings / normal weight | 15 / 6.67% |
| Small-cap position weight | Maximum 5% |
| Sector / industry caps | 25% / 15% |
| Aggregate mid / small caps | 40% / 20% |
| Fundamental / share-count age | 800 / 120 calendar days |
| Runtime panel freshness | 100 days |

There is no price-only stop. An incumbent exits when it leaves the effective universe, becomes
untradable, loses essential evidence, breaches a market/quality/growth/group hold gate, falls
outside the hold rank, or is replaced by a sufficiently better challenger. Valuation deterioration
reduces the valuation score and rank rather than triggering an arbitrary price threshold.

## Data and Failure Rules

| Input | Source and implementation | Failure behavior |
|---|---|---|
| Membership | EODHD current and historical S&P 500 component evidence at the decision checkpoint | Disagreement, absence, or ambiguous identity blocks the panel |
| Security identity | EODHD mapping and General evidence, reconciled to permanent security/issuer IDs | Missing CIK, exchange, taxonomy, USD quote, or catalogue match blocks the member |
| Market data | EODHD daily OHLCV, splits, and dividends; adjustments and cost estimates calculated locally | Missing official sessions, stale retrieval, or unresolved action fails closed |
| Fundamentals | SEC submissions, original filing headers, and accession-filtered Company Facts | Facts accepted after cutoff, stale facts, ambiguous contexts, or unreconciled SIC stay missing |
| Market cap | Latest admissible share-class shares within 120 days × raw decision close | Issuer-only multi-class shares are not copied to a share class; the stock is excluded |
| Calendar | Content-pinned ICE/NYSE compiler artifact imported as immutable XNYS evidence | Byte/hash mismatch or a different existing calendar blocks import |
| Broker identity | Shared instrument catalogue with exact positive IBKR stock conId | Symbol inference is forbidden; any coverage gap blocks the whole acquisition before HTTP fan-out |
| Account and orders | Fresh IBKR paper account, positions, quotes, settled-USD ledger, durable outbox and reconciliation | Missing/stale authority blocks entries; exits remain independently possible |

A recent listing may omit only one exact contiguous pre-listing price prefix backed by its
point-in-time listing date. Such names retain four factor rows with momentum explicitly missing.
The structural breadth exclusion is capped at 1%; unexplained gaps and incomplete SPY history
block the panel.

Unsupported in the MVP: retrospective analyst revisions, historical news completeness, vendor
fundamental snapshots "as known then," comprehensive merger/spinoff terms, and indefinitely
unattended retail IBKR authentication. These are not silently approximated.

## Automated Flow

```text
pinned official XNYS calendar
  -> EODHD membership, identity, prices, splits and dividends
  -> SEC filing/accession evidence
  -> one atomic DB transaction:
       immutable observations
       -> market and fundamental calculations
       -> exactly four factor rows per member
       -> derived manifest with full policy identity
       -> validated synchronized panel revision
  -> indicator ranking and target book
  -> canonical signal, rank/audit/model state
  -> transactional outbox and scoring account plan
  -> IBKR paper execution (exits before entries)
  -> fill/restart reconciliation and audit
```

All provider calls finish before the database transaction starts. Source evidence, factors,
manifest, and panel either commit together or all roll back. The producer checks the real clock
again immediately before registration and commit; crossing the next official open rolls back the
attempt. Replays are content addressed and idempotent.

The panel command is default-off and checks that two consecutive official sessions cross a
calendar-quarter boundary. An external scheduler may invoke it repeatedly; non-quarter-end and
already-complete runs are no-ops.

## Implementation Progress

Completed:

- [x] v0.2 four-factor contract, exact normalization, portfolio constraints, hysteresis, and exits.
- [x] Disabled strategy/config registration and quarterly 100-day runtime freshness.
- [x] Generic immutable observation writer and owner-scoped provider authority.
- [x] EODHD membership, permanent identity, SPY, prices, splits, and dividends acquisition.
- [x] SEC filing/header/Company Facts acquisition with exact acceptance-time handling.
- [x] Local split/total-return coordinates, transaction-cost evidence, market regime, market cap,
  and recent-listing handling.
- [x] Exactly four durable factor details per member; missing evidence is explicit.
- [x] DB-recomputed panel validator, policy-bound manifest, atomic registration, and one-shot
  quarter-end producer.
- [x] Exact pinned XNYS artifact importer using the existing ingestor image.
- [x] Forward registration coverage gate: 80% overall and 70% per sector with at least 10
  effective members, with deterministic complete/total diagnostics.
- [x] Generic reviewed, content-addressed equity catalogue/IBKR `STK` conId importer with
  dry-run, exact replay, required positive whole-share lot size, and existing-XNYS attachment.
- [x] Canonical signal/rank/outbox route, quarterly selection, and objective exit behavior.
- [x] IBKR paper guard requiring a coherent raw USD settled-cash ledger and an explicit entry cash
  buffer; EUR cash, FX, margin, and buying power cannot fund USD entries.
- [x] Generic content-addressed synchronized-panel writer/loader and historical runner path with
  exact strategy/snapshot identity, official-session adjacency, and closed evaluation windows.
- [x] Real SEC `-index-headers.html` parsing of the escaped full filing header, verified against an
  acquired filing rather than only a simplified fixture.
- [x] Replacement real EODHD v4 diagnostic snapshot and immutable v0.1 baseline evidence.
- [x] Exact EXP-00 missing-component diagnosis, EXP-01A quality substitution, and EXP-01B growth
  simplification using the same recorded evidence and pre-declared gates.
- [x] v0.2 registration and immutable factor/panel identities; v0.1 remains deprecated lineage.
- [x] Descriptive factor-risk model 2.0 aligned to the same raw operating-profitability evidence,
  with a calculator-to-risk-adapter integration test.
- [x] Five v0.2 historical panels and one synchronized comparison backtest with immutable source,
  panel, policy, and result lineage.
- [x] Explicit zero target-weight drift in the canonical strategy signal, rank-model scoring safety
  settings, and optional binding-level `entry_cash_buffer_bps` propagation to execution policy.

Still required before paper activation:

- [ ] Raise factor-complete coverage above the provisional 80% overall / 70% material-sector gate
  using a separately pre-registered evidence improvement. v0.2 reaches only 61.7–64.4%, so do not
  weaken the gate or tune portfolio thresholds against the smoke-test return.
- [ ] Acquire confirmatory membership/permanent-identity evidence and pass the stricter snapshot
  admission scope. The completed snapshot is diagnostic-only and explicitly incomplete.
- [ ] Review and apply a complete catalogue artifact for the qualified universe; the generic
  import/attachment seam is implemented, but current source-controlled coverage is partial.
- [ ] Compile/import the current official XNYS artifact and prove one real EODHD/SEC PostgreSQL
  panel during an actual quarter-end close-to-next-open window.
- [ ] Confirm the IBKR paper DU/account ID, retail gateway route, fixed-versus-tiered pricing,
  deployable NAV, and settled-USD funding amount/policy.
- [ ] Register the exact round-trip commission bps and `entry_cash_buffer_bps` from that pricing
  decision; do not use a generic placeholder.
- [ ] Bind the exact user, paper broker account, instruments, and owner-scoped delayed quote feed;
  keep entries disabled until reconciliation is clean.
- [ ] Add a paper-only IBKR credential route and certify an order-scoped exact-fill/fee reader.
  The current native paper adapter cannot yet satisfy the platform's complete fill contract.
- [x] Generalize the existing evidence-hashed promotion manifest to one exact synchronized
  portfolio/model-configuration/allowlist/binding/account/broker scope without adding a parallel
  gate. Rebalance legs must be a subset of the reviewed allowlist and all-cash remains valid.
  Runtime activation remains blocked by the other items in this list.
- [ ] Add the deployment scheduler/alert for the default-off one-shot and monthly data/risk check.
- [ ] Run the full paper E2E restart, idempotency, partial-fill, and reconciliation proof.
- [ ] Only after those gates pass: activate the strategy row, packaged config, and paper bindings.
  The quarter-end evidence producer needs only the registered active model version, so all three
  execution-authority surfaces remain inactive while the panel and promotion evidence are built.
  Do not enable live authority.

Optional later work:

- After the coverage gate passes, pre-register one valuation challenger at a time. Test
  cutoff-safe free-cash-flow yield as a replacement for cash-flow yield only if SEC capital-
  expenditure coverage is sufficient; do not add a correlated fifth factor.
- Prospectively archive analyst/news payloads before evaluating them as new factor versions.
- Expand from the S&P 500 to a reviewed US large/mid/small universe with joint EODHD/IBKR coverage.
- Add after-tax Netherlands reporting when the applicable account/treaty treatment is confirmed.

## Operator Commands

Build the validation runtime, then compile an official artifact using the retained compiler
(currently pinned through 2026-12-31):

```bash
vmdev build venvs
build/venvs/strategy-validation/bin/python \
  -m dev_cli.validation.backtest.equity_run compile-official-sessions \
  --output-root .artifacts/xnys \
  --start 2024-01-01 \
  --end 2026-12-31
```

Set the exact printed path and SHA-256, then import it through the declared service:

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  --profile quality-compounder run --rm quality-compounder-panel \
  python -m apps.market_data_ingestor.market_data_ingestor.main \
  quality-compounder-calendar-import
```

After the final official quarter-end close, the external scheduler runs:

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  --profile quality-compounder run --rm quality-compounder-panel
```

Required environment variables are documented in [`.env.example`](../../../.env.example). The
panel command remains a safe no-op while `QUALITY_COMPOUNDER_PANELS_ENABLED=false`.

## Validation

The v0.2 panel manifest passed the retained generic loader, codec/session checks, and artifact-hash
verification. The focused implementation suites cover the factor calculator, materializer,
persistence, panel validator, producer, quarterly job, strategy core, seed lineage, and runtime
selection/freshness boundaries. Exact final check counts are recorded with the implementation
handoff rather than copied here, where they would become stale.

The recorded-data run proves that the synchronized strategy can be evaluated honestly and that
v0.2 is a better-covered candidate than v0.1. It does not prove long-term robustness: it covers
about one year, only five decisions and 24 closed trades, uses a diagnostic-only snapshot, and is
USD pre-tax. EUR funding/FX, Netherlands tax treatment, a real PostgreSQL quarter-end panel, and
IBKR paper order/fill reconciliation remain separate gates. The strategy, runtime config, and
bindings stay disabled.

## Primary References

- [EODHD historical EOD data](https://eodhd.com/financial-apis/api-for-historical-data-and-volumes)
- [EODHD index constituents](https://eodhd.com/financial-apis/stock-market-indices-api)
- [EODHD splits and dividends](https://eodhd.com/financial-apis/api-splits-dividends)
- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [SEC fair-access policy](https://www.sec.gov/about/webmaster-frequently-asked-questions)
- [IBKR Client Portal API](https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/)
- [IBKR paper-trading limitations](https://www.ibkrguides.com/clientportal/aboutpapertradingaccounts.htm)
