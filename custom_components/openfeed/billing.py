"""Turn openfeed billing transactions and invoices into money series.

Billing transactions are append-only line items (usage, demand, onceOff,
otherCharges, payment). The integration keeps these hourly series per
account, booked at each line's executionDateTime:

* charges   positive non-payment line items (gross charges)
* credits   negative non-payment line items, as a positive amount credited
* payments  payment line items, signed exactly as the retailer reports them,
            so a refund paid out and a payment received have opposite signs
* invoiced  invoice totals, booked at 00:00 AEST on the issue date

The raw line items (date, type, amount, GST) are kept for diagnostics.
Free-text descriptions are never kept.

This module has no Home Assistant imports so it can be unit tested alone.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from .usage import AEST

CHARGES = "charges"
CREDITS = "credits"
PAYMENTS = "payments"
INVOICED = "invoiced"
PAYMENT_TYPE = "payment"
MAX_LINES = 2000


@dataclass(slots=True)
class BillingSummary:
    """Hourly money series, month-to-date totals and line items for one account."""

    hourly: dict[str, dict[datetime, float]] = field(default_factory=dict)
    transactions: int = 0
    invoices: int = 0
    oldest_transaction: date | None = None
    oldest_invoice: date | None = None
    month_to_date_charges: float | None = None
    month_to_date_credits: float | None = None
    charges_by_type: dict[str, float] = field(default_factory=dict)
    credits_by_type: dict[str, float] = field(default_factory=dict)
    # The latest AEST date with charge or credit lines, and its totals.
    last_statement_date: date | None = None
    last_statement_charges: float | None = None
    last_statement_credits: float | None = None
    # Newest first: {"date", "type", "amount", "gst", "has_invoice", "has_meter"}
    lines: list[dict[str, Any]] = field(default_factory=list)


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


def _round(values: dict[str, float]) -> dict[str, float]:
    return {k: round(v, 2) for k, v in sorted(values.items())}


def _last_statement(summary: BillingSummary) -> None:
    """Totals for the latest AEST date that carries charge or credit lines.

    Retailers usually date every line on the statement date, so this is the
    most recent statement.
    """
    per_day: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for index, series in enumerate((CHARGES, CREDITS)):
        for hour, amount in summary.hourly.get(series, {}).items():
            per_day[hour.astimezone(AEST).date()][index] += amount
    if not per_day:
        return
    day = max(per_day)
    summary.last_statement_date = day
    summary.last_statement_charges = round(per_day[day][0], 2)
    summary.last_statement_credits = round(per_day[day][1], 2)


def summarise_billing(
    transactions: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
    today: date,
) -> BillingSummary:
    """Aggregate billing transactions and invoices into hourly series."""
    summary = BillingSummary()
    hourly: dict[str, dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    month_start = today.replace(day=1)
    charges_mtd: dict[str, float] = defaultdict(float)
    credits_mtd: dict[str, float] = defaultdict(float)
    lines: list[tuple[datetime, dict[str, Any]]] = []
    for tx in transactions:
        when = _parse_dt(tx.get("executionDateTime"))
        amount = _num(tx.get("amount"))
        if when is None or amount is None:
            continue
        summary.transactions += 1
        kind = str(tx.get("transactionUType") or "other")
        lines.append(
            (
                when,
                {
                    "date": when.astimezone(AEST).isoformat(),
                    "type": kind,
                    "amount": amount,
                    "gst": _num(tx.get("gst")),
                    "has_invoice": bool(tx.get("invoiceNumber")),
                    "has_meter": bool(tx.get("meterId")),
                },
            )
        )
        local_day = when.astimezone(AEST).date()
        if summary.oldest_transaction is None or local_day < summary.oldest_transaction:
            summary.oldest_transaction = local_day
        hour = when.replace(minute=0, second=0, microsecond=0)
        if kind.lower() == PAYMENT_TYPE:
            hourly[PAYMENTS][hour] += amount
            continue
        this_month = local_day >= month_start
        if amount >= 0:
            hourly[CHARGES][hour] += amount
            if this_month:
                charges_mtd[kind] += amount
        else:
            hourly[CREDITS][hour] += -amount
            if this_month:
                credits_mtd[kind] += -amount
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
    _last_statement(summary)
    summary.lines = [line for _when, line in sorted(lines, key=lambda x: x[0], reverse=True)][
        :MAX_LINES
    ]
    if summary.transactions:
        summary.charges_by_type = _round(charges_mtd)
        summary.credits_by_type = _round(credits_mtd)
        summary.month_to_date_charges = round(sum(charges_mtd.values()), 2)
        summary.month_to_date_credits = round(sum(credits_mtd.values()), 2)
    return summary
