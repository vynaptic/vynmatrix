# Broker credentials

This document owns credential acquisition, account-scoped storage, and rotation.
Credentials do not certify a broker, activate a strategy, or authorize an
order. Current broker-certification and incident state is in
[RUNBOOK.md](RUNBOOK.md); configuration ownership is in
[CONFIGURATION.md](CONFIGURATION.md).

## Credential boundaries

There are two separate secrets:

| Purpose | Storage and consumer |
| --- | --- |
| Market-data feed credential | Private .env value forwarded only to the selected market-data process |
| Broker-account credential | One complete encrypted JSON document linked to one owner account and resolved by execution |

Execution does not inherit shared feed credentials. A paper account uses the
local paper broker and has an empty exchange credential document. Its explicit
initial cash/equity and account currency are account data, not secrets.

## Coinbase market-data credential

Coinbase Advanced Trade uses CDP keys. Create a least-privilege key in the
Coinbase Developer Platform and retain the displayed key ID and secret through
your secure local secret mechanism. View permission is appropriate for
market-data-only use; never grant Transfer. Re-check current provider steps and
permissions before creating or rotating a key.

Put only private values in .env:

~~~dotenv
COINBASE_API_KEY=<key ID exactly as issued>
COINBASE_API_SECRET=<Ed25519 base64 secret or escaped ECDSA PEM>
~~~

The launcher forwards those optional values only to selected Coinbase
market-data work. Public candles do not require a trading key. Feed
connectivity does not prove complete, fresh, or account-authorized data.

## Broker-account credential document

Create a new account through the shared CLI contract:

~~~text
vmdev user account --config account.yaml --secrets-file protected-credentials.json
~~~

The public account configuration contains no credentials. The protected input
contains one complete JSON document. An identical existing account is validated,
not overwritten; an expected-value account patch cannot include credentials.
The account must have a stable owner-relative config_key, concrete
broker/environment identity, and required uppercase base currency.
On Windows, use --secrets-file - with protected redirected standard input:
owner-only file-mode validation is unavailable there.

Replace an existing broker credential document through the authenticated backend
PUT /broker-accounts/{account_id}/credentials route. Its replacement is atomic;
use an expected-value account patch only for non-secret account fields.

| Broker | Required encrypted fields | Additional boundary |
| --- | --- | --- |
| Coinbase | api_key, api_secret; passphrase only when that credential format requires it | Exact account and complete order-scoped fill reader remain required. |
| Deribit | api_key, api_secret; optional subaccount | Region/account and separate certification evidence remain required. |
| IBKR Client Portal | subaccount, gateway_url, ca_cert for live gateway | Gateway/session is owner-operated; use exact catalogued conid and account identity. |
| Zerodha | api_key, api_secret, access_token, access_token_expires_at | Expiry must be offset-aware; replace the full daily session document atomically. |
| Saxo | api_key, api_secret, access/refresh tokens and expiries, account_key, client_key | Refresh rotation replaces the complete document before the current token becomes unsafe. |
| Delta | api_key, api_secret, region | Region is exactly global or india; no regional fallback. |

Each document permits only fields valid for the selected broker. Credential
identity never selects an instrument or contract; exact broker instrument
mapping comes from the reviewed catalogue. Missing, expired, disconnected,
ambiguous, or untrusted credential/account state blocks broker I/O.

## Account and execution boundaries

The designated owner is resolved by the control plane; request callers cannot
supply another user identity. A binding references one concrete broker account,
and that account/currency stays attached through decision, order, fill,
position, P&L, and feedback.

Paper accounts require explicit initial cash/equity and cannot hold exchange
credentials. Execution reconstructs paper state from canonical account-scoped
fills and their persisted FX observations; positions and metrics are projections,
not restart-accounting input. Contract multiplier, contract type, and fill-time
FX are explicit ledger terms where applicable. Do not encode account selection,
currency, or parity assumptions in credential JSON.

## Encrypted storage and rotation

The database stores an account-owned secret reference and encrypted ciphertext
in managed_secrets. The newest-first SECRETS_MASTER_KEYS ring encrypts new
writes with its first key and reads ciphertext produced by every configured key.
Keep key recovery material separate from database backups.

To rotate the encryption key, add a new protected key to the front of the ring,
restart backend and execution through the supported lifecycle, re-encrypt each
registered ciphertext, verify it, then remove the old key only after every
reader and retained backup no longer needs it. The legacy manage_broker_secret
rotate action re-encrypts an existing document; it does not replace a
broker-specific credential document. Never publish a key, pass it on a command
line, or delete an old key while ciphertext still depends on it.

## Security rules

- Keep .env, protected credential input, and key-recovery material out of Git.
- Use distinct least-privilege credentials for feed and broker-account use.
- Confirm a provider's current IP allowlist, egress, rate, gateway, and session
  requirements before any future order-capable certification.
- Treat Coinbase sandbox and broker simulation environments as their own
  certification paths, not proof of real-data paper accounting.
- Keep EXECUTION_MODE=paper and EXECUTION_ENGINE_ALLOW_LIVE=false. This
  documentation does not grant authority to change either gate.
