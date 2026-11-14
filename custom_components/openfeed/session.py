"""Token lifecycle for a configured openfeed entry.

Holds the DPoP-bound token set, refreshes it before expiry under a lock and
persists every new token set to the config entry, because openfeed may
rotate the refresh token on each refresh.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OpenfeedClient
from .const import (
    CONF_APP_KEY,
    CONF_CLIENT_ID,
    CONF_DPOP_KEY,
    CONF_GRANT_ID,
    CONF_ISSUER,
    CONF_TOKEN,
    DEFAULT_API_BASE,
    DEFAULT_ISSUER,
    DEFAULT_RESOURCE,
)
from .crypto import load_private_key
from .oauth import OpenfeedAuth, OpenfeedAuthError, TokenSet

_LOGGER = logging.getLogger(__name__)

REFRESH_MARGIN_SECONDS = 120


def build_auth(hass: HomeAssistant, data: dict[str, Any]) -> OpenfeedAuth:
    """Build an auth client from config entry (or flow) data."""
    return OpenfeedAuth(
        async_get_clientsession(hass),
        data[CONF_CLIENT_ID],
        load_private_key(data[CONF_APP_KEY]),
        load_private_key(data[CONF_DPOP_KEY]),
        data.get(CONF_ISSUER, DEFAULT_ISSUER),
        DEFAULT_RESOURCE,
    )


class OpenfeedSession:
    """Access-token provider and API client for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.auth = build_auth(hass, dict(entry.data))
        self.tokens = TokenSet.from_dict(entry.data[CONF_TOKEN])
        self._lock = asyncio.Lock()
        self.client = OpenfeedClient(
            async_get_clientsession(hass),
            self.auth.dpop_key,
            self.async_get_access_token,
            DEFAULT_API_BASE,
        )

    @property
    def grant_id(self) -> str | None:
        return self.entry.data.get(CONF_GRANT_ID) or self.tokens.grant_id

    async def async_get_access_token(self) -> str:
        """Return a valid access token, refreshing first if it is near expiry."""
        async with self._lock:
            if self.tokens.expires_within(REFRESH_MARGIN_SECONDS):
                await self._refresh_locked()
            return self.tokens.access_token

    async def async_force_refresh(self) -> None:
        """Refresh now, after the API rejected the current token."""
        async with self._lock:
            await self._refresh_locked()

    async def _refresh_locked(self) -> None:
        try:
            new = await self.auth.refresh(self.tokens)
        except OpenfeedAuthError as err:
            if err.is_invalid_grant:
                raise ConfigEntryAuthFailed(
                    "openfeed refresh token was rejected; sign in again"
                ) from err
            raise
        if new.grant_id is None:
            new.grant_id = self.tokens.grant_id
        self.tokens = new
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_TOKEN: new.as_dict(), CONF_GRANT_ID: new.grant_id},
        )
        _LOGGER.debug("openfeed access token refreshed")

    def clear_grant(self) -> None:
        """Forget the grant after a revocation so reauth starts a new one."""
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_GRANT_ID: None}
        )
