"""Entry setup: coordinator, sensors, Energy statistics, revocation."""

from __future__ import annotations

from datetime import timedelta
import time

import pytest

from homeassistant.components.recorder.statistics import get_last_statistics, list_statistic_ids
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


DER = {
    "approvedCapacity": 10,
    "protectionMode": {"exportLimitKva": 5},
    "acConnections": [
        {"connectionIdentifier": 1, "count": 1, "manufacturerName": "Tesla",
         "inverterSeries": "Tesla Powerwall", "inverterDeviceCapacity": 5, "status": "ACTIVE",
         "derDevices": [{"deviceIdentifier": 2, "count": 10, "type": "SOLAR_PV",
                         "nominalRatedCapacity": 0.4, "status": "ACTIVE"}]},
        {"connectionIdentifier": 3, "count": 1, "manufacturerName": "Old Co",
         "inverterDeviceCapacity": 3, "status": "DECOMMISSIONED", "derDevices": []},
    ],
}


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
        {"invoiceNumber": "2", "issueDate": "2026-10-01", "dueDate": "2026-10-15", "invoiceAmount": "88.10", "paymentStatus": "NOT_PAID",
         "meterIds": [METER]},
    ]})
    aioclient_mock.get(a, json={"data": {"accountId": ACC, "plans": [
        {"planOverview": {"displayName": "Wholesale", "startDate": "2026-01-01"}}]}})
    aioclient_mock.get(f"{a}/billing", json={"data": [
        {"accountId": ACC, "executionDateTime": f"{dt_util.now(AEST).date().replace(day=1).isoformat()}T09:00:00+10:00",
         "transactionUType": "usage", "amount": 12.5, "description": "Usage"},
        {"accountId": ACC, "executionDateTime": f"{dt_util.now(AEST).date().replace(day=1).isoformat()}T09:00:00+10:00",
         "transactionUType": "usage", "amount": -4.0, "description": "Feed-in credit"},
        {"accountId": ACC, "executionDateTime": "2026-09-03T09:00:00+10:00",
         "transactionUType": "payment", "amount": -100.0},
    ]})
    aioclient_mock.get(f"{a}/meters/{METER}/der", json={"data": DER})
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
    meter = f"sensor.meter_{NMI.lower()}"
    assert float(states[f"{meter}_latest_day_grid_consumption"].state) == 24.0
    assert float(states[f"{meter}_latest_day_grid_export"].state) == 12.0
    # 0.5 kWh per 30 minutes is 1 kW; 0.25 kWh is 0.5 kW.
    assert float(states[f"{meter}_latest_day_peak_grid_import"].state) == 1.0
    assert float(states[f"{meter}_latest_day_peak_grid_export"].state) == 0.5
    assert float(states[f"{meter}_der_solar_capacity"].state) == 4.0
    issues = states[f"{meter}_der_register_issues"]
    assert int(issues.state) == 3
    assert "decommissioned_records_listed" in issues.attributes["issues"]
    assert states["sensor.energy_home_plan"].state == "Wholesale"
    assert float(states["sensor.energy_home_month_to_date_charges"].state) == 12.5
    assert float(states["sensor.energy_home_month_to_date_credits"].state) == 4.0
    assert float(states["sensor.energy_home_last_statement_charges"].state) == 12.5
    statement = states["sensor.energy_home_last_statement_credits"]
    assert float(statement.state) == 4.0
    assert statement.attributes["net"] == 8.5
    assert float(states[f"{meter}_last_30_days_grid_import"].state) == 48.0
    assert float(states[f"{meter}_last_30_days_grid_export"].state) == 24.0
    history = states[f"{meter}_usage_history"]
    assert int(history.state) == 2
    assert f"openfeed:nmi_{NMI.lower()}_consumption" in history.attributes["statistics"]

    # The bills calendar holds one event per invoice.
    assert "calendar.energy_home_bills" in {s.entity_id for s in hass.states.async_all("calendar")}
    result = await hass.services.async_call(
        "calendar", "get_events",
        {"entity_id": "calendar.energy_home_bills", "start_date_time": "2026-08-01T00:00:00+10:00",
         "end_date_time": "2026-11-01T00:00:00+10:00"},
        blocking=True, return_response=True,
    )
    events = result["calendar.energy_home_bills"]["events"]
    assert [e["summary"] for e in events] == [
        "Energy bill AUD 100.00 (PAID)", "Energy bill AUD 88.10 (NOT_PAID)"]
    assert events[0]["start"] == "2026-09-01" and events[0]["end"] == "2026-09-16"

    # The meter device links to the Energy settings.
    from homeassistant.helpers import device_registry as dr
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, f"meter_nmi_{NMI.lower()}"), entry.entry_id)
    assert device.configuration_url == "homeassistant://config/energy"
    invoice = next(s for e, s in states.items() if "invoice_amount" in e)
    assert float(invoice.state) == 88.10
    assert states["sensor.energy_home_latest_invoice_status"].state == "NOT_PAID"
    assert states["sensor.bank_everyday_current_balance"].state == "unavailable"

    await async_wait_recording_done(hass)
    stat_id = f"openfeed:nmi_{NMI.lower()}_consumption"
    last = await hass.async_add_executor_job(get_last_statistics, hass, 1, stat_id, True, {"sum"})
    assert last[stat_id][0]["sum"] == pytest.approx(48.0)
    power_id = f"openfeed:nmi_{NMI.lower()}_consumption_power"
    last = await hass.async_add_executor_job(get_last_statistics, hass, 1, power_id, True, {"max", "mean"})
    assert last[power_id][0]["max"] == pytest.approx(1.0)
    stats = await hass.async_add_executor_job(list_statistic_ids, hass)
    ids = {s["statistic_id"] for s in stats}
    assert any(i.endswith("_charges") for i in ids)
    assert any(i.endswith("_invoiced") for i in ids)
    assert any(i.endswith("_payments") for i in ids)
    assert any(i.endswith("_credits") for i in ids)
    # The legacy signed payments series was rebuilt once.
    assert entry.data["money_stats_version"] == 2

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
    for secret in ("BEGIN PRIVATE KEY", "'rt'", NMI, ACC, METER, "user-1", "Home", "Feed-in credit"):
        assert secret not in text
    lines = diag["energy"]["account_0"]["billing_lines"]
    assert {line["amount"] for line in lines} == {12.5, -4.0, -100.0}
    totals = diag["energy"]["account_0"]["history"]["series_totals"]
    assert totals["payments"] == 100.0
    assert "refunds" not in totals


async def test_plan_sensor_removed_when_no_plan(hass, entry, aioclient_mock) -> None:
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.unique_id}_{ACC}_plan", config_entry=entry
    )
    _mock_api_no_plan(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    assert hass.states.get("sensor.energy_home_month_to_date_charges") is not None


def _mock_api_no_plan(aioclient_mock):
    _mock_api(aioclient_mock)
    a = f"{API}/v1/energy/accounts/{ACC}"
    # The first registered mock wins, so replace the detail mock.
    aioclient_mock._mocks = [m for m in aioclient_mock._mocks if str(m.url) != a]
    aioclient_mock.get(a, json={"data": {"accountId": ACC, "plans": None}})


async def test_sidebar_dashboard(hass, entry, aioclient_mock, hass_ws_client) -> None:
    from homeassistant.components.frontend import DATA_PANELS
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "lovelace", {})
    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    panel = hass.data[DATA_PANELS]["openfeed"]
    assert panel.sidebar_title == "Energy data"
    assert panel.component_name == "lovelace"

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "lovelace/config", "url_path": "openfeed"})
    msg = await client.receive_json()
    assert msg["success"], msg
    config = msg["result"]
    cards = config["views"][0]["cards"]
    titles = [c.get("title") for c in cards]
    assert "Grid power, hourly mean and peak (last 7 days)" in titles
    assert "Monthly charges, credits and payments" in titles
    # Every entity the dashboard names exists, and no NMI leaks into names.
    states = {s.entity_id for s in hass.states.async_all()}
    for card in cards:
        for row in card.get("entities", []):
            ref = row["entity"] if isinstance(row, dict) else row
            if ref.startswith("openfeed:"):
                continue
            assert ref in states, ref
            if isinstance(row, dict):
                assert NMI not in row["name"]
    power = next(c for c in cards if c["title"].startswith("Grid power"))
    assert power["entities"][0]["entity"] == f"openfeed:nmi_{NMI.lower()}_consumption_power"

    # Switching the option off removes the panel on reload.
    hass.config_entries.async_update_entry(entry, options={"sidebar_dashboard": False})
    await hass.async_block_till_done()
    assert "openfeed" not in hass.data[DATA_PANELS]


async def test_dashboard_skipped_without_lovelace(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
