"""PAR, token exchange, nonce retry and refresh against a mocked server."""

from __future__ import annotations

import aiohttp
import jwt
import pytest

from custom_components.openfeed.oauth import OpenfeedAuth, OpenfeedAuthError, TokenSet



def _discovery(issuer):
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/auth",
        "token_endpoint": f"{issuer}/token",
        "pushed_authorization_request_endpoint": f"{issuer}/request",
        "jwks_uri": f"{issuer}/jwks",
    }


TOKEN = {
    "access_token": "at", "refresh_token": "rt", "token_type": "DPoP", "expires_in": 600,
    "scope": "openid energy", "id_token": "x",
    "authorization_details": [{"type": "disclosure_grant_v1", "grant_id": "g-1"}],
}


@pytest.fixture
def mock(fake_server):
    fake_server.add("GET", "/.well-known/openid-configuration", json=_discovery(fake_server.url), repeat=True)
    return fake_server


def _auth(app_key, dpop_key, session, issuer):
    return OpenfeedAuth(session, "app-x", app_key, dpop_key, issuer, "https://api.openfeed.au")


def _posts(mock):
    return [r for r in mock.requests if r["method"] == "POST"]


async def test_par_sends_resource_and_assertion(mock, app_key, dpop_key):
    mock.add("POST", "/request", status=201, json={"request_uri": "urn:r", "expires_in": 60})
    async with aiohttp.ClientSession() as session:
        auth = _auth(app_key, dpop_key, session, mock.url)
        uri = await auth.push_authorization_request(
            redirect_uri="https://ha/cb", scopes=["openid", "energy"], state="s", nonce="n",
            code_challenge="c")
        assert uri == "urn:r"
        assert auth.authorize_url(uri) == f"{mock.url}/auth?client_id=app-x&request_uri=urn%3Ar"
    form = _posts(mock)[0]["form"]
    assert form["resource"] == "https://api.openfeed.au"
    assert form["code_challenge_method"] == "S256"
    claims = jwt.decode(form["client_assertion"], app_key.public_key(), algorithms=["PS256"],
                        audience=mock.url)
    assert claims["iss"] == "app-x"


async def test_par_error_mapped(mock, app_key, dpop_key):
    mock.add("POST", "/request", status=400, json={"error": "invalid_scope"})
    async with aiohttp.ClientSession() as session:
        auth = _auth(app_key, dpop_key, session, mock.url)
        with pytest.raises(OpenfeedAuthError) as err:
            await auth.push_authorization_request(
                redirect_uri="r", scopes=["x"], state="s", nonce="n", code_challenge="c")
    assert err.value.error == "invalid_scope"


async def test_exchange_retries_once_with_nonce(mock, app_key, dpop_key):
    mock.add("POST", "/token", status=400, json={"error": "use_dpop_nonce"},
              headers={"DPoP-Nonce": "abc"})
    mock.add("POST", "/token", status=200, json=TOKEN)
    async with aiohttp.ClientSession() as session:
        auth = _auth(app_key, dpop_key, session, mock.url)
        tokens = await auth.exchange_code("code", "verifier", "https://ha/cb")
    assert tokens.grant_id == "g-1"
    assert tokens.access_token == "at"
    calls = _posts(mock)
    second = jwt.decode(calls[1]["headers"]["DPoP"], options={"verify_signature": False})
    assert second["nonce"] == "abc"
    assert second["htu"] == f"{mock.url}/token"
    assert calls[1]["form"]["resource"] == "https://api.openfeed.au"


async def test_refresh_keeps_previous_refresh_token(mock, app_key, dpop_key):
    body = {k: v for k, v in TOKEN.items() if k not in ("refresh_token", "authorization_details")}
    mock.add("POST", "/token", status=200, json=body)
    previous = TokenSet.from_response(TOKEN, 0)
    async with aiohttp.ClientSession() as session:
        auth = _auth(app_key, dpop_key, session, mock.url)
        tokens = await auth.refresh(previous)
    assert tokens.refresh_token == "rt"
    assert tokens.grant_id == "g-1"


async def test_invalid_grant_flagged(mock, app_key, dpop_key):
    mock.add("POST", "/token", status=400, json={"error": "invalid_grant"})
    async with aiohttp.ClientSession() as session:
        auth = _auth(app_key, dpop_key, session, mock.url)
        with pytest.raises(OpenfeedAuthError) as err:
            await auth.refresh(TokenSet.from_response(TOKEN, 0))
    assert err.value.is_invalid_grant


async def test_issuer_mismatch_rejected(fake_server, app_key, dpop_key):
    fake_server.add("GET", "/.well-known/openid-configuration", json=_discovery("https://evil"))
    async with aiohttp.ClientSession() as session:
        auth = _auth(app_key, dpop_key, session, fake_server.url)
        with pytest.raises(OpenfeedAuthError) as err:
            await auth.discover()
    assert err.value.error == "issuer_mismatch"
