"""Sharing API client: error mapping, pagination, revocation."""

from __future__ import annotations

import asyncio

import aiohttp
import jwt
import pytest

from custom_components.openfeed import api
from custom_components.openfeed.api import OpenfeedClient



async def _token():
    return "at"


def _client(session, dpop_key, base):
    return OpenfeedClient(session, dpop_key, _token, base)


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (401, {"error": "invalid_token"}, api.TokenRejectedError),
        (402, {"code": "credit_exhausted"}, api.CreditExhaustedError),
        (403, {"code": "disclosure_grant_required"}, api.GrantRevokedError),
        (403, {"code": "balance_unavailable"}, api.BalanceUnavailableError),
        (404, {"code": "not_found"}, api.NotFoundError),
        (429, {"code": "rate_limited"}, api.RateLimitedError),
        (502, {"code": "balance_temporarily_unavailable"}, api.BalanceUnavailableError),
    ],
)
async def test_error_mapping(fake_server, dpop_key, status, payload, expected):
    fake_server.add("GET", "/v1/energy/accounts/a/balance", status=status, json=payload)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(expected):
            await _client(session, dpop_key, fake_server.url).energy_balance("a")


async def test_empty_403_retried_once(fake_server, dpop_key):
    fake_server.add("GET", "/v1/energy/accounts/a/balance", status=403, body="")
    fake_server.add("GET", "/v1/energy/accounts/a/balance", json={"data": {"balance": "1.00"}})
    async with aiohttp.ClientSession() as session:
        assert await _client(session, dpop_key, fake_server.url).energy_balance("a") == {"balance": "1.00"}


async def test_nonce_retry_on_401(fake_server, dpop_key):
    fake_server.add("GET", "/v1/energy/accounts/a/balance", status=401, body="",
                    headers={"WWW-Authenticate": 'DPoP error="use_dpop_nonce"', "DPoP-Nonce": "n2"})
    fake_server.add("GET", "/v1/energy/accounts/a/balance", json={"data": {}})
    async with aiohttp.ClientSession() as session:
        await _client(session, dpop_key, fake_server.url).energy_balance("a")
    proof = jwt.decode(fake_server.requests[1]["headers"]["DPoP"], options={"verify_signature": False})
    assert proof["nonce"] == "n2"


async def test_pagination_and_dpop_header(fake_server, dpop_key):
    base = fake_server.url
    fake_server.add("GET", "/v1/energy/accounts",
                    json={"data": [{"accountId": "1"}], "links": {"next": f"{base}/v1/energy/accounts?page=2"}})
    fake_server.add("GET", "/v1/energy/accounts", json={"data": [{"accountId": "2"}], "links": {}})
    async with aiohttp.ClientSession() as session:
        items = await _client(session, dpop_key, base).energy_accounts()
    assert [i["accountId"] for i in items] == ["1", "2"]
    first = fake_server.requests[0]
    assert first["query"]["limit"] == "1000"
    assert first["headers"]["Authorization"] == "DPoP at"
    claims = jwt.decode(first["headers"]["DPoP"], options={"verify_signature": False})
    assert claims["htu"] == f"{base}/v1/energy/accounts"
    assert claims["ath"]


async def test_ids_keep_raw_equals(fake_server, dpop_key):
    fake_server.add("GET", "/v1/energy/accounts/LTmp=/balance", json={"data": {}})
    async with aiohttp.ClientSession() as session:
        await _client(session, dpop_key, fake_server.url).energy_balance("LTmp=")
    assert fake_server.requests[0]["raw_path"] == "/v1/energy/accounts/LTmp=/balance"


async def test_der_404_is_none(fake_server, dpop_key):
    fake_server.add("GET", "/v1/energy/accounts/a/meters/m/der", status=404, json={})
    async with aiohttp.ClientSession() as session:
        assert await _client(session, dpop_key, fake_server.url).energy_der("a", "m") is None


async def test_revocation_cancels_siblings():
    cancelled = asyncio.Event()

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def revoked():
        raise api.GrantRevokedError(403, "disclosure_grant_required")

    with pytest.raises(api.GrantRevokedError):
        await api.cancel_on_revocation([slow(), revoked()])
    assert cancelled.is_set()


async def test_other_errors_returned_in_place():
    async def ok():
        return 1

    async def bad():
        raise api.BalanceUnavailableError(403, "balance_unavailable")

    result = await api.cancel_on_revocation([ok(), bad()])
    assert result[0] == 1
    assert isinstance(result[1], api.BalanceUnavailableError)
