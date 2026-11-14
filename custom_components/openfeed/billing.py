"""Turn openfeed billing transactions and invoices into money series.

Billing transactions are append-only line items (usage, demand, onceOff,
otherCharges, payment). Charges are positive and credits negative. The
integration keeps two hourly series per account:

* charges   every non-payment line item, net of credits
* payments  payment line items, as a positive amount paid

Invoices become a third series, invoiced, booked at 00:00 AEST on the
issue date.

This module has no Home Assistant imports so it can be unit tested alone.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from .usage import AEST

CHARGES = "charges"
PAYMENTS = "payments"
INVOICED = "invoiced"


@dataclass(slots=True)
class BillingSummary:
    """Hourly money series and month-to-date totals for one account."""

    hourly: dict[str, dict[datetime, float]] = field(default_factory=dict)
    transactions: int = 0
    invoices: int = 0
    oldest_transaction: date | None = None
    oldest_invoice: date | None = None
    month_to_date_charges: float | None = None
    by_type: dict[str, float] = field(default_factory=dict)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), datetime.min.time(), AEST)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=AEST)
    return parsed.astimezone(UTC)


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def summarise_billing(
    transactions: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
    today: date,
) -> BillingSummary:
    """Aggregate billing transactions and invoices into hourly series."""
    summary = BillingSummary()
    hourly: dict[str, dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    month_start = today.replace(day=1)
    mtd = 0.0
    by_type: dict[str, float] = defaultdict(float)
    for tx in transactions:
        when = _parse_dt(tx.get("executionDateTime"))
        amount = _num(tx.get("amount"))
        if when is None or amount is None:
            continue
        summary.transactions += 1
        local_day = when.astimezone(AEST).date()
        if summary.oldest_transaction is None or local_day < summary.oldest_transaction:
            summary.oldest_transaction = local_day
        hour = when.replace(minute=0, second=0, microsecond=0)
        kind = str(tx.get("transactionUType") or "other")
        if kind.lower() == "payment":
            hourly[PAYMENTS][hour] += abs(amount)
            continue
        hourly[CHARGES][hour] += amount
        if local_day >= month_start:
            mtd += amount
            by_type[kind] += amount
    for invoice in invoices:
        issued = _parse_dt(invoice.get("issueDate"))
        amount = _num(invoice.get("invoiceAmount"))
        if issued is None or amount is None:
            continue
        summary.invoices += 1
        local_day = issued.astimezone(AEST).date()
        if summary.oldest_invoice is None or local_day < summary.oldest_invoice:
            summary.oldest_invoice = local_day
        hourly[INVOICED][issued.replace(minute=0, second=0, microsecond=0)] += amount
    summary.hourly = {s: dict(sorted(v.items())) for s, v in hourly.items() if v}
    if summary.transactions:
        summary.month_to_date_charges = round(mtd, 2)
        summary.by_type = {k: round(v, 2) for k, v in sorted(by_type.items())}
    return summary
