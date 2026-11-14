"""Interval read aggregation."""

from __future__ import annotations

from datetime import UTC, date, datetime

from custom_components.openfeed.usage import (
    CONSUMPTION,
    CONTROLLED_LOAD,
    EXPORT,
    cumulative,
    summarise,
    usage_windows,
)


def _record(day, values, suffix="E1", controlled=None, unit="KWH", qualities=None):
    read = {
        "meterId": "M1", "registerId": "R", "registerSuffix": suffix,
        "readStartDate": day, "unitOfMeasure": unit, "readUType": "intervalRead",
        "intervalRead": {"readIntervalLength": 30, "intervalReads": values,
                         "readQualities": qualities or []},
    }
    if controlled is not None:
        read["controlledLoad"] = controlled
    return {"meterId": "M1", "intervalDate": day, "reads": [read]}


def test_aest_midnight_origin_and_hourly_sum():
    s = summarise([_record("2026-11-10", [0.5, 0.25, 1.0])])
    # Midnight AEST is 14:00 UTC the previous day; first two half hours share an hour.
    hours = s.hourly[CONSUMPTION]
    assert hours[datetime(2026, 11, 9, 14, tzinfo=UTC)] == 0.75
    assert hours[datetime(2026, 11, 9, 15, tzinfo=UTC)] == 1.0
    assert s.daily[CONSUMPTION][date(2026, 11, 10)] == 1.75


def test_negative_values_are_export():
    s = summarise([_record("2026-11-10", [1.0, -2.0])])
    assert sum(s.hourly[CONSUMPTION].values()) == 1.0
    assert sum(s.hourly[EXPORT].values()) == 2.0


def test_b_suffix_positive_is_export():
    s = summarise([_record("2026-11-10", [3.0], suffix="B1")])
    assert sum(s.hourly[EXPORT].values()) == 3.0
    assert CONSUMPTION not in s.hourly


def test_controlled_load_series():
    s = summarise([_record("2026-11-10", [1.5], suffix="E2", controlled=True)])
    assert sum(s.hourly[CONTROLLED_LOAD].values()) == 1.5


def test_wh_scaled_and_duplicates_deduplicated():
    rec = _record("2026-11-10", [500.0], unit="WH")
    s = summarise([rec, rec])
    assert sum(s.hourly[CONSUMPTION].values()) == 0.5


def test_quality_counts_and_basic_reads_skipped():
    rec = _record("2026-11-10", [1, 1, 1], qualities=[{"startInterval": 2, "endInterval": 3,
                                                       "quality": "SUBSTITUTE"}])
    basic = {"meterId": "M1", "intervalDate": "2026-11-10",
             "reads": [{"readUType": "basicRead", "basicRead": {"value": 9}}]}
    s = summarise([rec, basic])
    assert s.substituted_intervals == 2
    assert s.skipped_reads == 1


def test_windows_are_at_most_30_days():
    w = usage_windows(date(2026, 1, 1), date(2026, 3, 1), 30)
    assert w[0] == (date(2026, 1, 1), date(2026, 1, 30))
    assert w[-1][1] == date(2026, 3, 1)
    assert all((b - a).days < 30 for a, b in w)


def test_cumulative_from_base():
    h = {datetime(2026, 1, 1, 1, tzinfo=UTC): 1.0, datetime(2026, 1, 1, 0, tzinfo=UTC): 2.0}
    rows = cumulative(h, 10.0)
    assert [r[2] for r in rows] == [12.0, 13.0]
