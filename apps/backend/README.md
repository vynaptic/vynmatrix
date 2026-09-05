# Backend configuration API

Backend is the administrative API for the designated deployment owner, linked
broker accounts, strategy bindings/configuration, risk mandates, calendars, and
encrypted credentials. The owner comes from database designation; callers cannot
select another user through a path, query, header, or request body. Historical
user/account IDs remain for durable attribution.

The backend runs in the application group using BACKEND_DATABASE_URL as the
least-privilege backend login. Its loopback endpoint defaults to
http://127.0.0.1:8081 and requires X-Admin-Key with BACKEND_ADMIN_API_KEY.
BACKEND_ALLOW_ANON is false. It returns no plaintext credentials. A future
owner UI may serve static assets from this group without a new container.

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

Accounts require stable config_key, canonical uppercase base currency, and exact
broker/environment identity. Profile/account updates use expected and changes
values; stale writes conflict. A financially active account cannot change its
financial identity. Credential replacement is complete and atomic.

New bindings are inactive with autopilot disabled. Enabling one does not replace
strategy/version, account, promotion, market-session, or execution gates.
[BROKER_CREDENTIALS.md](../../docs/BROKER_CREDENTIALS.md) defines credential
documents; [CONFIGURATION.md](../../docs/CONFIGURATION.md) defines worker
selection and private listeners.
