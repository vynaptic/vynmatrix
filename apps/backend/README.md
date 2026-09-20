# Backend configuration API

Backend is the administrative API for the designated deployment owner, linked
broker accounts, strategy bindings/configuration, risk mandates, calendars, and
encrypted credentials. The owner comes from database designation; callers cannot
select another user through a path, query, header, or request body. Historical
user/account IDs remain for durable attribution.

The backend runs in the application group using BACKEND_DATABASE_URL as the
least-privilege backend login. Its loopback endpoint defaults to
http://127.0.0.1:8081 and requires X-Admin-Key with BACKEND_ADMIN_API_KEY.
BACKEND_ALLOW_ANON is false. It returns no plaintext credentials.

## Owner UI

The same process serves an owner UI at http://127.0.0.1:8081/ (it redirects to
/ui/): a Dashboard, a Strategies page, a Profit and loss page and a Settings
page. No new container, port, environment variable or dependency is involved.
The UI cannot place an order, hold a secret, or release a strategy.

- **Shell.** Plain HTML, CSS and ES modules in `backend/ui/`, with no build step
  and no third-party code. It is public because a browser navigation cannot send
  the admin header, and it contains no data. Its responses carry a same-origin
  Content-Security-Policy, so it uses no inline script or style.
- **Read API.** `/api/ui/*` sits behind the same X-Admin-Key dependency as every
  other route and resolves the owner on the server. The page asks once for the
  key, keeps it in the tab's sessionStorage, sends it only as that header and
  forgets it on Lock, on tab close or on any 401.
- **Control.** Three routes write. `POST /api/ui/bindings` binds a released
  strategy to one of the owner's connected accounts; `POST /api/ui/bindings/{id}`
  changes one that exists. Both take a mode -- `off`, `close_only` or `trading` --
  which is the subset of the four authority flags that satisfies every CHECK on
  the table, so the control cannot build a row the database rejects. A change is
  a partial patch fenced by `{expected, changes}`, confirmed in the browser
  before it is sent, and audited in the same transaction. Refusals are audited
  too, in a transaction of their own, because the rolled-back change carries
  nothing. Profile and account edits reuse `PATCH /owner` and
  `PATCH /broker-accounts/{id}` unchanged.
- **What the UI still cannot do.** Release, activate, deprecate or pull a
  strategy version; delete anything (the backend role holds `DELETE` on no
  table, so switching a binding off is the honest control); accept key material
  in any field; or change an account's broker, environment or `config_key`.
- **Data.** `ui_queries.py` selects explicit columns only, because migration
  `0107_backend_ui_read` grants the backend role column-level SELECT on exactly
  those columns with owner-scoped row policies. A test fails if a query reads an
  ungranted column or a grant goes unused. Equity, realized P&L and positions are
  projections recorded by the execution engine, shown with their as-of time in
  the account currency; nothing is recomputed and currencies are never added.
- **Extending.** A page is one module in `backend/ui/pages/`, one entry in the
  `PAGES` list in `backend/ui/app.js`, one route in `ui_api.py` and one read
  model in `ui_queries.py`.

Owner designation is maintenance-only; this API cannot silently adopt or change
the designated owner. See [DATABASE.md](../../docs/DATABASE.md) for that
workflow.

| Surface | Routes |
| --- | --- |
| Profile | GET /owner; PATCH /owner |
| Accounts | GET /broker-accounts; POST /broker-accounts |
| Existing account adoption | POST /broker-accounts/{account_id}/adopt |
| Account update | PATCH /broker-accounts/{account_id} |
| Credential replacement | PUT /broker-accounts/{account_id}/credentials |
| Bindings | GET /bindings; POST /bindings; DELETE /bindings/{binding_id} |
| Strategy configuration | GET /strategy-configs; PUT, DELETE /strategy-configs/{strategy_id} |
| Drawdown policy | GET, PUT /risk-mandates/drawdown |
| Market-calendar coverage | PUT /market-calendars/{code} |
| Owner UI shell (public, static) | GET /; GET /ui/* |
| Owner UI read API | GET /api/ui/overview; /api/ui/strategies; /api/ui/pnl?days=; /api/ui/fills?limit=&before=; /api/ui/version; /api/ui/control |
| Owner UI control | POST /api/ui/bindings; POST /api/ui/bindings/{binding_id} |

Accounts require stable config_key, canonical uppercase base currency, and exact
broker/environment identity. Profile/account updates use expected and changes
values; stale writes conflict. A financially active account cannot change its
financial identity. Credential replacement is complete and atomic.

New bindings are inactive with autopilot disabled. Enabling one does not replace
strategy/version, account, promotion, market-session, or execution gates.
[BROKER_CREDENTIALS.md](../../docs/BROKER_CREDENTIALS.md) defines credential
documents; [CONFIGURATION.md](../../docs/CONFIGURATION.md) defines worker
selection and private listeners.
