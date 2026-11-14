"""A bills calendar per energy account: one event per invoice.

Each invoice is an all-day event from its issue date to its due date, so
the calendar shows when every bill arrived, when it is due and how much
it was.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .coordinator import OpenfeedConfigEntry, OpenfeedCoordinator
from .sensor import energy_device_info


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def invoice_event(invoice: dict[str, Any], provider: str, currency: str) -> CalendarEvent | None:
    """An all-day event spanning issue date to due date (inclusive)."""
    issued = _date(invoice.get("issueDate"))
    due = _date(invoice.get("dueDate"))
    if issued is None and due is None:
        return None
    start = issued or due
    end = (due if due and due >= start else start) + timedelta(days=1)
    amount = _num(invoice.get("invoiceAmount"))
    status = invoice.get("paymentStatus")
    if amount is None:
        title = f"{provider} bill"
    elif amount < 0:
        title = f"{provider} credit {currency} {-amount:.2f}"
    else:
        title = f"{provider} bill {currency} {amount:.2f}"
    if status:
        title = f"{title} ({status})"
    lines = [
        f"Issued: {issued.isoformat()}" if issued else None,
        f"Due: {due.isoformat()}" if due else None,
        f"Amount: {amount:.2f}" if amount is not None else None,
        f"GST: {_num(invoice.get('gstAmount')):.2f}"
        if _num(invoice.get("gstAmount")) is not None
        else None,
        f"Balance at issue: {_num(invoice.get('balanceAtIssue')):.2f}"
        if _num(invoice.get("balanceAtIssue")) is not None
        else None,
        f"Status: {status}" if status else None,
    ]
    # A stable UID that does not expose the invoice number.
    uid = hashlib.sha256(str(invoice.get("invoiceNumber") or start).encode()).hexdigest()[:16]
    return CalendarEvent(
        start=start,
        end=end,
        summary=title,
        description="\n".join(line for line in lines if line),
        uid=f"openfeed-{uid}",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenfeedConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One bills calendar per energy account."""
    coordinator = entry.runtime_data
    async_add_entities(
        BillsCalendar(coordinator, account_id) for account_id in coordinator.data.energy
    )


class BillsCalendar(CoordinatorEntity[OpenfeedCoordinator], CalendarEntity):
    """Invoices as calendar events."""

    _attr_has_entity_name = True
    _attr_translation_key = "bills"
    _attr_attribution = "Consumer Data Right data via openfeed"

    def __init__(self, coordinator: OpenfeedCoordinator, account_id: str) -> None:
        super().__init__(coordinator)
        self._account_id = account_id
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{account_id}_bills"
        self._attr_device_info = energy_device_info(coordinator, account_id)

    def _events(self) -> list[CalendarEvent]:
        account = self.coordinator.data.energy.get(self._account_id)
        if account is None:
            return []
        provider = account.account.get("providerName") or "Energy"
        currency = (account.balance or {}).get("currency") or "AUD"
        events = [invoice_event(i, provider, currency) for i in account.invoices]
        return sorted((e for e in events if e), key=lambda e: e.start)

    @property
    def event(self) -> CalendarEvent | None:
        """The bill open today, else the next one."""
        today = dt_util.now().date()
        events = self._events()
        for e in events:
            if e.start <= today < e.end:
                return e
        upcoming = [e for e in events if e.start > today]
        if upcoming:
            return upcoming[0]
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        start = dt_util.as_local(start_date).date()
        end = dt_util.as_local(end_date).date()
        return [e for e in self._events() if e.start <= end and e.end > start]
