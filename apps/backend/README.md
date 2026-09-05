# Backend configuration API

This FastAPI service is the administrative write surface for user strategy
bindings, linked broker accounts, and encrypted broker credentials. Credential
material is never returned by the API. Each database unit of work applies the
selected tenant scope, with PostgreSQL RLS as the persistence backstop.

Authentication currently uses `BACKEND_ADMIN_API_KEY` for a trusted operator
or web BFF. The `user_id` path value is selected by that trusted caller; it is
not derived from a verified end-user identity token. Do not expose this service
as a direct end-user API until an IdP and token-to-user mapping are implemented.

The declared local Compose service uses the `vm_backend_login` database role
and reuses the scoring-engine image. No cloud deployment is included. Configure
`BACKEND_ADMIN_API_KEY` and keep `BACKEND_ALLOW_ANON=false` for local setup.

New strategy bindings are inactive with autopilot disabled unless the trusted
operator explicitly sets both controls. Broker-account onboarding requires a
canonical uppercase `base_ccy`; the API never guesses a tenant's reporting
currency. Broker secrets are submitted in the request's nested `credentials`
object under the exact per-broker contract documented in
[the broker credential reference](../../docs/BROKER_CREDENTIALS.md). Credential rotation is a full-document atomic
replacement through
`PUT /users/{user_id}/broker-accounts/{account_id}/credentials`; partial OAuth
token updates are rejected.

The same trusted operator boundary exposes
`PUT /market-calendars/{code}` for complete official broker/exchange session
coverage. A replacement declares its HTTPS source, observation time, bounded
coverage interval, regular-session windows, and exact instrument assignments in
one transaction. Empty complete coverage represents an authoritative closed
interval. Crypto remains explicitly continuous and cannot be assigned a
scheduled calendar; non-crypto execution fails closed when fresh coverage is
missing.

Optional official schedule writers run from the market-data image under the
`calendar-ibkr`, `calendar-saxo`, and `calendar-zerodha` Compose profiles. They
read canonical-to-venue identity with the least-privilege market-data role and
submit normalized provider observations here using the same admin key; they do
not write calendar tables directly.
