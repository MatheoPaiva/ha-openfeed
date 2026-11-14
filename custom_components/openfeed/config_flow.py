"""Config flow for openfeed.

Setup, in order:

1. user       The integration generates both PS256 keypairs and shows the
              public JWKS. The user registers an app in the openfeed
              dashboard, pastes the JWKS, and enters the two IDs it returns.
2. authorize  External step: a pushed authorisation request, then the
              openfeed sign-in. The callback view validates state and iss,
              and the flow exchanges the code with a DPoP proof.
3. consent    External step, only when no disclosure grant exists yet: the
              openfeed consent deep link where the user picks accounts.
              The flow then refreshes to obtain a grant-bound token.
4. finish     Creates (or, for reauth and reconfigure, updates) the entry.

Private keys are generated inside Home Assistant and never displayed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlencode

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    CALLBACK_PATH,
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_BACKFILL_DAYS,
    CONF_SIDEBAR_DASHBOARD,
    CONF_CLIENT_ID,
    CONF_DPOP_KEY,
    CONF_GRANT_ID,
    CONF_INCLUDE_BANKING,
    CONF_ISSUER,
    CONF_REDIRECT_BASE,
    CONF_SCOPES,
    CONF_SUBJECT,
    CONF_TOKEN,
    CONSENT_PATH,
    CONSENT_URL,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_ISSUER,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    REGISTER_URL,
    SCOPE_BANKING,
    SCOPE_ENERGY,
    SCOPE_OFFLINE,
    SCOPE_OPENID,
)
from .crypto import generate_private_key_pem, load_private_key, pkce_pair, public_jwks, random_token
from .oauth import OpenfeedAuthError, TokenSet
from .session import build_auth
from .views import async_register_views, discard_pending, register_pending

_LOGGER = logging.getLogger(__name__)

_CLIENT_ID_RE = re.compile(r"^app-[0-9a-fA-F-]{36}$")
_APP_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _scopes(include_banking: bool) -> list[str]:
    scopes = [SCOPE_OPENID, SCOPE_OFFLINE, SCOPE_ENERGY]
    if include_banking:
        scopes.append(SCOPE_BANKING)
    return scopes


class OpenfeedConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an openfeed config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._verifier: str | None = None
        self._state: str | None = None
        self._nonce: str | None = None
        self._redirect_uri: str | None = None
        self._tokens: TokenSet | None = None
        self._subject: str | None = None
        self._failure: str | None = None
        self._grant_action = "create"

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OpenfeedOptionsFlow()

    # Step 1 ----------------------------------------------------------------

    def _default_base(self) -> str:
        try:
            return get_url(self.hass, allow_ip=True, prefer_external=True)
        except NoURLAvailableError:
            return ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the JWKS and collect the registered app's IDs."""
        async_register_views(self.hass)
        if CONF_APP_KEY not in self._data:
            app_pem, dpop_pem = await self.hass.async_add_executor_job(
                lambda: (generate_private_key_pem(), generate_private_key_pem())
            )
            self._data.update({CONF_APP_KEY: app_pem, CONF_DPOP_KEY: dpop_pem})

        errors: dict[str, str] = {}
        if user_input is not None:
            client_id = user_input[CONF_CLIENT_ID].strip()
            app_id = user_input[CONF_APP_ID].strip()
            base = user_input[CONF_REDIRECT_BASE].strip().rstrip("/")
            if not _CLIENT_ID_RE.match(client_id):
                errors[CONF_CLIENT_ID] = "invalid_client_id"
            if not _APP_ID_RE.match(app_id):
                errors[CONF_APP_ID] = "invalid_app_id"
            if not base.startswith(("http://", "https://")):
                errors[CONF_REDIRECT_BASE] = "invalid_url"
            if not errors:
                self._data.update(
                    {
                        CONF_CLIENT_ID: client_id,
                        CONF_APP_ID: app_id,
                        CONF_REDIRECT_BASE: base,
                        CONF_ISSUER: DEFAULT_ISSUER,
                        CONF_SCOPES: _scopes(user_input[CONF_INCLUDE_BANKING]),
                    }
                )
                return await self.async_step_authorize()

        jwks = public_jwks(load_private_key(self._data[CONF_APP_KEY]))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CLIENT_ID): str,
                    vol.Required(CONF_APP_ID): str,
                    vol.Required(CONF_REDIRECT_BASE, default=self._default_base()): str,
                    vol.Required(CONF_INCLUDE_BANKING, default=False): bool,
                }
            ),
            description_placeholders={
                "register_url": REGISTER_URL,
                "jwks": json.dumps(jwks),
            },
            errors=errors,
        )

    # Step 2 ----------------------------------------------------------------

    async def _start_authorization(self) -> str:
        auth = build_auth(self.hass, self._data)
        self._verifier, challenge = pkce_pair()
        self._state = random_token()
        self._nonce = random_token()
        self._redirect_uri = f"{self._data[CONF_REDIRECT_BASE]}{CALLBACK_PATH}"
        grant_id = self._data.get(CONF_GRANT_ID) if self._grant_action == "replace" else None
        request_uri = await auth.push_authorization_request(
            redirect_uri=self._redirect_uri,
            scopes=self._data[CONF_SCOPES],
            state=self._state,
            nonce=self._nonce,
            code_challenge=challenge,
            grant_management_action="replace" if grant_id else "create",
            grant_id=grant_id,
        )
        register_pending(self.hass, self._state, self.flow_id, "auth")
        return auth.authorize_url(request_uri)

    async def async_step_authorize(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Run the sign-in as an external step."""
        if user_input is None:
            try:
                url = await self._start_authorization()
            except OpenfeedAuthError as err:
                _LOGGER.warning("openfeed pushed authorisation request failed: %s", err)
                reason = "invalid_client" if err.error == "invalid_client" else "par_failed"
                if err.error == "invalid_scope":
                    reason = "invalid_scope"
                return self.async_abort(reason=reason, description_placeholders={"error": str(err)})
            return self.async_external_step(step_id="authorize", url=url)

        self._failure = await self._handle_callback(user_input)
        if self._failure:
            return self.async_external_step_done(next_step_id="failed")
        assert self._tokens is not None
        if self._tokens.grant_id:
            return self.async_external_step_done(next_step_id="finish")
        return self.async_external_step_done(next_step_id="consent")

    async def _handle_callback(self, query: dict[str, Any]) -> str | None:
        """Validate the redirect and exchange the code. Returns a failure reason."""
        discard_pending(self.hass, self.flow_id)
        if "error" in query:
            _LOGGER.warning("openfeed sign-in returned %s: %s", query["error"], query.get("error_description"))
            return "access_denied" if query["error"] == "access_denied" else "auth_error"
        if query.get("state") != self._state:
            return "state_mismatch"
        auth = build_auth(self.hass, self._data)
        disc = await auth.discover()
        # RFC 9207: the iss parameter must match the issuer we started with.
        if query.get("iss", "").rstrip("/") != disc.issuer.rstrip("/"):
            return "issuer_mismatch"
        try:
            tokens = await auth.exchange_code(query["code"], self._verifier or "", self._redirect_uri or "")
            if not tokens.refresh_token:
                return "no_refresh_token"
            if tokens.id_token:
                claims = await auth.verify_id_token(tokens.id_token, self._nonce or "")
                self._subject = claims.get("sub")
        except (OpenfeedAuthError, KeyError) as err:
            _LOGGER.warning("openfeed code exchange failed: %s", err)
            return "token_failed"
        self._tokens = tokens
        return None

    # Step 3 ----------------------------------------------------------------

    async def async_step_consent(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Send the user to pick the accounts to share."""
        if user_input is None:
            state = random_token()
            register_pending(self.hass, state, self.flow_id, "consent")
            return_uri = f"{self._data[CONF_REDIRECT_BASE]}{CONSENT_PATH}/{state}"
            url = f"{CONSENT_URL}?{urlencode({'appId': self._data[CONF_APP_ID], 'redirectUri': return_uri})}"
            return self.async_external_step(step_id="consent", url=url)

        discard_pending(self.hass, self.flow_id)
        if user_input.get("consented") != "true":
            _LOGGER.info("openfeed consent declined: %s", user_input.get("reason"))
            self._failure = "consent_declined"
            return self.async_external_step_done(next_step_id="failed")
        # The new access token carries the grant without a second sign-in.
        assert self._tokens is not None
        try:
            refreshed = await build_auth(self.hass, self._data).refresh(self._tokens)
        except OpenfeedAuthError as err:
            _LOGGER.warning("openfeed post-consent refresh failed: %s", err)
            self._failure = "token_failed"
            return self.async_external_step_done(next_step_id="failed")
        expected = user_input.get("grantId")
        if not refreshed.grant_id or (expected and refreshed.grant_id != expected):
            self._failure = "grant_mismatch"
            return self.async_external_step_done(next_step_id="failed")
        self._tokens = refreshed
        return self.async_external_step_done(next_step_id="finish")

    # Step 4 ----------------------------------------------------------------

    async def async_step_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason=self._failure or "auth_error")

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create or update the entry."""
        assert self._tokens is not None
        data = {
            **self._data,
            CONF_TOKEN: self._tokens.as_dict(),
            CONF_GRANT_ID: self._tokens.grant_id,
        }
        if self._subject:
            data[CONF_SUBJECT] = self._subject
        unique_id = f"{data[CONF_CLIENT_ID]}_{self._subject or 'user'}"

        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            entry = (
                self._get_reauth_entry() if self.source == SOURCE_REAUTH else self._get_reconfigure_entry()
            )
            if entry.unique_id and entry.unique_id != unique_id:
                return self.async_abort(reason="wrong_account")
            return self.async_update_reload_and_abort(entry, data=data)

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="openfeed CDR", data=data)

    # Reauth and reconfigure --------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Sign in again, keeping the keys and registered app."""
        self._data = dict(entry_data)
        self._grant_action = "replace" if entry_data.get(CONF_GRANT_ID) else "create"
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        async_register_views(self.hass)
        return await self.async_step_authorize()

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change which accounts are shared (grant replace) or the redirect URL."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            base = str(user_input.get(CONF_REDIRECT_BASE) or "").strip().rstrip("/")
            if not base.startswith(("http://", "https://")):
                errors[CONF_REDIRECT_BASE] = "invalid_url"
            else:
                self._data = {
                    **entry.data,
                    CONF_SCOPES: _scopes(user_input[CONF_INCLUDE_BANKING]),
                    CONF_REDIRECT_BASE: base,
                }
                self._grant_action = "replace" if entry.data.get(CONF_GRANT_ID) else "create"
                async_register_views(self.hass)
                return await self.async_step_authorize()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INCLUDE_BANKING,
                        default=SCOPE_BANKING in entry.data.get(CONF_SCOPES, []),
                    ): bool,
                    vol.Required(
                        CONF_REDIRECT_BASE,
                        default=entry.data.get(CONF_REDIRECT_BASE) or self._default_base(),
                    ): str,
                }
            ),
            errors=errors,
        )


class OpenfeedOptionsFlow(OptionsFlow):
    """Options: usage backfill depth and the sidebar dashboard."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
        sidebar = self.config_entry.options.get(CONF_SIDEBAR_DASHBOARD, True)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BACKFILL_DAYS, default=current): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=MAX_BACKFILL_DAYS)
                    ),
                    vol.Required(CONF_SIDEBAR_DASHBOARD, default=sidebar): bool,
                }
            ),
        )
