"""Client assertion and DPoP proof shape (openfeed build checklist)."""

from __future__ import annotations

import jwt

from custom_components.openfeed import crypto


def test_client_assertion_claims(app_key):
    token = crypto.client_assertion(app_key, "app-x", "https://auth.openfeed.au", now=1000)
    pub = app_key.public_key()
    claims = jwt.decode(token, pub, algorithms=["PS256"], audience="https://auth.openfeed.au",
                        options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == claims["sub"] == "app-x"
    assert claims["exp"] - claims["iat"] == 60
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "PS256"
    assert header["kid"] == crypto.public_jwks(app_key)["keys"][0]["kid"]


def test_assertion_jti_is_fresh(app_key):
    a = jwt.decode(crypto.client_assertion(app_key, "app-x", "i"), options={"verify_signature": False})
    b = jwt.decode(crypto.client_assertion(app_key, "app-x", "i"), options={"verify_signature": False})
    assert a["jti"] != b["jti"]


def test_dpop_proof_header_and_claims(dpop_key):
    url = "https://api.openfeed.au/v1/energy/accounts/LTmp=/meters?limit=1000#x"
    proof = crypto.dpop_proof(dpop_key, "get", url, access_token="tok", nonce="n1")
    header = jwt.get_unverified_header(proof)
    assert header["typ"] == "dpop+jwt"
    assert header["alg"] == "PS256"
    assert set(header["jwk"]) == {"kty", "n", "e"}  # public only
    claims = jwt.decode(proof, dpop_key.public_key(), algorithms=["PS256"])
    assert claims["htm"] == "GET"
    # Query and fragment stripped, '=' left raw.
    assert claims["htu"] == "https://api.openfeed.au/v1/energy/accounts/LTmp=/meters"
    assert claims["ath"] == crypto.access_token_hash("tok")
    assert claims["nonce"] == "n1"


def test_token_endpoint_proof_has_no_ath(dpop_key):
    claims = jwt.decode(crypto.dpop_proof(dpop_key, "POST", "https://auth/token"),
                        dpop_key.public_key(), algorithms=["PS256"])
    assert "ath" not in claims


def test_jwks_has_no_private_material(app_key):
    key = crypto.public_jwks(app_key)["keys"][0]
    assert not {"d", "p", "q", "dp", "dq", "qi"} & set(key)
    assert key["alg"] == "PS256" and key["use"] == "sig"


def test_pkce_pair():
    verifier, challenge = crypto.pkce_pair()
    import base64
    import hashlib
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert 43 <= len(verifier) <= 128
