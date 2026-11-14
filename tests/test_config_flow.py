"""End-to-end config flow: JWKS, PAR, sign-in callback, consent, entry."""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt
import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.openfeed.const import (
    CONF_GRANT_ID,
    CONF_SCOPES,
    CONF_SUBJECT,
    CONF_TOKEN,
    DOMAIN,
    PENDING_KEY,
)
from custom_components.openfeed.crypto import jwk_thumbprint, public_jwk

ISSUER = "https://auth.openfeed.au"
CLIENT_ID = "app-0b7c2f4e-1a2b-4c3d-8e9f-0123456789ab"
APP_ID = "0b7c2f4e-1a2b-4c3d-8e9f-0123456789ab"
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/auth",
    "token_endpoint": f"{ISSUER}/token",
    "pushed_authorization_request_endpoint": f"{ISSUER}/request",
    "jwks_uri": f"{ISSUER}/jwks",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The recorder must start before hass, so request it ahead of the integrations."""
    yield


@pytest.fixture
def server_key(app_key):
    """Stand-in for openfeed's id_token signing key (any RSA key will do)."""
    return app_key


def _id_token(server_key, nonce: str) -> str:
    kid = jwk_thumbprint(public_jwk(server_key))
    now = int(time.time())
    return jwt.encode(
        {"iss": ISSUER, "aud": CLIENT_ID, "sub": "user-1", "nonce": nonce, "iat": now, "exp": now + 300},
        server_key, algorithm="PS256", headers={"kid": kid},
    )


@pytest.fixture
def openfeed(aioclient_mock, server_key, key_pems):
    """Mock openfeed. Token responses come from a queue the test fills."""
    state = {"nonce": None, "tokens": []}
    jwk = {**public_jwk(server_key), "kid": jwk_thumbprint(public_jwk(server_key)), "alg": "PS256"}
    aioclient_mock.get(f"{ISSUER}/.well-known/openid-configuration", json=DISCOVERY)
    aioclient_mock.get(f"{ISSUER}/jwks", json={"keys": [jwk]})

    async def par(method, url, data):
        state["nonce"] = data["nonce"]
        state["par"] = data
        return AiohttpClientMockResponse(method, url, status=201, json={"request_uri": "urn:par:1"})

    async def token(method, url, data):
        body = state["tokens"].pop(0)
        if body.get("id_token") == "SIGN":
            body = {**body, "id_token": _id_token(server_key, state["nonce"])}
        return AiohttpClientMockResponse(method, url, json=body)

    aioclient_mock.post(f"{ISSUER}/request", side_effect=par)
    aioclient_mock.post(f"{ISSUER}/token", side_effect=token)
    with patch(
        "custom_components.openfeed.config_flow.generate_private_key_pem",
        side_effect=list(key_pems) * 4,
    ), patch("custom_components.openfeed.async_setup_entry", return_value=True):
        yield state


def _pending_state(hass: HomeAssistant, kind: str) -> str:
    return next(s for s, p in hass.data[PENDING_KEY].items() if p.kind == kind)


async def _start(hass: HomeAssistant, banking: bool = False):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert '"alg": "PS256"' in result["description_placeholders"]["jwks"]
    assert '"d"' not in result["description_placeholders"]["jwks"]
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"client_id": CLIENT_ID, "app_id": APP_ID, "redirect_base": "https://ha.example",
         "include_banking": banking},
    )


async def test_full_flow_with_consent(hass: HomeAssistant, openfeed, aioclient_mock) -> None:
    openfeed["tokens"] = [
        {"access_token": "a1", "refresh_token": "r1", "expires_in": 600, "id_token": "SIGN"},
        {"access_token": "a2", "expires_in": 600,
         "authorization_details": [{"type": "disclosure_grant_v1", "grant_id": "g-1"}]},
    ]
    result = await _start(hass, banking=True)
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"] == f"{ISSUER}/auth?client_id={CLIENT_ID}&request_uri=urn%3Apar%3A1"
    assert openfeed["par"]["resource"] == "https://api.openfeed.au"
    assert openfeed["par"]["redirect_uri"] == "https://ha.example/api/openfeed/callback"
    assert "openfeed-au:data:banking:read" in openfeed["par"]["scope"].split()

    state = _pending_state(hass, "auth")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "c1", "iss": ISSUER, "state": state})
    assert result["type"] is FlowResultType.EXTERNAL_STEP_DONE
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"].startswith("https://app.openfeed.au/grants/disclosure?appId=" + APP_ID)

    consent_state = _pending_state(hass, "consent")
    assert consent_state in result["url"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"grantId": "g-1", "consented": "true"})
    assert result["type"] is FlowResultType.EXTERNAL_STEP_DONE
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_GRANT_ID] == "g-1"
    assert data[CONF_SUBJECT] == "user-1"
    assert data[CONF_TOKEN]["refresh_token"] == "r1"
    assert "openid" in data[CONF_SCOPES]
    assert result["result"].unique_id == f"{CLIENT_ID}_user-1"
    assert not hass.data[PENDING_KEY]


async def test_state_mismatch_aborts(hass: HomeAssistant, openfeed) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "c1", "iss": ISSUER, "state": "forged"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result == result | {"type": FlowResultType.ABORT, "reason": "state_mismatch"}


async def test_issuer_mismatch_aborts(hass: HomeAssistant, openfeed) -> None:
    result = await _start(hass)
    state = _pending_state(hass, "auth")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "c1", "iss": "https://evil.example", "state": state})
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["reason"] == "issuer_mismatch"


async def test_consent_declined(hass: HomeAssistant, openfeed) -> None:
    openfeed["tokens"] = [{"access_token": "a1", "refresh_token": "r1", "expires_in": 600, "id_token": "SIGN"}]
    result = await _start(hass)
    state = _pending_state(hass, "auth")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "c1", "iss": ISSUER, "state": state})
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"consented": "false", "reason": "user_cancelled"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["reason"] == "consent_declined"


async def test_invalid_ids_show_errors(hass: HomeAssistant, openfeed) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"client_id": APP_ID, "app_id": CLIENT_ID, "redirect_base": "ha.local", "include_banking": False},
    )
    assert result["errors"] == {"client_id": "invalid_client_id", "app_id": "invalid_app_id",
                                "redirect_base": "invalid_url"}


async def test_reauth_replaces_existing_grant(hass: HomeAssistant, openfeed, key_pems) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=f"{CLIENT_ID}_user-1",
        data={"client_id": CLIENT_ID, "app_id": APP_ID, "redirect_base": "https://ha.example",
              "issuer": ISSUER, "scopes": ["openid", "offline_access", "energy"],
              "app_key_pem": key_pems[0], "dpop_key_pem": key_pems[1], "grant_id": "g-1",
              "token": {"access_token": "old", "refresh_token": "old", "expires_at": 0}},
    )
    entry.add_to_hass(hass)
    openfeed["tokens"] = [
        {"access_token": "a3", "refresh_token": "r3", "expires_in": 600, "id_token": "SIGN",
         "authorization_details": [{"type": "disclosure_grant_v1", "grant_id": "g-1"}]},
    ]
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert openfeed["par"]["grant_management_action"] == "replace"
    assert openfeed["par"]["grant_id"] == "g-1"
    state = _pending_state(hass, "auth")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "c3", "iss": ISSUER, "state": state})
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN]["access_token"] == "a3"
