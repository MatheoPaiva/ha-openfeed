"""Evaluate a CDR energy plan's tariff detail at a point in time.

openfeed passes the plan through in the Consumer Data Standards shape
(EnergyPlanDetail): seasonal tariff periods, each with a single rate,
time-of-use rates or demand charges, plus solar feed-in tariffs. Unit
prices and daily supply charges are exclusive of GST; feed-in amounts are
paid as published.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
import re
from typing import Any

from .usage import AEST

GST_RATE = 0.10
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
SINGLE_RATE = "SINGLE_RATE"
TOU_TYPES = ("PEAK", "OFF_PEAK", "SHOULDER", "SHOULDER1", "SHOULDER2")
ENERGY_UNITS = (None, "", "KWH")
# Look this far ahead for the next tariff change, in these steps.
NEXT_CHANGE_HORIZON = timedelta(days=2)
NEXT_CHANGE_STEP = timedelta(minutes=5)

_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")


def amount(value: Any) -> float | None:
    """An AmountString as a float, else None."""
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def inc_gst(value: float | None) -> float | None:
    return None if value is None else round(value * (1 + GST_RATE), 6)


def _parse_time(value: Any) -> tuple[int, tzinfo | None] | None:
    """Minutes after midnight and an explicit offset, from an ISO 8601 time."""
    if not value:
        return None
    match = _TIME.match(str(value).strip())
    if not match:
        return None
    hour, minute, _sec, offset = match.groups()
    minutes = int(hour) * 60 + int(minute)
    tz: tzinfo | None = None
    if offset == "Z":
        tz = timezone.utc
    elif offset:
        sign = 1 if offset[0] == "+" else -1
        digits = offset[1:].replace(":", "")
        tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    return min(minutes, 1440), tz


def _zone(code: Any, local_tz: tzinfo) -> tzinfo:
    return AEST if str(code or "").upper() == "AEST" else local_tz


def window_matches(
    days: list[str] | None, start: Any, end: Any, now: datetime, default_tz: tzinfo
) -> bool:
    """Whether now falls in a day-of-week and time-of-day window.

    Missing start means midnight and missing end means end of day. A window
    whose end is at or before its start runs past midnight, and the part
    after midnight belongs to the day the window started.
    """
    s = _parse_time(start) or (0, None)
    e = _parse_time(end) or (1440, None)
    tz = s[1] or e[1] or default_tz
    local = now.astimezone(tz)
    minute = local.hour * 60 + local.minute
    listed = {str(d).upper() for d in days or []}
    wanted = listed - {"PUBLIC_HOLIDAYS"}
    if listed and not wanted:
        # Public holidays only: no holiday calendar here, so never in force.
        return False
    today = DAYS[local.weekday()]
    yesterday = DAYS[(local.weekday() - 1) % 7]

    def on(day: str) -> bool:
        return not wanted or day in wanted

    if s[0] < e[0]:
        return on(today) and s[0] <= minute < e[0]
    if s[0] == e[0]:
        return on(today)
    return (on(today) and minute >= s[0]) or (on(yesterday) and minute < e[0])


def season_matches(period: dict[str, Any], today: date) -> bool:
    """Whether a tariff period's recurring MM-DD range covers today."""
    start, end = period.get("startDate"), period.get("endDate")
    if not start or not end:
        return True
    key = today.strftime("%m-%d")
    if start <= end:
        return start <= key <= end
    return key >= start or key <= end


def _first_energy_rate(rates: list[dict[str, Any]] | None) -> float | None:
    for rate in rates or []:
        if rate.get("measureUnit") in ENERGY_UNITS:
            return amount(rate.get("unitPrice"))
    return None


def _blocks(rates: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {
            "unit_price_ex_gst": amount(r.get("unitPrice")),
            "unit": r.get("measureUnit") or "KWH",
            "volume": r.get("volume"),
        }
        for r in rates or []
    ]


@dataclass(slots=True)
class TariffNow:
    """What the plan charges and pays at one moment."""

    import_ex_gst: float | None = None
    import_type: str | None = None
    import_name: str | None = None
    import_blocks: list[dict[str, Any]] = field(default_factory=list)
    supply_ex_gst: float | None = None
    feed_in: float | None = None
    feed_in_name: str | None = None
    demand_ex_gst: float | None = None
    demand_unit: str | None = None
    demand_name: str | None = None
    demand_detail: dict[str, Any] | None = None
    periods: list[str] = field(default_factory=list)
    components: list[dict[str, Any]] = field(default_factory=list)

    def signature(self) -> tuple[Any, ...]:
        return (
            self.import_ex_gst,
            self.import_type,
            self.supply_ex_gst,
            self.feed_in,
            self.demand_ex_gst,
        )


def _add(total: float | None, value: float | None) -> float | None:
    if value is None:
        return total
    return round((total or 0.0) + value, 6)


def evaluate(detail: dict[str, Any] | None, now: datetime, local_tz: tzinfo) -> TariffNow | None:
    """Rates in force at now, or None when the plan shares no tariff detail."""
    if not detail:
        return None
    periods = detail.get("tariffPeriod") or []
    feed_ins = detail.get("solarFeedInTariff") or []
    if not periods and not feed_ins:
        return None
    out = TariffNow()
    today = now.astimezone(local_tz).date()
    for period in periods:
        if not season_matches(period, today):
            continue
        tz = _zone(period.get("timeZone") or detail.get("timeZone"), local_tz)
        name = period.get("displayName")
        if name:
            out.periods.append(name)
        out.supply_ex_gst = _add(out.supply_ex_gst, amount(period.get("dailySupplyCharges")))
        kind = period.get("rateBlockUType")
        price: float | None = None
        if kind == "singleRate" or (kind is None and period.get("singleRate")):
            single = period.get("singleRate") or {}
            price = _first_energy_rate(single.get("rates"))
            if price is None and (general := amount(single.get("generalUnitPrice"))) is not None:
                price = round(general / (1 + GST_RATE), 6)
            if price is not None:
                out.import_type = out.import_type or SINGLE_RATE
                out.import_name = out.import_name or single.get("displayName")
                out.import_blocks = out.import_blocks or _blocks(single.get("rates"))
        elif kind == "timeOfUseRates" or (kind is None and period.get("timeOfUseRates")):
            for tou in period.get("timeOfUseRates") or []:
                windows = tou.get("timeOfUse") or []
                if any(
                    window_matches(w.get("days"), w.get("startTime"), w.get("endTime"), now, tz)
                    for w in windows
                ):
                    price = _first_energy_rate(tou.get("rates"))
                    out.import_type = out.import_type or (tou.get("type") or None)
                    out.import_name = out.import_name or tou.get("displayName")
                    out.import_blocks = out.import_blocks or _blocks(tou.get("rates"))
                    break
        elif kind == "demandCharges" or (kind is None and period.get("demandCharges")):
            for charge in period.get("demandCharges") or []:
                if window_matches(
                    charge.get("days"), charge.get("startTime"), charge.get("endTime"), now, tz
                ):
                    out.demand_ex_gst = _add(out.demand_ex_gst, amount(charge.get("amount")))
                    out.demand_unit = out.demand_unit or (charge.get("measureUnit") or "KVA")
                    out.demand_name = out.demand_name or charge.get("displayName")
                    out.demand_detail = out.demand_detail or {
                        "measurement_period": charge.get("measurementPeriod"),
                        "charge_period": charge.get("chargePeriod"),
                        "min_demand_kw": amount(charge.get("minDemand")),
                        "max_demand_kw": amount(charge.get("maxDemand")),
                    }
                    break
        out.import_ex_gst = _add(out.import_ex_gst, price)
        if price is not None or period.get("dailySupplyCharges"):
            out.components.append(
                {
                    "period": name,
                    "type": period.get("type") or "OTHER",
                    "unit_price_ex_gst": price,
                    "daily_supply_ex_gst": amount(period.get("dailySupplyCharges")),
                }
            )
    for fit in feed_ins:
        value = _feed_in_now(fit, now, _zone(detail.get("timeZone"), local_tz))
        if value is not None:
            out.feed_in = _add(out.feed_in, value)
            out.feed_in_name = out.feed_in_name or fit.get("displayName")
    return out


def _feed_in_now(fit: dict[str, Any], now: datetime, tz: tzinfo) -> float | None:
    kind = fit.get("tariffUType")
    if kind == "singleTariff" or (kind is None and fit.get("singleTariff")):
        return amount((fit.get("singleTariff") or {}).get("amount"))
    varying = fit.get("timeVaryingTariffs")
    if isinstance(varying, dict):
        varying = [varying]
    for tariff in varying or []:
        variations = tariff.get("timeVariations") or [{}]
        if any(
            window_matches(v.get("days"), v.get("startTime"), v.get("endTime"), now, tz)
            for v in variations
        ):
            return amount(tariff.get("amount"))
    return None


def has_import(detail: dict[str, Any] | None) -> bool:
    return any(
        p.get("singleRate") or p.get("timeOfUseRates")
        for p in (detail or {}).get("tariffPeriod") or []
    )


def has_supply(detail: dict[str, Any] | None) -> bool:
    return any(p.get("dailySupplyCharges") for p in (detail or {}).get("tariffPeriod") or [])


def has_demand(detail: dict[str, Any] | None) -> bool:
    return any(p.get("demandCharges") for p in (detail or {}).get("tariffPeriod") or [])


def has_feed_in(detail: dict[str, Any] | None) -> bool:
    return bool((detail or {}).get("solarFeedInTariff"))


def next_change(detail: dict[str, Any] | None, now: datetime, local_tz: tzinfo) -> datetime | None:
    """The next time any rate changes, to the nearest five minutes."""
    current = evaluate(detail, now, local_tz)
    if current is None:
        return None
    base = current.signature()
    step = now.replace(second=0, microsecond=0)
    step += timedelta(minutes=5 - step.minute % 5)
    end = now + NEXT_CHANGE_HORIZON
    while step <= end:
        later = evaluate(detail, step, local_tz)
        if later is not None and later.signature() != base:
            return step
        step += NEXT_CHANGE_STEP
    return None
