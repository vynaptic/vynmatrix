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

Shared market-data access and tenant trading access are independent:

| Path | What it does | Where creds live | Permission needed |
|------|--------------|------------------|-------------------|
| **Market data** | Fetch candles for isolated warmup/backfill and live polling | Dedicated platform feed env vars (or an authenticated IBKR gateway); public venues need none | **View / market-data entitlement** |
| **Trading** | Place and track orders for one selected user account | Per-broker-account **JSON secret** referenced by `broker_credentials.secret_ref` | **Trade** |

For Coinbase, both paths use the **Coinbase Advanced Trade API** with **CDP
keys** (ES256 JWT auth — not the legacy HMAC key/secret/passphrase). Prefer
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

The local stack already forwards these to the market-data ingestor and execution
engine:

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml up -d
```

Verify market-data auth works (no more 401):

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml logs market-data-ingestor 2>&1 | grep -i "ingestion cycle complete"
```

---

## 3. Configure — future hosted environments

No hosted runtime is configured by this migration. The retained
`config/deployment/{staging,production}.yaml` files declare environment-backed
secret names, not a provisioned service. A future owner must separately review
secret delivery and inject values into the exact consuming process. Keep
account-scoped execution credentials separate from market-data credentials;
never copy a previous operator's environment or credential store.

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
  normal tenant paper execution always uses the deterministic in-process
  broker. Its explicit initial equity and cash belong to the linked account,
  not a secret. Exchange sandboxes use isolated certification workflows rather
  than tenant credential state.

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
different machine from the API client is unsupported. A DigitalOcean route must
therefore not be assumed from a successful workstation session.

IBKR execution resolves a positive-integer conid from the selected account's
`instrument_broker_symbols.broker_instrument_id`; symbol search and first-result
selection are forbidden. The adapter calls the documented `/tickle` boundary
before order submission after the cached health window, and an expired,
disconnected, or unauthenticated brokerage session fails closed. The canonical
seed contains only IBKR's officially documented AAPL conid (`265598`); every
other conid must come from reviewed IBKR reference data.

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
absent. The canonical seed includes only Saxo's publicly documented EURUSD
mapping (`UIC=21`, `AssetType=FxSpot`); other instruments must be onboarded
from authoritative Saxo reference data. Outstanding pre-trade disclaimers
block automated placement, and the adapter never accepts them on a user's
behalf.

Create a linked account with
`POST /users/{user_id}/broker-accounts`. Rotate any credential with
`PUT /users/{user_id}/broker-accounts/{account_id}/credentials`; rotation is a
complete replacement, never a partial patch. Both endpoints require the backend
operator key and return only the account/secret reference and expiry metadata,
never plaintext.

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

The `secret_ref` is resolved at execution time by a **provider-agnostic** secrets
backend — no cloud is assumed. Select it with `SECRETS_BACKEND`:

| `SECRETS_BACKEND` | Backend | Use |
|---|---|---|
| `env` (backend local default) | `EnvSecretsProvider` — env var `BROKER_CREDS_{REF}` (e.g. `BROKER_CREDS_COINBASE_LIVE_MAIN`) holds the JSON; read-only through the API | local inspection |
| `db` | `DbSecretsProvider` — JSON stored **encrypted at rest** in the `managed_secrets` table, encrypted with the newest key and decrypted by any key in `SECRETS_MASTER_KEYS` | **explicitly configured runtimes** |
| `composite` | DB (if `SECRETS_MASTER_KEYS` is set) then env | dev fallback |

### Encrypted account storage: the `db` backend

DO has no managed secrets API, and per-user env vars don't scale (a new tenant
would need a redeploy). The `db` backend stores each account's JSON **encrypted**
in Postgres, keyed by an account-owned `secret_ref`, using an ordered Fernet key
ring:

```bash
# 1. Generate the initial key (store as a DO secret / Droplet .env, never commit):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. On every DB-secret reader/writer (execution engine and backend API), set:
SECRETS_BACKEND=db
SECRETS_MASTER_KEYS=<the key from step 1>

# 3. Onboard a user's broker keys (encrypt + upsert into managed_secrets):
python scripts/manage_broker_secret.py set \
  --account-id <linked_broker_account_id> \
  --secret-ref "users/<user_id>/broker-accounts/<linked_broker_account_id>" \
  --api-key "$COINBASE_API_KEY" --api-secret "$COINBASE_API_SECRET"
```

Only the key-ring environment variable is deployed; per-user credentials remain
encrypted in the DB and are added with a row insert — **no redeploy per tenant**.
Keys are ordered newest first: new writes use the first key, while reads accept
every configured key.

### Rotate the encryption master key

Rotation is explicit so reads remain side-effect free and each account can be
audited. Generate a new key, prepend it to the existing ring, deploy that ring to
every DB-secrets reader, and then re-encrypt each registered credential:

```bash
SECRETS_MASTER_KEYS=<new_key>,<previous_key>

python scripts/manage_broker_secret.py rotate \
  --account-id <linked_broker_account_id> \
  --secret-ref "users/<user_id>/broker-accounts/<linked_broker_account_id>"

python scripts/manage_broker_secret.py check \
  --account-id <linked_broker_account_id> \
  --secret-ref "users/<user_id>/broker-accounts/<linked_broker_account_id>"
```

`rotate` updates the ciphertext and `broker_credentials.last_rotated_at` in one
transaction and never returns or prints plaintext. After every active
`broker_credentials` row has `last_rotated_at` at or after the recorded rollout
start and every service uses the new ring, remove the previous key and redeploy.
Never remove an old key before all ciphertext and all readers have moved.

`SECRETS_MASTER_KEYS` is the only supported key configuration. The companion
Any future deployment must inject that plural, newest-first ring; the removed
singular `SECRETS_MASTER_KEY` name is intentionally rejected.

- **Local backend reads:** leave `SECRETS_BACKEND` unset. The backend uses
  `EnvSecretsProvider`, so binding and broker-account reads remain available.
  Broker-account onboarding and credential rotation return 503 without changing
  database state because env variables are not a safe per-request write surface.
- **Local credential writes and execution:** set `SECRETS_BACKEND=db` and
  `SECRETS_MASTER_KEYS` to use the account-scoped encrypted store against local
  Postgres. The execution-engine Compose service deliberately defaults to `db`;
  it must resolve the same persisted `secret_ref` values and key ring as the
  backend rather than silently switching to process environment credentials.
- Every tenant-linked `environment="paper"` account executes through the local
  in-process paper broker and carries no exchange credential. Coinbase sandbox
  and Saxo SIM are separate, operator-run certification workflows; they are not
  tenant paper routes.
- Saxo `environment="live"` uses
  `gateway.saxobank.com/openapi`. Certification uses
  `gateway.saxobank.com/sim/openapi` with a distinct SIM application and token;
  a credential or host from one environment is never reused in the other.

> Live trading also requires `EXECUTION_MODE=live` **and**
> `EXECUTION_ENGINE_ALLOW_LIVE=true`; both default to off so nothing trades real
> money by accident.

---

## Security

- **Never commit** keys. `.env` and `*.pem` are gitignored; prod injects them as
  env vars (Droplet `.env` / App Platform `SECRET` env).
- **Least privilege:** a `View`-only key for market data; add `Trade` only for the
  live account. Never enable `Transfer`/withdrawal.
- **IP-allowlist** production keys; **rotate** by minting a new CDP key and
  updating the secret, then deleting the old key.
