"""Key material, client assertions and DPoP proofs for openfeed.

openfeed uses the FAPI 2.0 security profile: private_key_jwt client
authentication and DPoP-bound access tokens (RFC 9449), both signed with
PS256. The integration keeps two separate RSA keypairs, as openfeed advises:
an app keypair whose public half is registered in the openfeed dashboard, and
a DPoP keypair that never leaves Home Assistant.

This module has no Home Assistant imports so it can be unit tested alone.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ALG = "PS256"
KEY_SIZE = 2048
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def generate_private_key_pem() -> str:
    """Generate a new RSA private key and return it as PKCS#8 PEM."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def load_private_key(pem: str) -> rsa.RSAPrivateKey:
    """Load an RSA private key from PEM."""
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("openfeed keys must be RSA")
    return key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def public_jwk(key: rsa.RSAPrivateKey) -> dict[str, str]:
    """Return the minimal public JWK (kty, n, e) for a private key."""
    full = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {"kty": full["kty"], "n": full["n"], "e": full["e"]}


def jwk_thumbprint(jwk: dict[str, str]) -> str:
    """RFC 7638 thumbprint of an RSA public JWK."""
    canonical = json.dumps(
        {"e": jwk["e"], "kty": jwk["kty"], "n": jwk["n"]},
        separators=(",", ":"),
        sort_keys=True,
    )
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def public_jwks(key: rsa.RSAPrivateKey) -> dict[str, list[dict[str, str]]]:
    """The JWKS to paste into the openfeed dashboard for the app keypair."""
    jwk = public_jwk(key)
    return {
        "keys": [
            {**jwk, "kid": jwk_thumbprint(jwk), "use": "sig", "alg": ALG},
        ]
    }


def client_assertion(
    key: rsa.RSAPrivateKey,
    client_id: str,
    issuer: str,
    now: float | None = None,
) -> str:
    """Build a private_key_jwt client assertion.

    openfeed requires aud to be the issuer, not the endpoint URL, and a fresh
    jti on every assertion. Assertions are never cached.
    """
    issued = int(now if now is not None else time.time())
    jwk = public_jwk(key)
    return jwt.encode(
        {
            "iss": client_id,
            "sub": client_id,
            "aud": issuer,
            "jti": str(uuid.uuid4()),
            "iat": issued,
            "exp": issued + 60,
        },
        key,
        algorithm=ALG,
        headers={"kid": jwk_thumbprint(jwk)},
    )


def htu(url: str) -> str:
    """The DPoP htu claim: the request URL without query or fragment.

    The path is left exactly as sent. openfeed IDs can contain a raw '='
    (base64 padding) and the proof fails if the request and the htu disagree
    on its encoding.
    """
    return url.split("#", 1)[0].split("?", 1)[0]


def access_token_hash(access_token: str) -> str:
    """The DPoP ath claim: base64url SHA-256 of the access token."""
    return _b64url(hashlib.sha256(access_token.encode()).digest())


def dpop_proof(
    key: rsa.RSAPrivateKey,
    method: str,
    url: str,
    access_token: str | None = None,
    nonce: str | None = None,
    now: float | None = None,
) -> str:
    """Build a single-use DPoP proof JWT (RFC 9449)."""
    payload: dict[str, Any] = {
        "htm": method.upper(),
        "htu": htu(url),
        "iat": int(now if now is not None else time.time()),
        "jti": str(uuid.uuid4()),
    }
    if access_token is not None:
        payload["ath"] = access_token_hash(access_token)
    if nonce is not None:
        payload["nonce"] = nonce
    return jwt.encode(
        payload,
        key,
        algorithm=ALG,
        headers={"typ": "dpop+jwt", "jwk": public_jwk(key)},
    )


def pkce_pair() -> tuple[str, str]:
    """Return a PKCE (verifier, S256 challenge) pair."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def random_token() -> str:
    """A URL-safe random value for state and nonce."""
    return secrets.token_urlsafe(32)
