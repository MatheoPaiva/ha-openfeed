"""Billing transactions and invoices into money series."""

from datetime import UTC, date, datetime

from custom_components.openfeed.billing import CHARGES, INVOICED, PAYMENTS, summarise_billing


def test_summarise_billing() -> None:
    tx = [
        {"executionDateTime": "2026-11-02T10:15:00+10:00", "transactionUType": "usage", "amount": 20.0},
        {"executionDateTime": "2026-11-02T10:45:00+10:00", "transactionUType": "usage", "amount": -5.5},
        {"executionDateTime": "2026-11-03T08:00:00Z", "transactionUType": "otherCharges", "amount": 1.0},
        {"executionDateTime": "2026-10-20T09:00:00+10:00", "transactionUType": "usage", "amount": 7.0},
        {"executionDateTime": "2026-10-25T09:00:00+10:00", "transactionUType": "payment", "amount": -50.0},
        {"executionDateTime": None, "amount": 3.0},
    ]
    invoices = [
        {"issueDate": "2026-11-04", "invoiceAmount": 14.59},
        {"issueDate": "2026-10-04", "invoiceAmount": "30.10"},
    ]
    s = summarise_billing(tx, invoices, date(2026, 11, 14))
    assert s.transactions == 5
    assert s.invoices == 2
    assert s.month_to_date_charges == 15.5
    assert s.by_type == {"otherCharges": 1.0, "usage": 14.5}
    assert s.oldest_transaction == date(2026, 10, 20)
    assert s.oldest_invoice == date(2026, 10, 4)
    assert s.hourly[CHARGES][datetime(2026, 11, 2, 0, tzinfo=UTC)] == 14.5
    assert s.hourly[PAYMENTS][datetime(2026, 10, 24, 23, tzinfo=UTC)] == 50.0
    # Invoices book at 00:00 AEST on the issue date.
    assert s.hourly[INVOICED][datetime(2026, 11, 3, 14, tzinfo=UTC)] == 14.59


def test_no_transactions() -> None:
    s = summarise_billing([], [], date(2026, 11, 14))
    assert s.month_to_date_charges is None
    assert s.hourly == {}
