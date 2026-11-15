---
title: Privacy policy
---

# Privacy policy

This policy covers the openfeed Consumer Data Right integration for Home Assistant
("the integration"), published by purcell.id.au Pty Ltd at
https://github.com/purcell-lab/ha-openfeed.

## Bottom line

The integration runs inside your own Home Assistant. It sends your data nowhere else.
The publisher never receives, sees or stores your data.

## Who holds your data

| Party | Role |
|---|---|
| Your energy retailer and bank | Hold your data and share it with openfeed under the Consumer Data Right |
| openfeed (Biza Pty Ltd) | An accredited data recipient that shares your data with apps you approve |
| You, as the registered app owner | You register the openfeed app yourself, so you are both the consumer and the developer |
| Your Home Assistant | Stores the data the integration reads |
| purcell.id.au Pty Ltd | Publishes the code only. Receives no data |

## What the integration reads

Only what you approve in openfeed, and only these scopes:

- `openfeed-au:data:energy:read`: energy accounts, plans, balances, billing
  transactions, invoices, meters, interval usage and DER register records.
- `openfeed-au:data:banking:read`: bank account list and balances, only if you
  choose to share banking data.
- `openid` and `offline_access`: your openfeed user ID and a refresh token so
  Home Assistant stays connected.

It does not request bank transactions, payment schedules or customer details.

## Where data is stored

- Tokens and the integration's private keys are stored in your Home Assistant
  configuration. The private keys are generated in Home Assistant and never leave it.
- Usage, power and money series are stored as Home Assistant long-term statistics.
- Current values are stored as sensor states and in Home Assistant's history.

Retention follows your Home Assistant recorder settings. Long-term statistics are kept
until you delete them.

## How to stop sharing and delete data

1. Revoke the grant in your [openfeed dashboard](https://app.openfeed.au). The
   integration stops collecting at once, discards cached data and tokens, and asks you
   to reconnect.
2. Delete the integration in Home Assistant to remove tokens, keys and sensors. The
   integration also revokes its refresh token at openfeed as it is deleted. The
   disclosure grant stays listed in openfeed until you revoke it there (step 1).
3. Delete the `openfeed:` statistics in **Developer tools > Statistics** to remove history.

## Diagnostics

Home Assistant diagnostics for the integration redact tokens, keys, account numbers,
names, NMIs, meter IDs and equipment identifiers. Check a diagnostics file before you
share it.

## Contact

Raise an issue at https://github.com/purcell-lab/ha-openfeed/issues.
