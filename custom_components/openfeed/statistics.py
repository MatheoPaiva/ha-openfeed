"""Import openfeed data into Home Assistant long-term statistics.

Each meter gets external statistics openfeed:<meter_key>_<series> in kWh,
so retailer meter data can be added to the Energy dashboard as grid
consumption and return to grid. Each meter also gets
openfeed:<meter_key>_<series>_power in kW (hourly mean, min and max of the
meter's interval reads), which keeps peak demand and peak export that the
hourly energy sums hide. Each account gets money series
openfeed:<account_key>_<charges|credits|payments|refunds|invoiced>. Each refresh re-imports a trailing window,
restarting the running sum from the last statistic before the window, so
finalised substitute reads overwrite earlier values.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import logging
import re

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .billing import CHARGES, CREDITS, INVOICED, PAYMENTS, REFUNDS
from .usage import CONSUMPTION, CONTROLLED_LOAD, EXPORT, cumulative

_LOGGER = logging.getLogger(__name__)

SERIES_NAMES = {
    CONSUMPTION: "grid consumption",
    EXPORT: "grid export",
    CONTROLLED_LOAD: "controlled load",
}


POWER_NAMES = {
    CONSUMPTION: "grid import power",
    EXPORT: "grid export power",
}

MONEY_NAMES = {
    CHARGES: "billed charges",
    CREDITS: "billed credits",
    PAYMENTS: "payments made",
    REFUNDS: "refunds received",
    INVOICED: "invoiced",
}


def meter_key(meter: dict) -> str:
    """A stable, statistic-safe key for a meter.

    The NMI when present (it is stable across retailers), otherwise a short
    hash of the provider-scoped meter ID.
    """
    nmi = str(meter.get("nationalMeteringId") or "").lower()
    if re.fullmatch(r"[a-z0-9]{10,11}", nmi):
        return f"nmi_{nmi}"
    digest = hashlib.sha256(str(meter.get("meterId")).encode()).hexdigest()[:12]
    return f"meter_{digest}"


def account_key(account_id: str) -> str:
    """A stable, statistic-safe key for a tokenised account ID."""
    return f"acct_{hashlib.sha256(str(account_id).encode()).hexdigest()[:12]}"


def statistic_id(key: str, series: str) -> str:
    return f"{DOMAIN}:{key}_{series}"


async def _sum_before(hass: HomeAssistant, stat_id: str, first_hour: datetime) -> float:
    """The running sum of the last statistic before first_hour, else 0."""
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        first_hour - timedelta(days=400),
        first_hour,
        {stat_id},
        "hour",
        None,
        {"sum"},
    )
    series = rows.get(stat_id) or []
    if not series:
        return 0.0
    return float(series[-1].get("sum") or 0.0)


async def async_import_meter(
    hass: HomeAssistant,
    key: str,
    label: str,
    hourly: dict[str, dict[datetime, float]],
) -> dict[str, int]:
    """Import hourly kWh series for one meter. Returns rows written per series."""
    return await _import_sums(
        hass, key, label, hourly, SERIES_NAMES, UnitOfEnergy.KILO_WATT_HOUR, "energy"
    )


async def async_clear(hass: HomeAssistant, stat_ids: list[str]) -> None:
    """Delete statistics, for series whose meaning changed between versions.

    Waits for the recorder to finish, so a following import does not read a
    running sum from rows that are about to be deleted.
    """
    instance = get_instance(hass)
    instance.async_clear_statistics(stat_ids)
    await instance.async_block_till_done()


async def async_import_money(
    hass: HomeAssistant,
    key: str,
    label: str,
    hourly: dict[str, dict[datetime, float]],
    currency: str,
    rebuild: frozenset[str] = frozenset(),
) -> dict[str, int]:
    """Import hourly money series for one account.

    Series in rebuild start their running sum at zero instead of continuing
    from earlier rows, because those rows were just cleared.
    """
    return await _import_sums(
        hass, key, label, hourly, MONEY_NAMES, currency, None, rebuild
    )


async def async_import_power(
    hass: HomeAssistant,
    key: str,
    label: str,
    hourly_power: dict[str, dict[datetime, tuple[float, float, float]]],
) -> dict[str, int]:
    """Import hourly mean/min/max kW for one meter."""
    written: dict[str, int] = {}
    for series, values in hourly_power.items():
        if not values or series == CONTROLLED_LOAD:
            continue
        stat_id = statistic_id(key, f"{series}_power")
        rows = [
            StatisticData(start=hour, mean=round(mean, 4), min=round(low, 4), max=round(high, 4))
            for hour, (mean, low, high) in sorted(values.items())
        ]
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name=f"{label} {POWER_NAMES.get(series, series)}",
            source=DOMAIN,
            statistic_id=stat_id,
            unit_class="power",
            unit_of_measurement=UnitOfPower.KILO_WATT,
        )
        async_add_external_statistics(hass, metadata, rows)
        written[series] = len(rows)
    return written


async def _import_sums(
    hass: HomeAssistant,
    key: str,
    label: str,
    hourly: dict[str, dict[datetime, float]],
    names: dict[str, str],
    unit: str,
    unit_class: str | None,
    rebuild: frozenset[str] = frozenset(),
) -> dict[str, int]:
    written: dict[str, int] = {}
    for series, values in hourly.items():
        if not values:
            continue
        stat_id = statistic_id(key, series)
        first_hour = min(values)
        base = 0.0 if series in rebuild else await _sum_before(hass, stat_id, first_hour)
        rows = [
            StatisticData(start=hour, state=state, sum=total)
            for hour, state, total in cumulative(values, base)
        ]
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{label} {names.get(series, series)}",
            source=DOMAIN,
            statistic_id=stat_id,
            unit_class=unit_class,
            unit_of_measurement=unit,
        )
        async_add_external_statistics(hass, metadata, rows)
        written[series] = len(rows)
        _LOGGER.debug("Imported %d hourly rows into %s", len(rows), stat_id)
    return written
