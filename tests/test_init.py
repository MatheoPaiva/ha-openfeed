"""Entry setup: coordinator, sensors, Energy statistics, revocation."""

from __future__ import annotations

from datetime import timedelta
import time

import pytest

from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.openfeed.const import DOMAIN
from custom_components.openfeed.usage import AEST

ISSUER = "https://auth.openfeed.au"
API = "https://api.openfeed.au"
CLIENT_ID = "app-0b7c2f4e-1a2b-4c3d-8e9f-0123456789ab"
ACC = "LTmpAcc="
METER = "MtrId="
NMI = "QB12345678"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    yield


@pytest.fixture
def entry(hass: HomeAssistant, key_pems) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=f"{CLIENT_ID}_user-1", title="openfeed CDR",
        data={"client_id": CLIENT_ID, "app_id": CLIENT_ID[4:], "redirect_base": "https://ha.example",
              "issuer": ISSUER, "scopes": ["openid", "offline_access", "openfeed-au:data:energy:read",
                                           "openfeed-au:data:banking:read"],
              "app_key_pem": key_pems[0], "dpop_key_pem": key_pems[1], "grant_id": "g-1",
              "subject": "user-1",
              "token": {"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600,
                        "grant_id": "g-1"}},
        options={"backfill_days": 3},
    )
    entry.add_to_hass(hass)
    return entry


def _usage_records():
    today = dt_util.now(AEST).date()
    out = []
    for back in (2, 1):
        day = (today - timedelta(days=back)).isoformat()
        out.append({"meterId": METER, "intervalDate": day, "reads": [
            {"meterId": METER, "registerSuffix": "E1", "readStartDate": day, "unitOfMeasure": "KWH",
             "readUType": "intervalRead", "intervalRead": {"readIntervalLength": 30,
                                                           "intervalReads": [0.5] * 48}},
            {"meterId": METER, "registerSuffix": "B1", "readStartDate": day, "unitOfMeasure": "KWH",
             "readUType": "intervalRead", "intervalRead": {"readIntervalLength": 30,
                                                           "intervalReads": [0.25] * 48}},
        ]})
    return out


def _mock_api(aioclient_mock, revoked: bool = False):
    a = f"{API}/v1/energy/accounts/{ACC}"
    if revoked:
        aioclient_mock.get(f"{API}/v1/energy/accounts", status=403,
                           json={"code": "disclosure_grant_required"})
        return
    aioclient_mock.get(f"{API}/v1/energy/accounts", json={"data": [{"accountId": ACC, "displayName": "Home"}]})
    aioclient_mock.get(f"{a}/balance", json={"data": {"balance": "-123.45", "currency": "AUD"}})
    aioclient_mock.get(f"{a}/meters", json={"data": [{"meterId": METER, "nationalMeteringId": NMI}]})
    aioclient_mock.get(f"{a}/invoices", json={"data": [
        {"invoiceNumber": "1", "issueDate": "2026-09-01", "dueDate": "2026-09-15", "invoiceAmount": "100.00", "paymentStatus": "PAID"},
        {"invoiceNumber": "2", "issueDate": "2026-10-01", "dueDate": "2026-10-15", "invoiceAmount": "88.10", "paymentStatus": "NOT_PAID"},
    ]})
    aioclient_mock.get(f"{a}/meters/{METER}/der", status=404, json={})
    aioclient_mock.get(f"{a}/meters/{METER}/usage", json={"data": _usage_records()})
    aioclient_mock.get(f"{API}/v1/banking/accounts", json={"data": [{"accountId": "B1=", "displayName": "Everyday"}]})
    aioclient_mock.get(f"{API}/v1/banking/accounts/B1=/balance", status=403,
                       json={"code": "balance_unavailable"})


async def test_setup_creates_sensors_and_statistics(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    balance = next(s for e, s in states.items() if "balance" in e and s.attributes.get("unit_of_measurement") == "AUD"
                   and s.state not in ("unknown", "unavailable"))
    assert float(balance.state) == -123.45
    consumption = next(s for e, s in states.items() if "consumption" in e)
    assert float(consumption.state) == 24.0
    export = next(s for e, s in states.items() if "export" in e and "limit" not in e)
    assert float(export.state) == 12.0
    invoice = next(s for e, s in states.items() if "invoice_amount" in e)
    assert float(invoice.state) == 88.10
    assert states["sensor.energy_home_latest_invoice_status"].state == "NOT_PAID"
    assert states["sensor.bank_everyday_current_balance"].state == "unavailable"

    await async_wait_recording_done(hass)
    stat_id = f"openfeed:nmi_{NMI.lower()}_consumption"
    last = await hass.async_add_executor_job(get_last_statistics, hass, 1, stat_id, True, {"sum"})
    assert last[stat_id][0]["sum"] == pytest.approx(48.0)

    # Every API call carried a DPoP-bound token.
    for method, url, data, headers in aioclient_mock.mock_calls:
        if str(url).startswith(API):
            assert headers["Authorization"] == "DPoP at"
            assert headers["DPoP"]


async def test_revocation_triggers_reauth(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock, revoked=True)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"
    assert entry.data["grant_id"] is None


async def test_credit_exhausted_raises_issue(hass, entry, aioclient_mock) -> None:
    aioclient_mock.get(f"{API}/v1/energy/accounts", status=402, json={"code": "credit_exhausted"})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert ir.async_get(hass).async_get_issue(DOMAIN, "credit_exhausted")


async def test_diagnostics_redacts(hass, entry, aioclient_mock) -> None:
    from custom_components.openfeed.diagnostics import async_get_config_entry_diagnostics

    _mock_api(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    for secret in ("BEGIN PRIVATE KEY", "'rt'", NMI, ACC, METER, "user-1"):
        assert secret not in text
