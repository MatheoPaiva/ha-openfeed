# openfeed Consumer Data Right for Home Assistant

Bring your own Consumer Data Right (CDR) energy data into Home Assistant through
[openfeed](https://openfeed.au). You get your retailer's half-hourly meter reads in the
Energy dashboard, your account balance and latest bill as sensors, and optionally your
bank balances.

| Version | Home Assistant | Status |
|---|---|---|
| 0.1.0 | 2026.4 or later | First release |

## How it works

The [Consumer Data Right](https://www.cdr.gov.au/) lets you direct a data holder (your
energy retailer or bank) to share your data with an accredited data recipient. Only
ACCC-accredited providers can offer CDR services.

openfeed is the accredited recipient. You connect your retailer and bank to openfeed once.
This integration then receives your data from openfeed by onward disclosure, under a
sharing grant you approve and can revoke at any time from the openfeed dashboard. The
integration never talks to your retailer or bank directly.

Two consents are involved:

1. **openfeed sign-in.** You sign in to openfeed and approve this Home Assistant app.
2. **Disclosure grant.** You choose which accounts openfeed shares with the app.

Revoking the grant in openfeed stops data immediately. The integration discards cached
data and asks you to reconnect.

## What you get

| Data | Entity or statistic | Freshness |
|---|---|---|
| Grid consumption, export, controlled load | External statistics `openfeed:nmi_<nmi>_consumption`, `_export`, `_controlled_load` (hourly, kWh) | Batch; openfeed refreshes every 6 hours, meter data usually lags a day |
| Latest full day per series | `sensor.meter_<nmi>_latest_day_*` | As above |
| Account balance | `sensor.energy_<account>_account_balance` | Live, cached up to 15 minutes |
| Latest invoice amount, due date, status | `sensor.energy_<account>_latest_invoice_*` | Hourly poll |
| Plan name | `sensor.energy_<account>_plan` | Hourly poll |
| DER approved capacity and export limit | `sensor.meter_<nmi>_der_*` (when the retailer publishes DER data) | Hourly poll |
| Substituted intervals | `sensor.meter_<nmi>_substituted_intervals` | With usage |
| Bank current and available balance (optional) | `sensor.bank_<account>_*_balance` | Live, cached up to 15 minutes |

Add the three external statistics in **Settings > Dashboards > Energy** under grid
consumption and return to grid. The first load imports 60 days of history (change it in
the integration options, up to 365). After that the last 7 days are re-imported every
3 hours so late and substituted reads are corrected.

Basic (accumulation) meter reads are not imported to statistics; only interval reads are.

## Scopes

| Scope | Why |
|---|---|
| `openid` | Identifies your openfeed user so one Home Assistant entry maps to one person |
| `offline_access` | Refresh tokens, so Home Assistant stays connected without signing in again |
| `openfeed-au:data:energy:read` | Accounts, balances, invoices, meters, usage and DER |
| `openfeed-au:data:banking:read` | Only when you tick "Also share banking data" |

Transactions, payment schedules and personal details are not requested.

## Setup

1. Install the integration (see below) and restart Home Assistant.
2. Go to **Settings > Devices & services > Add integration > openfeed Consumer Data Right**.
3. The first screen shows a public JSON Web Key Set (JWKS). Home Assistant generated the
   keys; the private keys never leave Home Assistant.
4. In a new tab, [register an app on openfeed](https://app.openfeed.au/registered-apps/new):
   - Client authentication: **private_key_jwt**.
   - Keys: **Inline JWKS JSON**. Paste the JWKS from step 3.
   - Scopes: energy, and banking if you want it.
5. Copy two values back into Home Assistant:
   - **OAuth2 client ID**, which starts with `app-`.
   - **App ID**, the bare UUID.
6. Check the Home Assistant URL. openfeed sends your browser back to
   `<url>/api/openfeed/callback`, so it must be reachable from the browser you sign in
   with. It does not need to be reachable from the internet.
7. Sign in to openfeed, approve the app, then choose the accounts to share.

To change which accounts are shared, use **Reconfigure** on the integration. The existing
grant is updated in place rather than a new one created.

## Costs

openfeed is free for consumers. As the registered app owner you are also the developer:
openfeed debits 1 credit per active grant when it is created and on each monthly
anniversary. The first 10 credits are free; see [openfeed pricing](https://openfeed.au/pricing).
When credits run out openfeed suspends the grant. Home Assistant raises a repair issue
and resumes automatically once you top up.

## Security

The integration follows openfeed's FAPI 2.0 profile:

- Pushed authorisation requests (PAR) with PKCE S256.
- `private_key_jwt` client authentication, PS256.
- DPoP sender-constrained tokens; every API call carries a fresh proof bound to the
  access token, with server nonces honoured.
- RFC 9207 `iss` check on the sign-in redirect, and id_token signature, audience and
  nonce verification.
- Diagnostics redact tokens, keys, account numbers, NMIs and meter IDs.

## Install

### HACS

Add `https://github.com/purcell-lab/ha-openfeed` as a custom repository of type
Integration, then install **openfeed Consumer Data Right**.

### Manual

Copy `custom_components/openfeed` into your Home Assistant `config/custom_components/`
directory and restart.

## Development

```
pip install -r requirements-test.txt
pytest
ruff check .
```

The OAuth, DPoP, API client and usage aggregation modules (`crypto.py`, `oauth.py`,
`api.py`, `usage.py`) have no Home Assistant imports and are tested on their own.
Config flow and entry setup are tested with `pytest-homeassistant-custom-component`.

## Disclaimer

This is an independent project. It is not affiliated with or endorsed by openfeed,
Biza Pty Ltd, or the ACCC. openfeed's terms govern your use of openfeed.

## Licence

MIT
