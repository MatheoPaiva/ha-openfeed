"""Summarise the AEMO DER Register record openfeed shares for a meter.

The record comes from AEMO's DER Register via the retailer. Networks
maintain it from the connection applications installers lodge, so it can
drift from what is on site. The summary flags common gaps so the owner
can ask the network to correct it.

This module has no Home Assistant imports so it can be unit tested alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ACTIVE = "ACTIVE"
STORAGE = "STORAGE"
SOLAR_PV = "SOLAR_PV"
_BATTERY_WORDS = ("powerwall", "battery", "storage")

ISSUE_DECOMMISSIONED = "decommissioned_records_listed"
ISSUE_BATTERY_NO_STORAGE = "battery_inverter_without_storage_device"
ISSUE_PV_ON_BATTERY = "solar_panels_on_battery_inverter"
ISSUE_OVER_APPROVED = "active_inverters_exceed_approved_capacity"
ISSUE_NO_ACTIVE = "no_active_equipment"


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _count(value: Any) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


@dataclass(slots=True)
class DerSummary:
    """Totals and data-quality flags for one DER record."""

    active_pv_kw: float = 0.0
    active_inverter_kw: float = 0.0
    active_storage_kwh: float = 0.0
    active_inverters: int = 0
    decommissioned_connections: int = 0
    approved_capacity: float | None = None
    inverters: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _looks_like_battery(conn: dict[str, Any]) -> bool:
    """True when the inverter description names a battery product."""
    text = " ".join(
        str(conn.get(k) or "")
        for k in ("inverterSeries", "inverterModelNumber", "manufacturerName")
    ).lower()
    return any(word in text for word in _BATTERY_WORDS)


def summarise_der(der: dict[str, Any] | None) -> DerSummary | None:
    """Totals and flags for a DER record, or None when there is no record.

    Device counts are taken as reported per AC connection; they are not
    multiplied by the connection count, because registers record panel
    counts for the whole connection.
    """
    if not der:
        return None
    s = DerSummary(approved_capacity=_num(der.get("approvedCapacity")) or None)
    battery_without_storage = False
    pv_on_battery = False
    for conn in der.get("acConnections") or []:
        status = str(conn.get("status") or "").upper()
        devices = conn.get("derDevices") or []
        count = _count(conn.get("count"))
        s.inverters.append(
            {
                "manufacturer": conn.get("manufacturerName"),
                "model": conn.get("inverterModelNumber"),
                "series": conn.get("inverterSeries"),
                "count": count,
                "capacity_kw": _num(conn.get("inverterDeviceCapacity")),
                "status": status or None,
                "commissioned": conn.get("commissioningDate"),
                "devices": [
                    {
                        "type": d.get("type"),
                        "manufacturer": d.get("manufacturer"),
                        "model": d.get("modelNumber"),
                        "count": _count(d.get("count")),
                        "rated_kw": _num(d.get("nominalRatedCapacity")),
                        "storage_kwh": _num(d.get("nominalStorageCapacity")),
                        "status": d.get("status"),
                    }
                    for d in devices
                ],
            }
        )
        if status != ACTIVE:
            s.decommissioned_connections += 1
            continue
        s.active_inverters += count
        s.active_inverter_kw += count * _num(conn.get("inverterDeviceCapacity"))
        has_storage = False
        has_pv = False
        for d in devices:
            if str(d.get("status") or ACTIVE).upper() != ACTIVE:
                continue
            kind = str(d.get("type") or "").upper()
            n = _count(d.get("count"))
            if kind == SOLAR_PV:
                has_pv = True
                s.active_pv_kw += n * _num(d.get("nominalRatedCapacity"))
            storage = n * _num(d.get("nominalStorageCapacity"))
            if kind == STORAGE or storage > 0:
                has_storage = True
                s.active_storage_kwh += storage
        if _looks_like_battery(conn):
            battery_without_storage |= not has_storage
            pv_on_battery |= has_pv
    s.active_pv_kw = round(s.active_pv_kw, 3)
    s.active_inverter_kw = round(s.active_inverter_kw, 3)
    s.active_storage_kwh = round(s.active_storage_kwh, 3)
    if s.decommissioned_connections:
        s.issues.append(ISSUE_DECOMMISSIONED)
    if battery_without_storage:
        s.issues.append(ISSUE_BATTERY_NO_STORAGE)
    if pv_on_battery:
        s.issues.append(ISSUE_PV_ON_BATTERY)
    if s.approved_capacity and s.active_inverter_kw > s.approved_capacity + 1e-6:
        s.issues.append(ISSUE_OVER_APPROVED)
    if not s.active_inverters:
        s.issues.append(ISSUE_NO_ACTIVE)
    return s
