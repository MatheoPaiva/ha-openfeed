"""Data update coordinator for openfeed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    BalanceUnavailableError,
    CreditExhaustedError,
    GrantRevokedError,
    OpenfeedApiError,
    RateLimitedError,
    TokenRejectedError,
    cancel_on_revocation,
)
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_SCOPES,
    DEFAULT_BACKFILL_DAYS,
    DOMAIN,
    SCOPE_BANKING,
    SCOPE_ENERGY,
    UPDATE_INTERVAL_MINUTES,
    USAGE_REFRESH_HOURS,
    USAGE_RESTATE_DAYS,
    USAGE_WINDOW_DAYS,
)
from .oauth import OpenfeedAuthError
from .session import OpenfeedSession
from .statistics import async_import_meter, meter_key
from .usage import AEST, UsageSummary, summarise, usage_windows

_LOGGER = logging.getLogger(__name__)

ISSUE_CREDITS = "credit_exhausted"


@dataclass(slots=True)
class MeterData:
    """One service point and what the integration knows about it."""

    account_id: str
    meter: dict[str, Any]
    key: str
    der: dict[str, Any] | None = None
    usage: UsageSummary | None = None
    usage_fetched_at: datetime | None = None


@dataclass(slots=True)
class EnergyAccountData:
    """One energy account."""

    account: dict[str, Any]
    balance: dict[str, Any] | None = None
    balance_status: str = "ok"
    latest_invoice: dict[str, Any] | None = None
    meters: dict[str, MeterData] = field(default_factory=dict)


@dataclass(slots=True)
class BankingAccountData:
    """One banking account."""

    account: dict[str, Any]
    balance: dict[str, Any] | None = None
    balance_status: str = "ok"


@dataclass(slots=True)
class OpenfeedData:
    """Everything one refresh produced."""

    energy: dict[str, EnergyAccountData] = field(default_factory=dict)
    banking: dict[str, BankingAccountData] = field(default_factory=dict)
    fetched_at: datetime | None = None


type OpenfeedConfigEntry = ConfigEntry[OpenfeedCoordinator]


def _latest_invoice(invoices: list[dict[str, Any]]) -> dict[str, Any] | None:
    dated = [i for i in invoices if i.get("issueDate")]
    if not dated:
        return invoices[-1] if invoices else None
    return max(dated, key=lambda i: str(i["issueDate"]))


class OpenfeedCoordinator(DataUpdateCoordinator[OpenfeedData]):
    """Polls the Sharing API and imports usage statistics."""

    config_entry: OpenfeedConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, session: OpenfeedSession) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.session = session
        self._usage_cache: dict[str, MeterData] = {}
        self._backfilled: set[str] = set()

    @property
    def scopes(self) -> list[str]:
        return list(self.config_entry.data.get(CONF_SCOPES, [SCOPE_ENERGY]))

    async def _async_update_data(self) -> OpenfeedData:
        try:
            try:
                data = await self._collect()
            except TokenRejectedError:
                await self.session.async_force_refresh()
                data = await self._collect()
        except GrantRevokedError as err:
            # Revocation is a hard stop: purge cached data and the grant,
            # then ask the user to reconnect.
            self._usage_cache.clear()
            self.session.clear_grant()
            raise ConfigEntryAuthFailed(
                "The openfeed disclosure grant was revoked or has ended"
            ) from err
        except TokenRejectedError as err:
            raise ConfigEntryAuthFailed("openfeed rejected a fresh access token") from err
        except CreditExhaustedError as err:
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE_CREDITS, is_fixable=False,
                severity=ir.IssueSeverity.ERROR, translation_key=ISSUE_CREDITS,
            )
            raise UpdateFailed("openfeed developer credits are exhausted; grant suspended") from err
        except RateLimitedError as err:
            raise UpdateFailed("openfeed rate limit reached; backing off") from err
        except (OpenfeedApiError, OpenfeedAuthError) as err:
            raise UpdateFailed(f"openfeed request failed: {err}") from err
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_CREDITS)
        return data

    async def _collect(self) -> OpenfeedData:
        data = OpenfeedData(fetched_at=dt_util.utcnow())
        client = self.session.client
        if SCOPE_ENERGY in self.scopes:
            for account in await client.energy_accounts():
                data.energy[account["accountId"]] = await self._collect_energy_account(account)
        if SCOPE_BANKING in self.scopes:
            accounts = await client.banking_accounts()
            balances = await cancel_on_revocation(
                [client.banking_balance(a["accountId"]) for a in accounts]
            )
            for account, balance in zip(accounts, balances, strict=True):
                item = BankingAccountData(account=account)
                _apply_balance(item, balance)
                data.banking[account["accountId"]] = item
        return data

    async def _collect_energy_account(self, account: dict[str, Any]) -> EnergyAccountData:
        client = self.session.client
        account_id = account["accountId"]
        item = EnergyAccountData(account=account)
        balance, meters, invoices = await cancel_on_revocation(
            [
                client.energy_balance(account_id),
                client.energy_meters(account_id),
                client.energy_invoices(account_id),
            ]
        )
        _apply_balance(item, balance)
        if isinstance(invoices, list):
            item.latest_invoice = _latest_invoice(invoices)
        if isinstance(meters, OpenfeedApiError):
            raise meters
        for meter in meters:
            item.meters[meter["meterId"]] = await self._collect_meter(account_id, meter)
        return item

    async def _collect_meter(self, account_id: str, meter: dict[str, Any]) -> MeterData:
        meter_id = meter["meterId"]
        cached = self._usage_cache.get(meter_id)
        md = MeterData(account_id=account_id, meter=meter, key=meter_key(meter))
        md.der = await self.session.client.energy_der(account_id, meter_id)
        now = dt_util.utcnow()
        if cached and cached.usage_fetched_at and now - cached.usage_fetched_at < timedelta(
            hours=USAGE_REFRESH_HOURS
        ):
            md.usage, md.usage_fetched_at = cached.usage, cached.usage_fetched_at
        else:
            md.usage = await self._refresh_usage(md)
            md.usage_fetched_at = now
        self._usage_cache[meter_id] = md
        return md

    async def _refresh_usage(self, md: MeterData) -> UsageSummary:
        today = dt_util.now(AEST).date()
        days = (
            USAGE_RESTATE_DAYS
            if md.meter["meterId"] in self._backfilled
            else int(self.config_entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS))
        )
        oldest = today - timedelta(days=days)
        records: list[dict[str, Any]] = []
        for start, end in usage_windows(oldest, today, USAGE_WINDOW_DAYS):
            records.extend(
                await self.session.client.energy_usage(
                    md.account_id, md.meter["meterId"], start.isoformat(), end.isoformat()
                )
            )
        summary = summarise(records)
        if summary.hourly:
            label = f"openfeed {md.meter.get('nationalMeteringId') or md.key}"
            await async_import_meter(self.hass, md.key, label, summary.hourly)
        self._backfilled.add(md.meter["meterId"])
        return summary


def _apply_balance(item: EnergyAccountData | BankingAccountData, result: Any) -> None:
    if isinstance(result, BalanceUnavailableError):
        item.balance_status = "unavailable"
    elif isinstance(result, OpenfeedApiError):
        item.balance_status = "error"
    else:
        item.balance = result


def latest_day_total(
    usage: UsageSummary | None, series: str, today: date | None = None
) -> tuple[date, float] | None:
    """The most recent full AEST day total for a series (today excluded)."""
    if usage is None or not usage.daily.get(series):
        return None
    today = today or dt_util.now(AEST).date()
    days = [d for d in usage.daily[series] if d < today]
    if not days:
        return None
    day = max(days)
    return day, round(usage.daily[series][day], 3)
