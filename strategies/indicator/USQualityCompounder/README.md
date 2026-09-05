# US Quality Compounder

USQualityCompounder is a disabled, quarterly, long-only US equity research
strategy. It ranks point-in-time S&P 500 share classes using four reproducible
factors and produces an account-neutral target book. It does not provide stock
tips, a paper-promotion result, broker authority, or live-trading authority.

Current runtime status is maintained in
[Strategy Readiness](../../../docs/STRATEGY_READINESS.md). The source
configuration remains disabled in development.

## Status

| Item | Current state |
| --- | --- |
| Strategy ID / candidate version | us_quality_compounder_v1 / 0.2.0 |
| Universe / benchmark | Point-in-time S&P 500 / SPY total return |
| Cadence | Final official XNYS session of each calendar quarter |
| Portfolio policy | Target 15 stocks; 1–25 allowed; unused capacity is cash |
| Runtime | Disabled signal worker |
| Evidence | Recorded-data diagnostic and v0.2 smoke backtest completed; below the paper-promotion gate |

Version 0.1.0 remains deprecated immutable lineage. Version 0.2.0 is a
candidate needed by its factor and panel evidence; the catalogue row, source
configuration, and account bindings remain inactive.

## Recorded-data diagnostic

A replacement EODHD snapshot was admitted only for historical-validation
diagnostics. Sequential experiments retained v0.2 only after their
pre-declared coverage and correlation checks:

| Experiment | Isolated change | Result |
| --- | --- | --- |
| EXP-00 | Leave-one-component-out diagnosis | Filing drift alone could not repair factor coverage. |
| EXP-01A | Operating profitability replaces gross profitability in quality | Quality coverage improved, but whole-model coverage remained insufficient. |
| EXP-01B | Comparable revenue plus issuer-aware fundamental growth | Whole-model factor-complete coverage reached 61.7–64.4% with no named-sector coverage failure. |

The v0.2 one-year comparison produced 11.45% total return versus 8.48% for
v0.1 and 18.99% for SPY, with 9.93% maximum drawdown and 1.92x annual
turnover. It covers five decisions and 24 closed trades, has diagnostic-only
source evidence, and remains below the provisional 80% overall/70%
material-sector coverage gate. It cannot establish robustness or justify
activation.

## Model

Only evidence reconstructable prospectively is enabled. Missing evidence stays
missing; it is not neutralized or imputed.

| Factor | Weight | Definition |
| --- | ---: | --- |
| Quality | 35% | Issuer-type profitability, cash return/conversion, accrual quality, and balance-sheet safety |
| Fundamental growth | 30% | Comparable revenue plus issuer-appropriate operating/net-income/cash-flow growth |
| Valuation | 20% | Issuer-type cash-flow/operating-income yield and book-to-market |
| Momentum | 15% | 126- and 252-session total returns ending 21 sessions before the decision |

~~~text
score = 0.35 quality + 0.30 fundamental growth + 0.20 valuation + 0.15 momentum
~~~

The ranker uses median/MAD normalization, a population-standard-deviation
fallback, winsorization at plus/minus three, and a minimum peer count. It does
not redistribute weights when evidence is missing. Market, sector, and industry
gates are independent, so a name with missing essential evidence or a closed
market gate remains cash.

## Portfolio and data rules

| Rule | Current policy |
| --- | --- |
| Target holdings / normal weight | 15 / 6.67% |
| Small-cap / sector / industry caps | 5% / 25% / 15% |
| Aggregate mid/small caps | 40% / 20% |
| Minimum market capitalization | USD 300 million |
| Minimum reference price / liquidity | USD 5 / USD 50 million median 126-session dollar volume |
| Entry / hold | Top 10% / top 20%, subject to factor and group gates |
| Maximum panel age | 100 days |

Membership, security identity, prices, adjustments, official sessions,
fundamentals, market capitalization, broker mapping, and account currency each
have an explicit source boundary. Missing or ambiguous evidence blocks a member
or the full panel as appropriate. Broker symbols are never inferred, and an
account must have exact settled-currency/FX authority before an entry.

The writer obtains provider evidence before its database transaction, then
persists immutable observations, factors, manifest, panel, rank, state, and
signal/rebalance lineage atomically. A changed input or replay either creates
the exact permitted result or rolls back; it does not partially refresh a panel.

## Activation blockers

Before paper activation, all of the following remain required:

- Raise factor-complete coverage above the registered overall and sector gates
  without weakening them or tuning against the smoke result.
- Confirm membership and permanent-security identity evidence, then register a
  complete reviewed catalogue and current official XNYS artifact.
- Produce a real PostgreSQL panel at the intended quarter-end boundary using
  exact provider/entitlement authority.
- Supply an explicitly mapped paper account, settled funding/currency policy,
  exact costs, instrument routes, and clean reconciliation.
- Implement and certify an account-scoped IBKR paper fill/fee route satisfying
  the complete exact-fill contract.
- Complete the separate paper E2E restart, idempotency, partial-fill, and
  reconciliation proof.

The registered quarter-end job is default-off. When its prerequisites exist, run
it through scripts.run_platform inside the existing worker or combined
application group; do not use a retired Compose profile or create a new
container. Configuration and evidence are owned by
[CONFIGURATION.md](../../../docs/CONFIGURATION.md) and
[E2E_VERIFICATION_GUIDE.md](../../../docs/E2E_VERIFICATION_GUIDE.md).

## References

- [EODHD historical data](https://eodhd.com/financial-apis/api-for-historical-data-and-volumes)
- [EODHD index constituents](https://eodhd.com/financial-apis/stock-market-indices-api)
- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [IBKR Client Portal API](https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/)
