"""The openfeed Consumer Data Right integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_GRANT_ID, DOMAIN
from .coordinator import OpenfeedConfigEntry, OpenfeedCoordinator
from .session import OpenfeedSession
from .views import async_register_views

PLATFORMS: list[Platform] = [Platform.CALENDAR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the redirect views so a reauth flow can complete."""
    async_register_views(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OpenfeedConfigEntry) -> bool:
    """Set up openfeed from a config entry."""
    async_register_views(hass)
    if not entry.data.get(CONF_GRANT_ID):
        raise ConfigEntryAuthFailed("No openfeed disclosure grant; reconnect to share data")
    session = OpenfeedSession(hass, entry)
    coordinator = OpenfeedCoordinator(hass, entry, session)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    options_snapshot = dict(entry.options)

    async def _async_entry_updated(hass: HomeAssistant, entry: OpenfeedConfigEntry) -> None:
        # Token refreshes rewrite entry data every hour; only an options
        # change should reload the entry.
        if dict(entry.options) != options_snapshot:
            await hass.config_entries.async_reload(entry.entry_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OpenfeedConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
