"""Sensors for openfeed energy and banking accounts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfApparentPower, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DASHBOARD_URL, DOMAIN
from .coordinator import (
    BankingAccountData,
    EnergyAccountData,
    MeterData,
    OpenfeedConfigEntry,
    OpenfeedCoordinator,
    latest_day_total,
)
from .usage import CONSUMPTION, CONTROLLED_LOAD, EXPORT


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


# Energy account sensors --------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AccountSensorDescription(SensorEntityDescription):
    value_fn: Callable[[EnergyAccountData], Any]
    attrs_fn: Callable[[EnergyAccountData], dict[str, Any]] = lambda _a: {}
    available_fn: Callable[[EnergyAccountData], bool] = lambda _a: True


def _plan(a: EnergyAccountData) -> dict[str, Any]:
    plans = a.account.get("plans") or []
    return (plans[0].get("planOverview") or {}) if plans else {}


def _invoice(a: EnergyAccountData, key: str) -> Any:
    return (a.latest_invoice or {}).get(key)


ACCOUNT_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="balance",
        translation_key="energy_balance",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda a: _num((a.balance or {}).get("balance")),
        attrs_fn=lambda a: {"freshness": "live, cached up to 15 minutes", "status": a.balance_status},
        available_fn=lambda a: a.balance is not None,
    ),
    AccountSensorDescription(
        key="latest_invoice_amount",
        translation_key="latest_invoice_amount",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda a: _num(_invoice(a, "invoiceAmount")),
        attrs_fn=lambda a: {
            "issue_date": _invoice(a, "issueDate"),
            "due_date": _invoice(a, "dueDate"),
            "payment_status": _invoice(a, "paymentStatus"),
            "gst_amount": _invoice(a, "gstAmount"),
            "balance_at_issue": _invoice(a, "balanceAtIssue"),
        },
        available_fn=lambda a: a.latest_invoice is not None,
    ),
    AccountSensorDescription(
        key="latest_invoice_due",
        translation_key="latest_invoice_due",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda a: _date(_invoice(a, "dueDate")),
        available_fn=lambda a: a.latest_invoice is not None,
    ),
    AccountSensorDescription(
        key="latest_invoice_status",
        translation_key="latest_invoice_status",
        value_fn=lambda a: _invoice(a, "paymentStatus"),
        available_fn=lambda a: a.latest_invoice is not None,
    ),
    AccountSensorDescription(
        key="plan",
        translation_key="plan",
        value_fn=lambda a: (_plan(a).get("displayName") or None),
        attrs_fn=lambda a: {
            "start_date": _plan(a).get("startDate"),
            "end_date": _plan(a).get("endDate"),
            "plan_detail": (a.account.get("plans") or [{}])[0].get("planDetail"),
        },
    ),
)


# Meter sensors -----------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class MeterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[MeterData], Any]
    attrs_fn: Callable[[MeterData], dict[str, Any]] = lambda _m: {}


def _day_sensor(series: str) -> MeterSensorDescription:
    def value(m: MeterData) -> float | None:
        total = latest_day_total(m.usage, series)
        return total[1] if total else None

    def attrs(m: MeterData) -> dict[str, Any]:
        total = latest_day_total(m.usage, series)
        return {
            "date": total[0].isoformat() if total else None,
            "interval_minutes": m.usage.interval_minutes if m.usage else None,
            "freshness": "batch, refreshed by openfeed every 6 hours",
        }

    return MeterSensorDescription(
        key=f"latest_day_{series}",
        translation_key=f"latest_day_{series}",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=value,
        attrs_fn=attrs,
    )


def _der(m: MeterData, key: str) -> Any:
    return (m.der or {}).get(key)


METER_SENSORS: tuple[MeterSensorDescription, ...] = (
    _day_sensor(CONSUMPTION),
    _day_sensor(EXPORT),
    _day_sensor(CONTROLLED_LOAD),
    MeterSensorDescription(
        key="der_approved_capacity",
        translation_key="der_approved_capacity",
        device_class=SensorDeviceClass.APPARENT_POWER,
        native_unit_of_measurement=UnitOfApparentPower.KILO_VOLT_AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: _num(_der(m, "approvedCapacity")),
        attrs_fn=lambda m: {
            "installed_phases": _der(m, "installedPhasesCount"),
            "available_phases": _der(m, "availablePhasesCount"),
            "islandable": _der(m, "islandableInstallation"),
            "ac_connections": _der(m, "acConnections"),
        },
    ),
    MeterSensorDescription(
        key="der_export_limit",
        translation_key="der_export_limit",
        device_class=SensorDeviceClass.APPARENT_POWER,
        native_unit_of_measurement=UnitOfApparentPower.KILO_VOLT_AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: _num(((m.der or {}).get("protectionMode") or {}).get("exportLimitKva")),
    ),
    MeterSensorDescription(
        key="substituted_intervals",
        translation_key="substituted_intervals",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.usage.substituted_intervals if m.usage else None,
    ),
    MeterSensorDescription(
        key="usage_fetched",
        translation_key="usage_fetched",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.usage_fetched_at,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenfeedConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors for every shared account and meter."""
    coordinator = entry.runtime_data
    data = coordinator.data
    entities: list[SensorEntity] = []
    for account_id, account in data.energy.items():
        entities.extend(
            EnergyAccountSensor(coordinator, account_id, d) for d in ACCOUNT_SENSORS
        )
        for meter_id, meter in account.meters.items():
            series_seen = set(meter.usage.daily) if meter.usage else set()
            for d in METER_SENSORS:
                if d.key.startswith("latest_day_") and d.key[11:] not in series_seen:
                    if d.key != f"latest_day_{CONSUMPTION}":
                        continue
                if d.key.startswith("der_") and meter.der is None:
                    continue
                entities.append(MeterSensor(coordinator, account_id, meter_id, d))
    for account_id in data.banking:
        entities.append(BankingBalanceSensor(coordinator, account_id, "current"))
        entities.append(BankingBalanceSensor(coordinator, account_id, "available"))
    async_add_entities(entities)


class _Base(CoordinatorEntity[OpenfeedCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_attribution = "Consumer Data Right data via openfeed"


class EnergyAccountSensor(_Base):
    entity_description: AccountSensorDescription

    def __init__(self, coordinator: OpenfeedCoordinator, account_id: str,
                 description: AccountSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._account_id = account_id
        account = coordinator.data.energy[account_id].account
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{account_id}_{description.key}"
        number = str(account.get("accountNumber") or "")[-4:]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"energy_{account_id}")},
            name=f"{account.get('providerName', 'Energy')} {account.get('displayName') or number}".strip(),
            manufacturer=account.get("providerName"),
            model="Energy account (CDR)",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=DASHBOARD_URL,
        )
        if description.device_class == SensorDeviceClass.MONETARY:
            balance = coordinator.data.energy[account_id].balance or {}
            self._attr_native_unit_of_measurement = balance.get("currency") or "AUD"

    @property
    def _account(self) -> EnergyAccountData | None:
        return self.coordinator.data.energy.get(self._account_id)

    @property
    def available(self) -> bool:
        a = self._account
        return super().available and a is not None and self.entity_description.available_fn(a)

    @property
    def native_value(self) -> Any:
        a = self._account
        return self.entity_description.value_fn(a) if a else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        a = self._account
        return self.entity_description.attrs_fn(a) if a else {}


class MeterSensor(_Base):
    entity_description: MeterSensorDescription

    def __init__(self, coordinator: OpenfeedCoordinator, account_id: str, meter_id: str,
                 description: MeterSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._account_id = account_id
        self._meter_id = meter_id
        meter = coordinator.data.energy[account_id].meters[meter_id]
        nmi = meter.meter.get("nationalMeteringId")
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{meter.key}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"meter_{meter.key}")},
            name=f"Meter {nmi or meter.key}",
            model=f"Service point ({meter.meter.get('detectedReadType') or 'unknown'} reads)",
            serial_number=nmi,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _meter(self) -> MeterData | None:
        account = self.coordinator.data.energy.get(self._account_id)
        return account.meters.get(self._meter_id) if account else None

    @property
    def available(self) -> bool:
        return super().available and self._meter is not None

    @property
    def native_value(self) -> Any:
        m = self._meter
        value = self.entity_description.value_fn(m) if m else None
        if isinstance(value, datetime) or value is None:
            return value
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._meter
        if m is None:
            return {}
        return {
            "statistic_prefix": f"{DOMAIN}:{m.key}_",
            "jurisdiction": m.meter.get("jurisdictionCode"),
            **self.entity_description.attrs_fn(m),
        }


class BankingBalanceSensor(_Base):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: OpenfeedCoordinator, account_id: str, kind: str) -> None:
        super().__init__(coordinator)
        self._account_id = account_id
        self._kind = kind
        self._attr_translation_key = f"banking_{kind}_balance"
        account = coordinator.data.banking[account_id].account
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{account_id}_bank_{kind}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"bank_{account_id}")},
            name=f"{account.get('providerName', 'Bank')} {account.get('displayName', '')}".strip(),
            manufacturer=account.get("providerName"),
            model=account.get("productName") or "Banking account (CDR)",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=DASHBOARD_URL,
        )

    @property
    def _item(self) -> BankingAccountData | None:
        return self.coordinator.data.banking.get(self._account_id)

    @property
    def available(self) -> bool:
        item = self._item
        return super().available and item is not None and item.balance is not None

    @property
    def native_unit_of_measurement(self) -> str | None:
        item = self._item
        return ((item.balance or {}).get("currency") if item else None) or "AUD"

    @property
    def native_value(self) -> float | None:
        item = self._item
        if item is None or item.balance is None:
            return None
        return _num(item.balance.get(f"{self._kind}Balance"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self._item
        if item is None:
            return {}
        return {
            "masked_number": item.account.get("maskedNumber"),
            "account_type": item.account.get("accountType"),
            "credit_limit": (item.balance or {}).get("creditLimit"),
            "status": item.balance_status,
        }
