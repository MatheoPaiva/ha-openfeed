"""Turn openfeed interval meter reads into hourly and daily energy series.

openfeed mirrors the Consumer Data Standards meter read: each read covers a
register from midnight AEST (a fixed UTC+10 offset, no daylight saving) on
readStartDate, with interval values of readIntervalLength minutes.
Positive values are consumption and negative values are export.

The integration splits every register into three series:

* consumption  energy imported from the grid on general registers
* export       energy exported to the grid
* controlled_load  energy on controlled-load registers

This module has no Home Assistant imports so it can be unit tested alone.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

AEST = timezone(timedelta(hours=10), "AEST")

CONSUMPTION = "consumption"
EXPORT = "export"
CONTROLLED_LOAD = "controlled_load"
SERIES = (CONSUMPTION, EXPORT, CONTROLLED_LOAD)

_UNIT_SCALE = {"KWH": 1.0, "WH": 0.001, "MWH": 1000.0}


@dataclass(slots=True)
class UsageSummary:
    """Hourly and daily kWh per series, plus data-quality counts."""

    # series -> UTC hour start -> kWh
    hourly: dict[str, dict[datetime, float]] = field(default_factory=dict)
    # series -> AEST date -> kWh
    daily: dict[str, dict[date, float]] = field(default_factory=dict)
    substituted_intervals: int = 0
    skipped_reads: int = 0
    interval_minutes: int | None = None

    @property
    def latest_complete_day(self) -> date | None:
        """The latest AEST date with data in any series."""
        days = [d for s in self.daily.values() for d in s]
        return max(days) if days else None


def _register_key(read: dict[str, Any]) -> str:
    return "|".join(
        str(read.get(k) or "") for k in ("meterId", "registerId", "registerSuffix")
    )


def _series_for(read: dict[str, Any], value: float) -> tuple[str, float] | None:
    if value == 0:
        return None
    if read.get("controlledLoad"):
        return CONTROLLED_LOAD, abs(value)
    suffix = str(read.get("registerSuffix") or "").upper()
    # NEM12 suffixes starting with B are export registers. Some holders
    # report them as positive values, so trust the suffix over the sign.
    if suffix.startswith("B"):
        return EXPORT, abs(value)
    if value < 0:
        return EXPORT, -value
    return CONSUMPTION, value


def summarise(records: list[dict[str, Any]]) -> UsageSummary:
    """Aggregate EnergyUsage records into hourly and daily series.

    Duplicate intervals for the same register (overlapping pages or
    windows) are de-duplicated, the last value winning.
    """
    summary = UsageSummary()
    # (register, interval start UTC) -> (series, kWh)
    intervals: dict[tuple[str, datetime], tuple[str, float]] = {}

    for record in records:
        for read in record.get("reads") or []:
            if read.get("readUType") != "intervalRead" or not read.get("intervalRead"):
                summary.skipped_reads += 1
                continue
            scale = _UNIT_SCALE.get(str(read.get("unitOfMeasure") or "KWH").upper())
            start_text = read.get("readStartDate") or record.get("intervalDate")
            interval = read["intervalRead"]
            length = interval.get("readIntervalLength")
            if scale is None or not start_text or not length:
                summary.skipped_reads += 1
                continue
            start_day = date.fromisoformat(str(start_text)[:10])
            origin = datetime.combine(start_day, datetime.min.time(), AEST)
            summary.interval_minutes = int(length)
            for quality in interval.get("readQualities") or []:
                first = int(quality.get("startInterval", 1))
                last = int(quality.get("endInterval", first))
                summary.substituted_intervals += max(0, last - first + 1)
            register = _register_key(read)
            for index, raw in enumerate(interval.get("intervalReads") or []):
                if raw is None:
                    continue
                start = (origin + timedelta(minutes=index * int(length))).astimezone(UTC)
                classified = _series_for(read, float(raw) * scale)
                if classified is None:
                    intervals.pop((register, start), None)
                    continue
                intervals[(register, start)] = classified

    hourly: dict[str, dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    daily: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for (_register, start), (series, kwh) in intervals.items():
        hour = start.replace(minute=0, second=0, microsecond=0)
        hourly[series][hour] += kwh
        daily[series][start.astimezone(AEST).date()] += kwh

    summary.hourly = {s: dict(sorted(v.items())) for s, v in hourly.items()}
    summary.daily = {s: dict(sorted(v.items())) for s, v in daily.items()}
    return summary


def usage_windows(oldest: date, newest: date, window_days: int) -> list[tuple[date, date]]:
    """Split a date range into inclusive windows of at most window_days."""
    windows: list[tuple[date, date]] = []
    start = oldest
    while start <= newest:
        end = min(newest, start + timedelta(days=window_days - 1))
        windows.append((start, end))
        start = end + timedelta(days=1)
    return windows


def cumulative(hourly: dict[datetime, float], base: float) -> list[tuple[datetime, float, float]]:
    """(hour, state, running sum) rows for recorder statistics."""
    rows = []
    total = base
    for hour, kwh in sorted(hourly.items()):
        total += kwh
        rows.append((hour, kwh, total))
    return rows
