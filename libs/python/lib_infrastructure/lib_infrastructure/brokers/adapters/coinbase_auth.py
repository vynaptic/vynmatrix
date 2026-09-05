"""Shared Coinbase Advanced Trade authentication helpers.

Supports both Coinbase CDP API-key types, detected from the secret format:

* **ECDSA** — a PEM-encoded EC private key; signed as ``ES256``.
* **Ed25519** — a base64-encoded 64-byte key (the newer CDP default); signed as
  ``EdDSA``.

Either key works for the market-data client and the trading adapter; the JWT
``kid``/``sub`` is the API key name/id exactly as Coinbase issues it.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
from collections.abc import Callable
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

# Ed25519 private keys are a 32-byte seed; CDP issues the 64-byte
# seed||public_key form, so we take the leading 32 bytes.
_ED25519_SEED_BYTES = 32


def normalize_coinbase_private_key(api_secret: str) -> str:
    """Normalize newline-escaped PEM content from env/JSON storage.

    Harmless for base64 Ed25519 secrets (which contain no escaped newlines).
    """
    return api_secret.replace("\\n", "\n")


def build_coinbase_auth_headers(
    *,
    api_key: str,
    api_secret: str,
    method: str,
    host: str,
    path: str,
) -> dict[str, str]:
    token = build_coinbase_rest_jwt(
        api_key=api_key,
        api_secret=api_secret,
        method=method,
        host=host,
        path=path,
    )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def build_coinbase_rest_jwt(
    *,
    api_key: str,
    api_secret: str,
    method: str,
    host: str,
    path: str,
) -> str:
    """Build a Coinbase Advanced Trade REST JWT (ES256 for EC keys, EdDSA for Ed25519)."""
    algorithm, sign = _resolve_signer(normalize_coinbase_private_key(api_secret))
    now = int(time.time())
    header = {
        "alg": algorithm,
        "kid": api_key,
        "nonce": secrets.token_hex(16),
        "typ": "JWT",
    }
    payload = {
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "sub": api_key,
        "uri": f"{method.upper()} {host}{path}",
    }

    encoded_header = _urlsafe_b64encode_json(header)
    encoded_payload = _urlsafe_b64encode_json(payload)
    signing_input = f"{encoded_header}.{encoded_payload}"
    encoded_signature = _urlsafe_b64encode(sign(signing_input.encode("utf-8")))
    return f"{signing_input}.{encoded_signature}"


def _resolve_signer(secret: str) -> tuple[str, Callable[[bytes], bytes]]:
    """Detect the CDP key type from the secret and return ``(jwt_alg, signer)``."""
    if "-----BEGIN" in secret:
        private_key = serialization.load_pem_private_key(secret.encode("utf-8"), password=None)
        if not isinstance(private_key, ec.EllipticCurvePrivateKey):
            msg = "Coinbase PEM API secret must be an EC private key"
            raise TypeError(msg)

        def _sign_es256(data: bytes) -> bytes:
            der_signature = private_key.sign(data, ec.ECDSA(hashes.SHA256()))
            r_int, s_int = decode_dss_signature(der_signature)
            return int(r_int).to_bytes(32, "big") + int(s_int).to_bytes(32, "big")

        return "ES256", _sign_es256

    # Otherwise: a base64-encoded Ed25519 key (the CDP "Ed25519" key type).
    try:
        raw = base64.b64decode(secret, validate=True)
    except (binascii.Error, ValueError) as exc:
        msg = "Coinbase API secret is neither an EC PEM nor a base64 Ed25519 key"
        raise ValueError(msg) from exc
    if len(raw) < _ED25519_SEED_BYTES:
        msg = "Coinbase Ed25519 API secret must decode to at least 32 bytes"
        raise ValueError(msg)
    ed25519_key = Ed25519PrivateKey.from_private_bytes(raw[:_ED25519_SEED_BYTES])
    return "EdDSA", ed25519_key.sign


def _urlsafe_b64encode_json(payload: dict[str, Any]) -> str:
    return _urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )


def _urlsafe_b64encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")
