"""Invoice calendar events."""

from datetime import date

from custom_components.openfeed.calendar import invoice_event


def test_bill_event_spans_issue_to_due() -> None:
    e = invoice_event(
        {"invoiceNumber": "INV-1", "issueDate": "2026-11-04", "dueDate": "2026-11-11",
         "invoiceAmount": 14.59, "gstAmount": 16.11, "balanceAtIssue": -4.49, "paymentStatus": "PAID"},
        "Amber", "AUD",
    )
    assert e.start == date(2026, 11, 4)
    assert e.end == date(2026, 11, 12)
    assert e.summary == "Amber bill AUD 14.59 (PAID)"
    assert "Balance at issue: -4.49" in e.description
    assert "INV-1" not in e.uid and "INV-1" not in e.description


def test_credit_and_missing_dates() -> None:
    e = invoice_event({"issueDate": "2024-12-03", "invoiceAmount": -399.12}, "Amber", "AUD")
    assert e.summary == "Amber credit AUD 399.12"
    assert e.end == date(2024, 12, 4)
    assert invoice_event({"invoiceAmount": 1}, "Amber", "AUD") is None
