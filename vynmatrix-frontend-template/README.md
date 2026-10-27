# Vynmatrix frontend

Standalone Next.js owner UI for the existing backend. The supported pages read
recorded backend data: Overview, Strategies, Profit & Loss, Settings, Activity,
Market Data, ML Lab, New Strategy and Paper Trading.
The backend continues to enforce ownership, releases, execution modes, and risk.

## Run locally

Use Node.js 24+ and npm. Start the backend through the repository's supported
[setup](../SETUP.md), keeping execution in paper mode and the live gate disabled.
Then run from this directory:

```bash
npm ci --ignore-scripts
cp .env.example .env.local
npm run dev
```

Open [localhost:3000](http://localhost:3000). Unlock with this installation's
`BACKEND_ADMIN_API_KEY`. The key stays in tab session storage, with an in-memory
fallback if storage is disabled. Lock or a rejected key clears authentication.

`NEXT_PUBLIC_API_BASE_URL=/api` uses the Next.js server rewrite to
`http://127.0.0.1:8081/api`. Login and page reads use the same configured base.
Never place credentials in `NEXT_PUBLIC_*` variables. This standalone frontend
is not installed or served by the backend's current deployment workflow.

## Data and status

- Amounts retain their recorded currencies. Totals group by currency without FX
  assumptions and preserve decimal precision. Missing data displays Unavailable.
- Position average prices and marks lack quote-currency identity in this API;
  the UI says so. Position notionals retain their supplied notional currency.
- Off, Close only, Trading, and Custom remain distinct binding modes.
- The connectivity indicator reports the last API request. It does not certify
  database or worker health. Execution context comes from backend safety facts.
- Market Data validates CSV imports into retained receipts, freezes immutable
  datasets, browses recorded feeds and exports exact stored data. CSV columns
  are `ts,open,high,low,close,volume`; optional `bar_open,bar_close` must appear
  together. Supply the actual instrument, source, provenance, quote currency,
  timeframe, timestamp semantics and adjustment policy. Eligibility refusals
  remain visible; an imported dataset does not modify runtime market data.
- ML Lab trains a real L2 return regression on a frozen dataset. It uses causal
  OHLCV features, chronological train/validation/test splits with purge and
  embargo, at least 1,000 usable observations and 200 test observations. Inspect
  forecast metrics against both baselines, register a successful model, download
  finite JSON and run compatible recorded-data predictions after its cutoff.
- New Strategy saves immutable Python/config revisions, validates canonical
  `PureSignalStrategy` implementations and runs historical backtests through the
  canonical engine. Select an optional registered model with the model template.
  The initial simulator supports funded long-only spot crypto, cash equities
  and ETFs in their recorded quote currency. Costs and terminal exposure remain
  explicit; research success does not release a strategy for runtime execution.
- Research jobs require the [host supervisor and combined runtime](../docs/DEPLOYMENT.md#supported-topology).
  They retain frozen inputs, lineage, results and failed attempts. Cancellation
  stops the exact sandbox; retry creates a new attempt. Page refreshes preserve
  jobs and revisions. A stopped supervisor leaves queued jobs visibly pending.
- Paper Trading creates local paper accounts and shows recorded balances,
  positions, fills and binding readiness. Confirmed expected-value changes use
  existing release, overlap and risk gates. Off does not liquidate positions;
  Close only permits exits. There is no direct order or account-reset action.
- Strategy search filters the recorded catalogue. Page search supports Ctrl/Cmd+K;
  Settings section links expose account, binding, and risk controls.
- P&L period controls select the daily equity history window. Equity shows its
  recorded observation time; realized P&L is labeled cumulative separately.

## Validation

```bash
npm test
npm run lint
npm run build
```

Tests render the React pages with deterministic API-boundary fixtures and cover
contracts, unavailable sections, mixed currencies, binding modes, authentication,
and hydration. They are unit/component checks. Historical paper-pipeline
acceptance follows the separate [verification guide](../docs/E2E_VERIFICATION_GUIDE.md).
The required CI gate includes frontend tests, lint, and the production build.
