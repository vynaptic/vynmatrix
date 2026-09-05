"""Coinbase Advanced Trade JWT auth — ECDSA (ES256) and Ed25519 (EdDSA) keys.

Verifies that ``build_coinbase_rest_jwt`` produces a structurally correct,
cryptographically valid JWT for both CDP key types, without hitting the network.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from lib_infrastructure.brokers.adapters.coinbase_auth import build_coinbase_rest_jwt


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _split(token: str) -> tuple[dict, dict, bytes, bytes]:
    header_b64, payload_b64, sig_b64 = token.split(".")
    header = json.loads(_b64url_decode(header_b64))
    payload = json.loads(_b64url_decode(payload_b64))
    signing_input = f"{header_b64}.{payload_b64}".encode()
    return header, payload, _b64url_decode(sig_b64), signing_input


def test_es256_jwt_for_ec_pem_key() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()

    token = build_coinbase_rest_jwt(
        api_key="organizations/o/apiKeys/k",
        api_secret=pem,
        method="GET",
        host="api.coinbase.com",
        path="/api/v3/brokerage/accounts",
    )

    header, payload, signature, signing_input = _split(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == "organizations/o/apiKeys/k"
    assert payload["sub"] == "organizations/o/apiKeys/k"
    assert payload["uri"] == "GET api.coinbase.com/api/v3/brokerage/accounts"
    # ES256 signature is raw r||s (64 bytes) -> rebuild DER to verify.
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    key.public_key().verify(encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256()))


def test_eddsa_jwt_for_ed25519_base64_key() -> None:
    key = Ed25519PrivateKey.generate()
    # CDP issues the 64-byte seed||public_key form, base64-encoded.
    seed = key.private_bytes_raw()
    public = key.public_key().public_bytes_raw()
    secret_b64 = base64.b64encode(seed + public).decode()

    token = build_coinbase_rest_jwt(
        api_key="00000000-0000-4000-8000-000000000001",
        api_secret=secret_b64,
        method="GET",
        host="api.coinbase.com",
        path="/api/v3/brokerage/products/BTC-USD/candles",
    )

    header, payload, signature, signing_input = _split(token)
    assert header["alg"] == "EdDSA"
    assert header["kid"] == "00000000-0000-4000-8000-000000000001"
    assert payload["iss"] == "cdp"
    # EdDSA signature is the raw 64-byte signature.
    key.public_key().verify(signature, signing_input)


def test_tampered_eddsa_signature_is_rejected() -> None:
    key = Ed25519PrivateKey.generate()
    secret_b64 = base64.b64encode(
        key.private_bytes_raw() + key.public_key().public_bytes_raw()
    ).decode()
    token = build_coinbase_rest_jwt(
        api_key="k", api_secret=secret_b64, method="GET", host="h", path="/p"
    )
    _, _, signature, signing_input = _split(token)
    with pytest.raises(InvalidSignature):
        key.public_key().verify(bytes([signature[0] ^ 0x01]) + signature[1:], signing_input)


def test_garbage_secret_raises() -> None:
    with pytest.raises(ValueError, match="neither an EC PEM nor a base64"):
        build_coinbase_rest_jwt(
            api_key="k", api_secret="not-a-key!!!", method="GET", host="h", path="/p"
        )
