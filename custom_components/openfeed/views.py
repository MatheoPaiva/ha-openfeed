"""Browser callback views for the openfeed sign-in and consent redirects.

Home Assistant's generic OAuth callback drops the RFC 9207 iss parameter,
which FAPI 2.0 requires the client to check, so the integration serves its
own views. Each pending flow registers a random state; the views resolve the
state to a flow and hand the query parameters to it.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from aiohttp import web

from homeassistant.components import http
from homeassistant.core import HomeAssistant, callback

from .const import CALLBACK_PATH, CONSENT_PATH, PENDING_KEY

_LOGGER = logging.getLogger(__name__)

_DONE_PAGE = (
    "<!doctype html><html><head><title>openfeed</title></head><body>"
    "<p>You can close this window and return to Home Assistant.</p>"
    "<script>window.close()</script></body></html>"
)


@dataclass(slots=True)
class PendingRedirect:
    """A flow waiting for a browser redirect."""

    flow_id: str
    kind: str  # "auth" or "consent"


@callback
def register_pending(hass: HomeAssistant, state: str, flow_id: str, kind: str) -> None:
    """Record that a flow is waiting for a redirect carrying this state."""
    hass.data.setdefault(PENDING_KEY, {})[state] = PendingRedirect(flow_id, kind)


@callback
def discard_pending(hass: HomeAssistant, flow_id: str) -> None:
    """Forget every pending redirect for a flow."""
    pending: dict[str, PendingRedirect] = hass.data.get(PENDING_KEY, {})
    for state in [s for s, p in pending.items() if p.flow_id == flow_id]:
        pending.pop(state, None)


def _take(hass: HomeAssistant, state: str | None, kind: str) -> PendingRedirect | None:
    if not state:
        return None
    pending: dict[str, PendingRedirect] = hass.data.get(PENDING_KEY, {})
    entry = pending.get(state)
    if entry is None or entry.kind != kind:
        return None
    pending.pop(state, None)
    return entry


async def _resume(hass: HomeAssistant, flow_id: str, user_input: dict[str, Any]) -> web.Response:
    try:
        await hass.config_entries.flow.async_configure(flow_id=flow_id, user_input=user_input)
    except Exception:  # noqa: BLE001 - surface any flow failure to the browser
        _LOGGER.exception("openfeed flow %s did not accept the redirect", flow_id)
        return web.Response(text="The Home Assistant setup flow could not continue.", status=400)
    return web.Response(content_type="text/html", text=_DONE_PAGE)


class OpenfeedCallbackView(http.HomeAssistantView):
    """Receives the authorisation code redirect."""

    requires_auth = False
    url = CALLBACK_PATH
    name = "api:openfeed:callback"

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app[http.KEY_HASS]
        query = request.query
        pending = _take(hass, query.get("state"), "auth")
        if pending is None:
            return web.Response(text="Unknown or expired state.", status=400)
        user_input = {k: query[k] for k in ("code", "iss", "state", "error", "error_description") if k in query}
        return await _resume(hass, pending.flow_id, user_input)


class OpenfeedConsentView(http.HomeAssistantView):
    """Receives the disclosure-consent deep-link return."""

    requires_auth = False
    url = CONSENT_PATH + "/{state}"
    name = "api:openfeed:consent"

    async def get(self, request: web.Request, state: str) -> web.Response:
        hass = request.app[http.KEY_HASS]
        pending = _take(hass, state, "consent")
        if pending is None:
            return web.Response(text="Unknown or expired state.", status=400)
        query = request.query
        user_input = {k: query[k] for k in ("grantId", "consented", "reason") if k in query}
        return await _resume(hass, pending.flow_id, user_input)


@callback
def async_register_views(hass: HomeAssistant) -> None:
    """Register both views once per Home Assistant run."""
    if hass.data.get(f"{PENDING_KEY}_views"):
        return
    hass.http.register_view(OpenfeedCallbackView())
    hass.http.register_view(OpenfeedConsentView())
    hass.data[f"{PENDING_KEY}_views"] = True
