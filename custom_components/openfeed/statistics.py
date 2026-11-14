"""Import openfeed interval usage into Home Assistant long-term statistics.

Each meter gets external statistics openfeed:<meter_key>_<series> in kWh,
so retailer meter data can be added to the Energy dashboard as grid
consumption and return to grid. Each refresh re-imports a trailing window,
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
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .usage import CONSUMPTION, CONTROLLED_LOAD, EXPORT, cumulative

_LOGGER = logging.getLogger(__name__)

SERIES_NAMES = {
    CONSUMPTION: "grid consumption",
    EXPORT: "grid export",
    CONTROLLED_LOAD: "controlled load",
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
    """Import hourly series for one meter. Returns rows written per series."""
    written: dict[str, int] = {}
    for series, values in hourly.items():
        if not values:
            continue
        stat_id = statistic_id(key, series)
        first_hour = min(values)
        base = await _sum_before(hass, stat_id, first_hour)
        rows = [
            StatisticData(start=hour, state=state, sum=total)
            for hour, state, total in cumulative(values, base)
        ]
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{label} {SERIES_NAMES.get(series, series)}",
            source=DOMAIN,
            statistic_id=stat_id,
            unit_class="energy",
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        async_add_external_statistics(hass, metadata, rows)
        written[series] = len(rows)
        _LOGGER.debug("Imported %d hourly rows into %s", len(rows), stat_id)
    return written
