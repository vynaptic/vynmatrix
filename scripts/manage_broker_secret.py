"""Manage DB-encrypted broker secrets (the ``SECRETS_BACKEND=db`` backend).

Encrypts per-account broker credentials using the newest key in the ordered
``SECRETS_MASTER_KEYS`` ring and upserts them into ``managed_secrets``. A
``secret_ref`` must be uniquely registered to the supplied linked broker account.
This is how live credentials are onboarded and master-key ciphertext is rotated
on a self-hosted / DigitalOcean deployment without exposing plaintext.

Never prints secret values — ``check`` only confirms a ref decrypts.

Usage:
    export SECRETS_MASTER_KEYS=newest,previous
    export DATABASE_URL=postgresql://...

    # Store/rotate a user's Coinbase live keys (values read from env, not argv,
    # so they do not leak into shell history / process listings):
    export SEC_API_KEY=... SEC_API_SECRET=...
    python scripts/manage_broker_secret.py set \
        --account-id 42 --secret-ref users/u1/broker-accounts/42

    # Re-encrypt existing ciphertext with the newest key, atomically:
    python scripts/manage_broker_secret.py rotate \
        --account-id 42 --secret-ref users/u1/broker-accounts/42

    # Verify the account-owned ref decrypts (prints field names only):
    python scripts/manage_broker_secret.py check \
        --account-id 42 --secret-ref users/u1/broker-accounts/42
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from lib_application.db.session import create_engine_for_env, get_session_factory
from lib_common.env_utils import build_database_url
from lib_infrastructure.brokers.secrets import create_secrets_provider


def _provider() -> Any:
    engine = create_engine_for_env(db_url=build_database_url())
    session_factory = get_session_factory(engine=engine)
    try:
        return create_secrets_provider(
            backend="db",
            session_factory=session_factory,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _cmd_set(args: argparse.Namespace) -> None:
    # Prefer env for the sensitive values so they never appear in argv/history.
    api_key = args.api_key or os.environ.get("SEC_API_KEY")
    api_secret = args.api_secret or os.environ.get("SEC_API_SECRET")
    passphrase = args.passphrase or os.environ.get("SEC_PASSPHRASE")
    if not api_key or not api_secret:
        print(
            "ERROR: provide --api-key/--api-secret or SEC_API_KEY/SEC_API_SECRET env",
            file=sys.stderr,
        )
        raise SystemExit(2)

    payload: dict[str, str] = {"api_key": api_key, "api_secret": api_secret}
    if passphrase:
        payload["passphrase"] = passphrase

    try:
        _provider().set_secret(
            args.secret_ref,
            json.dumps(payload),
            account_id=args.account_id,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"stored encrypted secret for ref={args.secret_ref!r}")


def _cmd_check(args: argparse.Namespace) -> None:
    raw = asyncio.run(
        _provider().get_secret_for_account(
            args.secret_ref,
            account_id=args.account_id,
        )
    )
    if raw is None:
        print(
            f"NOT FOUND, not account-owned, or not decryptable: {args.secret_ref!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"INVALID credential payload: {args.secret_ref!r}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not isinstance(payload, dict):
        print(f"INVALID credential payload: {args.secret_ref!r}", file=sys.stderr)
        raise SystemExit(1)
    present = [
        field
        for field in ("api_key", "api_secret", "passphrase", "subaccount")
        if payload.get(field)
    ]
    print(f"OK: {args.secret_ref!r} decrypts; fields present: {present}")


def _cmd_rotate(args: argparse.Namespace) -> None:
    try:
        _provider().rotate_secret(
            args.secret_ref,
            account_id=args.account_id,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"rotated encrypted secret for ref={args.secret_ref!r}")


def _add_account_ref_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--secret-ref", required=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage DB-encrypted broker secrets")
    sub = parser.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set", help="Encrypt + upsert a broker credential")
    _add_account_ref_args(p_set)
    p_set.add_argument("--api-key", help="(prefer SEC_API_KEY env to avoid argv leak)")
    p_set.add_argument("--api-secret", help="(prefer SEC_API_SECRET env)")
    p_set.add_argument("--passphrase", help="(optional; or SEC_PASSPHRASE env)")
    p_set.set_defaults(func=_cmd_set)

    p_check = sub.add_parser("check", help="Confirm a ref decrypts (no values printed)")
    _add_account_ref_args(p_check)
    p_check.set_defaults(func=_cmd_check)

    p_rotate = sub.add_parser(
        "rotate",
        help="Re-encrypt a ref with the newest master key",
    )
    _add_account_ref_args(p_rotate)
    p_rotate.set_defaults(func=_cmd_rotate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
