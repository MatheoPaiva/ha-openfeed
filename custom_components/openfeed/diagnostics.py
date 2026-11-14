"""Diagnostics for openfeed, with tokens, keys and identifiers redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_APP_KEY, CONF_DPOP_KEY, CONF_SUBJECT, CONF_TOKEN
from .coordinator import OpenfeedConfigEntry

TO_REDACT = {
    CONF_APP_KEY,
    CONF_DPOP_KEY,
    CONF_TOKEN,
    CONF_SUBJECT,
    "access_token",
    "refresh_token",
    "accountNumber",
    "maskedNumber",
    "nationalMeteringId",
    "accountId",
    "meterId",
    "servicePointId",
    "invoiceNumber",
    "grant_id",
    "client_id",
    "app_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OpenfeedConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics. Usage series are summarised, not dumped."""
    coordinator = entry.runtime_data
    data = coordinator.data
    energy = {}
    for index, account in enumerate(data.energy.values()):
        meters = []
        for meter in account.meters.values():
            usage = meter.usage
            meters.append(
                {
                    "meter": meter.meter,
                    "der": meter.der,
                    "usage": None
                    if usage is None
                    else {
                        "series": {s: len(v) for s, v in usage.hourly.items()},
                        "latest_day": str(usage.latest_complete_day),
                        "substituted_intervals": usage.substituted_intervals,
                        "skipped_reads": usage.skipped_reads,
                        "interval_minutes": usage.interval_minutes,
                    },
                    "usage_fetched_at": str(meter.usage_fetched_at),
                }
            )
        energy[f"account_{index}"] = {
            "account": account.account,
            "balance_status": account.balance_status,
            "latest_invoice": account.latest_invoice,
            "meters": meters,
        }
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "fetched_at": str(data.fetched_at),
            "energy": energy,
            "banking": {
                f"account_{i}": {"account": b.account, "balance_status": b.balance_status}
                for i, b in enumerate(data.banking.values())
            },
            "last_update_success": coordinator.last_update_success,
        },
        TO_REDACT,
    )
