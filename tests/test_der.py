"""DER Register summary and data-quality flags."""

from custom_components.openfeed.der import (
    ISSUE_BATTERY_NO_STORAGE,
    ISSUE_DECOMMISSIONED,
    ISSUE_NO_ACTIVE,
    ISSUE_OVER_APPROVED,
    ISSUE_PV_ON_BATTERY,
    summarise_der,
)

# Shape of a real Amber record (identifiers removed).
REAL = {
    "approvedCapacity": 19,
    "acConnections": [
        {"count": 1, "manufacturerName": "Solax Power Co Ltd", "inverterSeries": "SL-TL",
         "inverterDeviceCapacity": 4.6, "status": "DECOMMISSIONED",
         "derDevices": [{"count": 1, "type": "SOLAR_PV", "nominalRatedCapacity": 4.6,
                         "status": "DECOMMISSIONED"}]},
        {"count": 1, "manufacturerName": "Tesla Motors Australia Pty Ltd",
         "inverterSeries": "Tesla Powerwall", "inverterModelNumber": "AC Powerwall",
         "inverterDeviceCapacity": 5, "status": "ACTIVE",
         "derDevices": [{"count": 25, "type": "SOLAR_PV", "nominalRatedCapacity": 0.37,
                         "nominalStorageCapacity": 0, "status": "ACTIVE"}]},
        {"count": 2, "manufacturerName": "SolarEdge Technologies Ltd",
         "inverterSeries": "Three Phase Inverter", "inverterDeviceCapacity": 7, "status": "ACTIVE",
         "derDevices": [{"count": 25, "type": "SOLAR_PV", "nominalRatedCapacity": 0.37,
                         "status": "ACTIVE"}]},
    ],
}


def test_real_record_totals_and_flags() -> None:
    s = summarise_der(REAL)
    assert s.active_pv_kw == 18.5
    assert s.active_inverter_kw == 19.0
    assert s.active_inverters == 3
    assert s.active_storage_kwh == 0
    assert s.decommissioned_connections == 1
    assert s.issues == [ISSUE_DECOMMISSIONED, ISSUE_BATTERY_NO_STORAGE, ISSUE_PV_ON_BATTERY]
    assert len(s.inverters) == 3


def test_clean_record_with_storage() -> None:
    s = summarise_der({
        "approvedCapacity": 10,
        "acConnections": [
            {"count": 1, "inverterSeries": "Hybrid", "inverterDeviceCapacity": 10, "status": "ACTIVE",
             "derDevices": [
                 {"count": 20, "type": "SOLAR_PV", "nominalRatedCapacity": 0.4, "status": "ACTIVE"},
                 {"count": 1, "type": "STORAGE", "nominalStorageCapacity": 13.5, "status": "ACTIVE"},
             ]},
        ],
    })
    assert s.active_pv_kw == 8.0
    assert s.active_storage_kwh == 13.5
    assert s.issues == []


def test_over_approved_and_empty() -> None:
    s = summarise_der({"approvedCapacity": 5, "acConnections": [
        {"count": 2, "inverterDeviceCapacity": 5, "status": "ACTIVE", "derDevices": []}]})
    assert ISSUE_OVER_APPROVED in s.issues
    assert summarise_der({"approvedCapacity": 0, "acConnections": []}).issues == [ISSUE_NO_ACTIVE]
    assert summarise_der(None) is None
