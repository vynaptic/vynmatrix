# Broker Credentials

This is an inherited integration reference, not a request to obtain trading
permissions or connect a personal account. Default local paper verification uses
public market data and the local paper broker. No credentials or certifications
are transferred with vynmatrix; keep live execution disabled. Provider procedures
must be checked against official documentation before separately authorized use.

How to obtain and configure exchange/broker API credentials. This is the single
source of truth for account-scoped trading credentials and the separate shared
market-data credential paths.

---

## The two credential boundaries

Shared market-data access and the designated owner’s account trading access are independent:

| Path | What it does | Where creds live | Permission needed |
|------|--------------|------------------|-------------------|
| **Market data** | Fetch candles for isolated warmup/backfill and live polling | Dedicated platform feed env vars (or an authenticated IBKR gateway); public venues need none | **View / market-data entitlement** |
| **Trading** | Place and track orders for one exact owner account | Per-broker-account **JSON secret** referenced by `broker_credentials.secret_ref` | **Trade** |

For Coinbase, both paths use the **Coinbase Advanced Trade API** with **CDP
keys** (JWT authentication, not the legacy HMAC key/secret/passphrase). Prefer
separate least-privilege keys. Zerodha uses
`ZERODHA_MARKET_DATA_API_KEY`/`ZERODHA_MARKET_DATA_ACCESS_TOKEN`; Saxo uses
`SAXO_MARKET_DATA_ACCESS_TOKEN`, its RFC3339 expiry, and optional AccountKey.
IBKR uses its authenticated Client Portal Gateway. Deribit and Delta candle
feeds are public. The one-shot warmup uses the same selected source, exact
broker catalogue identity, provider, and credentials as live ingestion; it
never falls back to Coinbase or relabels source provenance.

> **Paper vs live.** Coinbase's Advanced Trade **sandbox** (`api-sandbox.coinbase.com`)
> returns static/mocked responses — it is a connectivity smoke test only, **not**
> a paper-trading venue. For realistic **paper trading**, use the platform's
> built-in paper broker (`EXECUTION_USE_LOCAL_PAPER_BROKER=true`), which simulates
> fills against **real** Coinbase market data. Use real Coinbase only for **live**.

---

## 1. Create a Coinbase CDP API key

1. Sign in to Coinbase, then open the **Coinbase Developer Platform**:
   <https://portal.cdp.coinbase.com>.
2. Create (or select) a **project**.
3. Go to **API keys → Create API key** and choose a **Secret API key** for the
   **Advanced Trade / Brokerage** API.
4. Configure it:
   - **Name** — e.g. `vynmatrix-market-data` or `vynmatrix-trading`.
   - **Permissions** — `View` for market-data-only; add `Trade` for live trading.
     Leave **Transfer** OFF (the platform never moves funds).
   - **IP allowlist** — for a separately authorized hosted integration, restrict to its verified egress IP.
5. Click **Create**. Coinbase shows two values **once**:
   - **Key name / Key ID** → `COINBASE_API_KEY`. Either `organizations/<org-id>/apiKeys/<key-id>`
     (legacy ECDSA) or a bare UUID (newer CDP key). Use it **exactly as shown**.
   - **Secret** → `COINBASE_API_SECRET`. Either an **EC private-key PEM** (ECDSA) or a
     **base64 string** (Ed25519). **Copy/download it now** — it is not shown again.

The platform auto-detects the key type — **ECDSA** keys are signed as `ES256`,
**Ed25519** keys as `EdDSA` — so either works with no extra configuration.

---

## 2. Configure — local development

Put the values in your **`.env`** (gitignored — never commit secrets):

```dotenv
# Key name/id exactly as Coinbase shows it (UUID for a newer CDP key):
COINBASE_API_KEY=00000000-0000-0000-0000-000000000000

# Ed25519 key: the base64 secret, single line, as-is:
COINBASE_API_SECRET=<base64-ed25519-secret>

# --- OR, for an ECDSA key, the EC PEM with newlines escaped as \n ---
# COINBASE_API_KEY=organizations/abcd-1234/apiKeys/efgh-5678
# COINBASE_API_SECRET=<EC PEM begin>\nMHcCAQEE...\n<EC PEM end>\n
```

For ECDSA, the normalizer (`normalize_coinbase_private_key`) converts the `\n`
back to real newlines at load time. Ed25519 secrets are plain base64 — no
escaping needed.

The platform launcher forwards these optional Coinbase feed credentials only
to the selected market-data process. Execution resolves account-scoped encrypted
credentials separately; it does not inherit shared feed keys. Public Coinbase
candles do not require a trading key.

Select `market-data` in `PLATFORM_WORKERS` and configure exact `INGESTOR_SYMBOLS`
after reviewing the source catalogue. Start or restart through the owner/database
lifecycle in [DATABASE.md](DATABASE.md). Inspect the existing worker group
(`application` instead of `workers` for the combined layout):

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml logs --tail 100 workers
```

Check component readiness for fresh source observations; absence of an auth
error alone does not establish complete or timely data.

---

## 3. Configure — future hosted environments

No hosted runtime is provisioned. The private single-host baseline uses the
same platform image and two/three-container layouts as local setup; backend
binds loopback and can be reached through an owner-controlled SSH tunnel.
[DEPLOYMENT.md](DEPLOYMENT.md) defines that boundary. Inject secret values only
into the consuming process through the declared allowlists. Do not reuse an
inherited operator environment or copy trading credentials into feed settings.

---

## 4. Configure — trading (per broker account)

Live trading creds are **per linked broker account**, not global. Each
`linked_broker_accounts` row has a `broker_credentials` record whose `secret_ref`
is a **pointer** (never the key) to a secret holding **JSON**:

```json
{
  "api_key": "organizations/abcd-1234/apiKeys/efgh-5678",
  "api_secret": "-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----\n"
}
```

### Broker-specific credential fields

The backend accepts credentials only inside the onboarding request's
`credentials` object and validates an exact allowlist for the selected broker.
Broker-specific fields live in one encrypted JSON document so one write replaces
one complete credential version. Never split one account's token lifecycle
across multiple secrets or add unowned fields to the document.

- Coinbase requires `api_key` and `api_secret`; `passphrase` is accepted only
  for an account whose Coinbase credential format uses it.
- Deribit requires `api_key` and `api_secret`; `subaccount` is optional.
- Every linked `paper` account requires an empty credential object because
  normal owner paper execution always uses the deterministic in-process
  broker. Its explicit initial equity and cash belong to the linked account,
  not a secret. Exchange sandboxes use isolated certification workflows rather
  than owner credential state.

Credential validity does not imply live certification. Live routing also
requires an order-scoped broker trade resource that supplies the stable venue
trade ID, actual fill time, quantity, price, fee amount, and fee currency
without inference. Coinbase currently has that complete persistence boundary.
Deribit's exact trade reader is implemented but remains blocked until its own
authenticated certification workflow is proven. IBKR, Saxo, Zerodha, and Delta
remain explicitly blocked where the currently integrated official contract
cannot supply or safely scope every required field. See
[Production Runbook](RUNBOOK.md#broker-certification-order).

For IBKR Client Portal specifically, the official
[`/iserver/account/trades`](https://ibkrcampus.com/campus/ibkr-api-page/cpapi-v1/#trades)
response documents execution ID, execution time, quantity, price, and commission,
but not commission currency. The platform must not infer that currency from the
instrument, account base currency, or another transaction endpoint. Native IBKR
paper execution therefore remains blocked until an authorized route exposes the
complete exact-fill contract; TWS/IB Gateway commission reports are a distinct
route and require an explicit operator decision and implementation.

Delta requires an explicit regional host selection. Use exactly `global` or
`india`; the adapter never falls back between regions:

```json
{
  "api_key": "<Delta API key>",
  "api_secret": "<Delta API secret>",
  "region": "india"
}
```

IBKR Client Portal is session-authenticated, so it does not use invented API
key fields. The credential stores one exact IBKR account and one owned gateway:

```json
{
  "subaccount": "U1234567",
  "gateway_url": "https://localhost:5000",
  "ca_cert": "/run/secrets/ibkr-gateway-ca.pem"
}
```

`subaccount` and `gateway_url` are always required. Live connections also
require `ca_cert`; the adapter never selects the first account returned by the
gateway, requires that `/iserver/accounts` explicitly lists the configured
account, and never disables TLS verification for live execution. The account
owner must complete IBKR's interactive Client Portal authentication and
reauthentication outside the execution process. IBKR documents the retail Client
Portal Gateway as a local-machine process and warns that operating it on a
different machine from the API client is unsupported. A remote-host route must therefore not be assumed from a successful workstation
session. The gateway is an explicit dependency with its own authentication
lifecycle. If separately approved as a container, it uses the third slot and the
platform must use the combined `all` group. An external owner-operated gateway
also requires an explicit URL and TLS configuration; the stack provisions neither.

IBKR execution resolves a positive-integer conid from the selected account's
`instrument_broker_symbols.broker_instrument_id`; symbol search and first-result
selection are forbidden. The adapter calls the documented `/tickle` boundary
before order submission after the cached health window, and an expired,
disconnected, or unauthenticated brokerage session fails closed. Every conid must come from reviewed IBKR reference data; a historical fixture
or source catalogue row does not grant account trading authority.

Zerodha requires the application pair plus the current daily Kite session:

```json
{
  "api_key": "<Kite API key>",
  "api_secret": "<Kite API secret>",
  "access_token": "<current daily access token>",
  "access_token_expires_at": "<timezone-aware RFC3339 expiry>"
}
```

The control plane atomically replaces that full document after the account
owner completes Kite login. The adapter rejects missing, naive, or expired
timestamps before opening a network connection and never revokes the external
daily token during process shutdown.

Saxo uses an OAuth authorization-code application and one concrete Saxo
account. SIM and LIVE have separate AppKeys, AppSecrets, authorization hosts,
tokens, and REST endpoints:

```json
{
  "api_key": "<Saxo AppKey>",
  "api_secret": "<Saxo AppSecret>",
  "access_token": "<current access token>",
  "access_token_expires_at": "<timezone-aware RFC3339 access-token expiry>",
  "refresh_token": "<current refresh token>",
  "refresh_token_expires_at": "<timezone-aware RFC3339 refresh-token expiry>",
  "account_key": "<exact Saxo AccountKey>",
  "client_key": "<exact Saxo ClientKey>"
}
```

Both expiry values must be timezone-aware RFC3339 timestamps. Saxo returns a
new refresh token on every refresh and invalidates the old one. The component
that calls Saxo's token endpoint must therefore atomically replace the entire
encrypted JSON document—including both new tokens and expiries—before the
current access token approaches expiry. The execution adapter never performs
the refresh-token exchange itself; it reloads the atomically persisted document
and refuses all broker calls when no safe replacement is available. This
prevents a process crash from silently losing Saxo's rotated refresh token.

The credential account identity is also verified against
`GET /port/v1/accounts/{AccountKey}` at connection time. Each order separately
resolves the selected linked account's broker mapping from
`instrument_broker_symbols`. Saxo requires a canonical positive-integer UIC in
`broker_instrument_id` and the exact OpenAPI AssetType in
`broker_instrument_type`; option orders additionally require an explicit
open/close intent. These are instrument-catalogue facts, not credential
defaults, and execution blocks before broker connection when the mapping is
absent. Every instrument must be onboarded from authoritative Saxo reference data;
a historical fixture mapping does not grant account trading authority. Outstanding pre-trade disclaimers
block automated placement, and the adapter never accepts them on a user's
behalf.

Create a linked account with
`POST /broker-accounts`. Rotate any credential with
`PUT /broker-accounts/{account_id}/credentials`; rotation is a
complete replacement, never a partial patch. Both endpoints require the backend
operator key and return only the account/secret reference and expiry metadata,
never plaintext.

The API resolves the designated owner; caller-provided `user_id` selectors are
rejected. Accounts require a stable owner-relative `config_key`; retrying the same
key preserves the account identity. The CLI and API share validation and atomic
credential writes. Owner designation and account adoption are explicit operations
described in [DATABASE.md](DATABASE.md).

Each linked account also has a required `base_ccy`. A strategy binding selects
one concrete `broker_account_id`; that account identity and currency remain
attached through decision, order, fill, position, P&L, and feedback. Execution
normalizes broker cash and positions to that currency only from fresh persisted
ECB/Coinbase observations. The onboarding API requires a canonical uppercase
currency code; it does not default an omitted account currency to USD. Do not
encode account selection or an FX-parity assumption in credential JSON.

Paper accounts additionally require explicit `paper_initial_equity` and
`paper_initial_cash`. The database rejects unconfigured paper accounts and
rejects local-paper capital on live accounts. Runtime paper state replays the
exact account-, broker-, and environment-scoped canonical fills with their
persisted FX observations. It repairs `positions` after a successful replay;
`positions` and `execution_metrics` are projections, not restart accounting
inputs. Missing provenance blocks the route instead of inventing a balance,
price, currency, or conversion. Linear futures and perpetual paper orders must
carry an exact positive contract multiplier and leverage plus the contract type.
Those terms and the fill-time FX provenance are retained on the canonical
order/fill ledger, so restart replay rebuilds contract quantities, variation
P&L, cash, equity, and gross-notional/leverage margin without applying spot cash
semantics. Missing or inconsistent terms and non-linear (inverse or quanto)
contracts fail closed. The generic paper model does not invent exchange-specific
funding charges or liquidation prices.

The `secret_ref` is resolved at execution time through the encrypted database
secrets provider. In the composed runtime the launcher fixes `SECRETS_BACKEND=db`
and gives `SECRETS_MASTER_KEYS` only to backend and execution child processes.
The key ring is mandatory at application startup; no fallback to global broker
environment credentials is used. Other provider implementations remain library
facilities, not alternative Compose account stores.

### Encrypted account storage

`DbSecretsProvider` stores each account’s JSON encrypted in `managed_secrets`,
keyed by an account-owned reference. `SECRETS_MASTER_KEYS` is a newest-first
Fernet key ring: writes use the first key and reads accept every configured key.
Keep its recovery material separate from database backups. No key is supplied
in [`.env.example`](../.env.example), and no new account needs an image rebuild.

Use `vmdev user account` with a reviewed account config and a separate protected
secrets file, or the authenticated backend account endpoints. Do not put secret
values in command arguments or account outputs. Paper accounts use the local
paper broker and accept no exchange credentials. Account onboarding requires
explicit currency and initial paper cash/equity; it does not activate trading.

Backend uses `BACKEND_ADMIN_API_KEY` as `X-Admin-Key`. Scoring/execution admin
keys protect different services and cannot substitute for it. Runtime database
URLs use the six `vm_*_login` identities; administrator and migration URLs stay
outside those processes. Fresh bootstrap runs historical role DDL that requires
an already provisioned PostgreSQL superuser. The default uses `trader` for both
maintenance stages with separate administrator-database and target-database
URLs. This never grants runtime superuser authority. See
[DATABASE.md](DATABASE.md) for exact maintenance and host-connection procedures.

### Rotate the encryption master key

Rotation is explicit and account-scoped. Generate a new key into the protected
key store, prepend it to the existing ring, and restart backend/execution through
the supported lifecycle so every reader accepts both keys. Use the existing
`scripts/manage_broker_secret.py rotate` and `check` maintenance utility for each
registered account/reference with the reviewed database connection; it does not
require an additional container. The utility updates ciphertext and
`broker_credentials.last_rotated_at` atomically and does not return plaintext.

After every active credential has been re-encrypted and all readers use the new
ring, remove the previous key and restart through the same lifecycle. Retain the
keys needed by retained backups under the operator’s recovery policy. Never drop
an old key while current ciphertext still requires it. `SECRETS_MASTER_KEYS` is
the only supported key configuration; the singular name is rejected.

Every owner-linked paper account uses the in-process paper broker. Coinbase
sandbox and Saxo SIM are separate certification workflows and cannot be treated
as real-data paper acceptance. Saxo live and SIM hosts and credentials remain
separate. The platform launcher fixes `EXECUTION_MODE=paper` and
`EXECUTION_ENGINE_ALLOW_LIVE=false`; this documentation does not grant authority
to change those gates.

---

## Security

- **Never commit** keys. Keep `.env`, protected credential files and encryption
  recovery material outside source control; inject only the scoped values.
- **Least privilege:** a `View`-only key for market data; add `Trade` only for the
  live account. Never enable `Transfer`/withdrawal.
- **IP-allowlist** production keys; **rotate** by minting a new CDP key and
  updating the secret, then deleting the old key.
