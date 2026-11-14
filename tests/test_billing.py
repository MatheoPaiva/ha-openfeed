"""Billing transactions and invoices into money series."""

from datetime import UTC, date, datetime

from custom_components.openfeed.billing import (
    CHARGES,
    CREDITS,
    INVOICED,
    PAYMENTS,
    summarise_billing,
)


def test_summarise_billing() -> None:
    tx = [
        {"executionDateTime": "2026-11-02T10:15:00+10:00", "transactionUType": "usage", "amount": 20.0,
         "gst": 1.82, "invoiceNumber": "INV1", "meterId": "M1", "description": "General usage"},
        {"executionDateTime": "2026-11-02T10:45:00+10:00", "transactionUType": "usage", "amount": -5.5},
        {"executionDateTime": "2026-11-03T08:00:00Z", "transactionUType": "otherCharges", "amount": 1.0},
        {"executionDateTime": "2026-10-20T09:00:00+10:00", "transactionUType": "usage", "amount": 7.0},
        {"executionDateTime": "2026-10-25T09:00:00+10:00", "transactionUType": "payment", "amount": 50.0},
        {"executionDateTime": "2026-10-26T09:00:00+10:00", "transactionUType": "payment", "amount": -30.0},
        {"executionDateTime": None, "amount": 3.0},
    ]
    invoices = [
        {"issueDate": "2026-11-04", "invoiceAmount": 14.59},
        {"issueDate": "2026-10-04", "invoiceAmount": "30.10"},
    ]
    s = summarise_billing(tx, invoices, date(2026, 11, 14))
    assert s.transactions == 6
    assert s.invoices == 2
    assert s.month_to_date_charges == 21.0
    assert s.month_to_date_credits == 5.5
    assert s.charges_by_type == {"otherCharges": 1.0, "usage": 20.0}
    assert s.credits_by_type == {"usage": 5.5}
    assert s.oldest_transaction == date(2026, 10, 20)
    assert s.oldest_invoice == date(2026, 10, 4)
    hour = datetime(2026, 11, 2, 0, tzinfo=UTC)
    assert s.hourly[CHARGES][hour] == 20.0
    assert s.hourly[CREDITS][hour] == 5.5
    # Payments keep the retailer's sign, so a refund nets against a payment.
    assert s.hourly[PAYMENTS][datetime(2026, 10, 24, 23, tzinfo=UTC)] == 50.0
    assert s.hourly[PAYMENTS][datetime(2026, 10, 25, 23, tzinfo=UTC)] == -30.0
    # Invoices book at 00:00 AEST on the issue date.
    assert s.hourly[INVOICED][datetime(2026, 11, 3, 14, tzinfo=UTC)] == 14.59


def test_line_items_newest_first_without_identifiers() -> None:
    tx = [
        {"executionDateTime": "2026-10-01T09:00:00+10:00", "transactionUType": "usage", "amount": 1.0},
        {"executionDateTime": "2026-11-01T09:00:00+10:00", "transactionUType": "usage", "amount": 2.0,
         "gst": 0.18, "invoiceNumber": "INV9", "meterId": "M9", "description": "Secret label"},
    ]
    s = summarise_billing(tx, [], date(2026, 11, 14))
    assert [line["amount"] for line in s.lines] == [2.0, 1.0]
    first = s.lines[0]
    assert first == {
        "date": "2026-11-01T09:00:00+10:00", "type": "usage", "amount": 2.0, "gst": 0.18,
        "has_invoice": True, "has_meter": True,
    }
    assert "Secret label" not in str(s.lines)
    assert "INV9" not in str(s.lines)


def test_no_transactions() -> None:
    s = summarise_billing([], [], date(2026, 11, 14))
    assert s.month_to_date_charges is None
    assert s.month_to_date_credits is None
    assert s.hourly == {}
    assert s.lines == []
