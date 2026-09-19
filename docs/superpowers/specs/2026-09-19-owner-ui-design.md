# Owner UI: dashboard, strategies and P&L served by the backend

Status: accepted for implementation (2026-09-19). Scope: a read-only web UI for the
single deployment owner, delivered in three phases (Dashboard, Strategies, P&L).

## 1. Goal and non-goals

Give a non-technical owner one local page that answers four questions without a
terminal: is the platform safe and running, which strategies exist and which are
bound to my account, how is my paper account doing, and what just happened.

Non-goals for this spec: any write or trading control (bindings, halts, live
arming stay in the existing admin API and CLI), multi-user access, a public or
remote deployment, per-strategy backtest views, and operator-grade pipeline
internals (outbox, runtime journal, supervisor metrics).

## 2. Evidence that shapes the design

| Fact | Source |
| --- | --- |
| The backend is the only published application port, bound to `127.0.0.1:${BACKEND_PORT:-8081}` | [docker-compose.stack.yml](../../../docker/docker-compose.stack.yml) |
| The image copies `apps/backend` as a source tree, so non-Python files under it ship with no Dockerfile change | [platform_runtime.Dockerfile](../../../docker/platform_runtime.Dockerfile) |
| One image, two Dockerfiles and at most three running containers are contract-tested | `tests/test_platform_container_contract.py`, [DEPLOYMENT.md](../../DEPLOYMENT.md) |
| Every backend route requires `X-Admin-Key`; the launcher forces `BACKEND_ALLOW_ANON=false` | [api.py](../../../apps/backend/backend/api.py), [platform_processes.py](../../../scripts/platform_processes.py) |
| `vm_backend` cannot read signals, fills, positions, NAV, metrics or prices today | migrations `0052`, `0094`, `0102` |
| Realized P&L is computed by the execution engine; `execution_metrics`, `positions` and `daily_nav` are account-currency projections; never SUM cumulative `realized_pnl` rows | [soak_report.py](../../../libs/python/lib_application/lib_application/services/soak_report.py) `_pnl_sections` |
| Third-party code needs licence preservation and a NOTICE provenance entry | [LICENSE](../../../LICENSE), [NOTICE](../../../NOTICE) |
| `apps/backend/README.md` already anticipates "a future owner UI may serve static assets from this group without a new container" | [apps/backend/README.md](../../../apps/backend/README.md) |

## 3. Approaches considered

1. **No-build static UI served by the existing backend (chosen).** Plain HTML, CSS
   and ES modules under `apps/backend/backend/ui/`, mounted at `/ui/`, calling a
   read-only JSON API under `/api/ui/`. No Node, no bundler, no new container,
   image, port, environment variable or Python dependency. A user upgrades with the
   same image rebuild they already run.
2. **React/Vite SPA behind its own web container.** Richer ecosystem, but it adds a
   Node toolchain, a supply chain that needs provenance records, a fourth container
   against the three-container contract, and CORS. Rejected as over-engineered for a
   single-owner local tool.
3. **Server-rendered templates or a packaged dashboard (Jinja/HTMX, Streamlit,
   Grafana).** Each adds a dependency or container and puts presentation inside
   Python or outside the repository's control. Rejected.

## 4. Architecture

```
browser ── GET /ui/* (static shell, no data, strict CSP) ──┐
        └─ GET /api/ui/* + X-Admin-Key ──> backend (8081) ─┴─> PostgreSQL as vm_backend_login
```

Backend (all in `apps/backend/backend/`, imports limited to the declared libs):

- `ui_api.py` — `register_ui(app, *, session_factory, require_admin)`, called once
  from `create_app` beside `_register_market_calendar_route`. It adds an
  `APIRouter(prefix="/api/ui", dependencies=[Depends(require_admin)])`, mounts
  `StaticFiles(directory=Path(__file__).parent / "ui", html=True)` at `/ui`, sets a
  same-origin Content-Security-Policy on `/ui` responses only, and redirects `/` to
  `/ui/`. `api.py` grows by one import and one call.
- `ui_queries.py` — pure read functions `(session, owner_id, ...) -> dict`. Every
  query selects explicit columns, is bounded by `LIMIT` or an indexed aggregate, and
  runs inside `_owner_session`, which resolves the deployment owner server-side and
  sets the transaction-local RLS tenant. No caller-supplied user id is accepted.

Frontend (`apps/backend/backend/ui/`): `index.html`, `app.css`, `app.js` (shell,
hash router, page registry, refresh), `api.js` (fetch wrapper, key handling),
`format.js`, `charts.js` (hand-written SVG; no third-party code), and one module per
page under `pages/`. Adding a page means one file in `pages/`, one line in the
registry, and one route in `ui_api.py`.

## 5. Read API

All responses are JSON, read-only and carry `as_of` (UTC). Money is a decimal
string with an explicit `currency`; values of different currencies are never added.
A section that cannot be computed is `null` with a `reason`, never a guessed zero.

| Route | Returns |
| --- | --- |
| `GET /api/ui/overview` | safety block (`execution_mode`, `allow_live`, `environment`), owner display name and base currency, per-account KPIs (equity with as-of and source, realized P&L, open positions, fills), strategy counts, freshness (`last_price_at`, `last_signal_at`, `last_fill_at`), and the latest 10 signals and fills merged as activity |
| `GET /api/ui/strategies` | catalogue rows: id, name, asset class, description, latest version and status, the owner's binding (active, account, entries, exits, autopilot) or `null`, last signal (time, action, symbol), realized P&L for that strategy |
| `GET /api/ui/pnl?days=90` | per account: equity series from `daily_nav`, equity-by-trade series and realized P&L by strategy and symbol from `execution_metrics` (latest row per partition, `blocked` excluded), open positions, fee totals by currency |
| `GET /api/ui/fills?limit=50&before=<exec_id>` | fills blotter joined to orders, intents and instruments, newest first, keyset-paginated |

Never returned: credential references (`external_ref`, `config_key`), secret or
credential tables, `order_intents.payload`, `client_order_id`, `broker_order_ref`,
signal `features`/`signal_meta`, email addresses.

## 6. Database access (migration `0107_backend_ui_read`)

`vm_backend` gains column-level `SELECT` only, on exactly the columns section 5
reads, plus owner-scoped `FOR SELECT TO vm_backend` policies on the RLS tables in
the style of `0102`:

| Table | Columns | Policy predicate |
| --- | --- | --- |
| `daily_nav` | user_id, account_id, date, nav_ccy, nav_value, drawdown | direct owner |
| `execution_metrics` | user_id, account_id, strategy_id, symbol, execution_mode, equity, available_cash, unrealized_pnl, realized_pnl, orders_filled, total_commission, commission_currency, created_at | direct owner |
| `order_intents` | intent_id, user_id, account_id, strategy_id, side | direct owner |
| `orders` | order_id, intent_id, account_id, settlement_currency, state | owner account |
| `positions` | account_id, instr_id, qty, avg_price, last_mark, gross_notional, notional_currency, updated_at | owner account |
| `executions` | exec_id, order_id, instr_id, fill_ts, qty, price, fee_ccy, fee_amount, venue | order of an owner account |
| `canonical_signals` | signal_id, strategy_id, instr_id, action, confidence, ts | none (no RLS; single-owner pipeline rows) |
| `prices` | instr_id, ts, close, timeframe, source | none (shared reference data) |

Direct owner means `user_id = public.vm_deployment_owner_id() AND user_id =
current_setting('app.current_tenant', true)`. No INSERT, UPDATE, DELETE, sequence or
default grant is added, the downgrade is symmetric, and the migration is a no-op
outside PostgreSQL. A statement-recording contract test and new rows in the
service-role integration expectations cover it; `docs/DATABASE.md` records the
wider read surface.

## 7. Authentication and browser security

The static shell is public because a navigation cannot carry a header; it contains
no data. On load the UI calls `/api/ui/overview`. A `401` shows a one-time prompt
for the admin key, which is kept in `sessionStorage` for the tab, sent only as
`X-Admin-Key`, never placed in a URL, and cleared by **Lock** or by any `401`. In
the documented anonymous development mode the same call succeeds and no prompt is
shown. `/ui` responses carry `Content-Security-Policy: default-src 'self';
script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self';
base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, so the UI uses no
inline script or style. The loopback binding is unchanged.

## 8. Pages

Shell: left navigation (Dashboard, Strategies, P&L) that collapses to a top bar on
narrow screens, a permanent **PAPER · live orders disabled** badge, last-refreshed
time with a manual refresh, light and dark themes following the system, and
automatic refresh every 30 seconds while the tab is visible.

- **Dashboard (phase 1).** KPI tiles per account (equity, realized P&L, open
  positions, active strategies), three freshness tiles in plain language (market
  data, signals, trades; only market data is graded fresh/delayed/stale, because
  quiet strategies are normal), an equity sparkline, and recent activity.
- **Strategies (phase 2).** One row per catalogue strategy with status, binding
  state, last signal and realized P&L; a row expands to its description and the
  binding's entry/exit/autopilot switches, read-only.
- **P&L (phase 3).** Equity curve (daily, falling back to equity by trade when fewer
  than two daily points exist), realized P&L by strategy and symbol as bars, open
  positions, fees, and the fills blotter with "load more".

Every empty or unavailable state says what is missing and what makes it appear.
Projections are labelled "recorded by the execution engine" with their as-of time.

## 9. Error handling

The API isolates each overview section: a failing query yields `null` with a reason
for that section and the page still renders. The frontend shows one inline banner
for network failures and keeps the last good data visible with its age.

## 10. Testing and acceptance

- Unit (SQLite): auth on every route (401 without key, 200 with, anonymous dev
  mode), owner isolation against a second user's rows, latest-per-partition P&L
  never double counts, no cross-currency sums, forbidden fields absent, keyset
  pagination, static shell served with the CSP, `/` redirect, no external URL in
  any asset.
- Migration contract test and PostgreSQL service-role expectations.
- Container: rebuild `vynmatrix/platform`, apply the migration through
  `vmdev db bootstrap`, start the declared services, confirm the shell and login
  prompt in a browser and the authenticated API against the recorded paper data.
  Rendered authenticated pages are checked on a host development server in
  anonymous mode against the same database, because an assistant must not type a
  secret into a browser field.
- Acceptance: `vmdev audit --strict`, the backend and affected suites, and CI.

## 11. Documentation

`apps/backend/README.md` owns the routes and the UI paragraph; `SETUP.md` and
`docs/QUICK_REFERENCE.md` gain an "Open the dashboard" step; `CHANGELOG.md` gains an
`Added` entry. No new top-level document.
