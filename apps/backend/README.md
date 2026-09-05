# Backend configuration API

Backend is the administrative API for the single deployment owner, linked broker
accounts, strategy bindings/configuration, risk mandates and encrypted broker
credentials. Owner identity comes from the database designation. Callers cannot
select another user through a path, query, header or request field; historical
user/account IDs remain available for durable attribution.

The backend process runs inside the `application` service from
`vynmatrix/platform`, using `BACKEND_DATABASE_URL` for `vm_backend_login`.
Its loopback host endpoint defaults to `http://127.0.0.1:8081`. Requests use
`X-Admin-Key` with `BACKEND_ADMIN_API_KEY`; the platform launcher fixes
`BACKEND_ALLOW_ANON=false`. It also supplies the database secrets backend and
newest-first `SECRETS_MASTER_KEYS` ring. No credentials are returned by this API.
Use loopback or an owner-controlled SSH tunnel. A future owner UI can serve
static assets here without another container; public authentication is not supplied.

Owner designation is a maintenance operation. Follow
[the database guide](../../docs/DATABASE.md) and the shared `vmdev user` workflow;
this API cannot designate or silently adopt an owner.

| Surface | Routes |
|---|---|
| Profile | `GET /owner`, `PATCH /owner` |
| Accounts | `GET /broker-accounts`, `POST /broker-accounts` |
| Existing owner account adoption | `POST /broker-accounts/{account_id}/adopt` |
| Account edits | `PATCH /broker-accounts/{account_id}` |
| Complete credential replacement | `PUT /broker-accounts/{account_id}/credentials` |
| Bindings | `GET /bindings`, `POST /bindings`, `DELETE /bindings/{binding_id}` |
| Strategy configuration | `GET /strategy-configs`, `PUT`/`DELETE /strategy-configs/{strategy_id}` |
| Drawdown policy | `GET`/`PUT /risk-mandates/drawdown` |
| Official calendar coverage | `PUT /market-calendars/{code}` |

Accounts require an explicit stable `config_key`, canonical uppercase `base_ccy`
and exact broker/environment identity. Paper accounts require explicit initial
cash/equity and contain no exchange credentials. Profile/account patches provide
`expected` and `changes`; stale edits conflict. Used account financial identity
cannot be changed while execution is active or after financial activity.
Credential rotation replaces the complete document atomically. CLI and API use
the same [broker credential contracts](../../docs/BROKER_CREDENTIALS.md).

New bindings are inactive and have autopilot disabled. Enabling a binding does
not replace the strategy/version, owner/account, promotion or execution gates.
Management liveness remains available when owner configuration or trading
readiness is incomplete.

Optional calendar workers are selected with `PLATFORM_WORKERS=calendar-ibkr`,
`calendar-saxo` and/or `calendar-zerodha` within `workers` or the combined group.
They resolve exact instrument identity using the market-data role and submit
complete official coverage with the backend admin key. Only one writer may own
each instrument. Empty complete coverage represents an authoritative closure;
missing or stale non-crypto coverage blocks execution. Crypto remains continuous.
