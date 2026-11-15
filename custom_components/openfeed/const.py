"""Constants for the openfeed Consumer Data Right integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "openfeed"

DEFAULT_ISSUER: Final = "https://auth.openfeed.au"
DEFAULT_RESOURCE: Final = "https://api.openfeed.au"
DEFAULT_API_BASE: Final = "https://api.openfeed.au"
CONSENT_URL: Final = "https://app.openfeed.au/grants/disclosure"
DASHBOARD_URL: Final = "https://app.openfeed.au"
# Opens Settings > Dashboards > Energy, where the meter statistics are added.
ENERGY_CONFIG_URL: Final = "homeassistant://config/energy"
REGISTER_URL: Final = "https://app.openfeed.au/registered-apps/new"

SCOPE_OPENID: Final = "openid"
SCOPE_OFFLINE: Final = "offline_access"
SCOPE_ENERGY: Final = "openfeed-au:data:energy:read"
SCOPE_BANKING: Final = "openfeed-au:data:banking:read"

# Config entry data keys.
CONF_CLIENT_ID: Final = "client_id"
CONF_APP_ID: Final = "app_id"
CONF_ISSUER: Final = "issuer"
CONF_REDIRECT_BASE: Final = "redirect_base"
CONF_INCLUDE_BANKING: Final = "include_banking"
# Flow-only: the user accepts a plain http:// redirect to a non-loopback host.
CONF_ALLOW_HTTP: Final = "allow_insecure_redirect"
CONF_SCOPES: Final = "scopes"
CONF_APP_KEY: Final = "app_key_pem"
CONF_DPOP_KEY: Final = "dpop_key_pem"
CONF_TOKEN: Final = "token"
CONF_GRANT_ID: Final = "grant_id"
CONF_SUBJECT: Final = "subject"
# Bumped when a money statistic changes meaning and must be rebuilt.
CONF_MONEY_STATS_VERSION: Final = "money_stats_version"
# 3: v0.1.5 rebuilds payments again; v0.1.4 could carry a stale running sum.
MONEY_STATS_VERSION: Final = 3

# Options.
CONF_SIDEBAR_DASHBOARD: Final = "sidebar_dashboard"
DASHBOARD_URL_PATH: Final = "openfeed"
CONF_BACKFILL_DAYS: Final = "backfill_days"
DEFAULT_BACKFILL_DAYS: Final = 60
MAX_BACKFILL_DAYS: Final = 365

# The callback views Home Assistant serves for the browser redirects.
CALLBACK_PATH: Final = "/api/openfeed/callback"
CONSENT_PATH: Final = "/api/openfeed/consent"

# Polling. Energy data is batch refreshed by openfeed every 6 hours and
# balances are cached for about 15 minutes, so hourly polling is enough to
# pick up new data promptly without hammering the API.
UPDATE_INTERVAL_MINUTES: Final = 60
USAGE_REFRESH_HOURS: Final = 3
# Re-import this many days on each usage refresh so substituted reads that
# are later finalised overwrite the earlier values.
USAGE_RESTATE_DAYS: Final = 7
# The usage endpoint may return 500 for windows longer than 30 days.
USAGE_WINDOW_DAYS: Final = 30

# Account detail (plans), invoices and billing change slowly.
ACCOUNT_DETAIL_REFRESH_HOURS: Final = 24
BILLING_REFRESH_HOURS: Final = 6

PENDING_KEY: Final = f"{DOMAIN}_pending"
