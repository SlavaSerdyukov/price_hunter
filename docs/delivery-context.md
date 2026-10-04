# Delivery context and delivered comparison

**Item price != delivered total. Market != destination. Unknown cost != zero.
Reference FX != landed-cost ranking. Existing watches remain item-price based.**

A BE catalog offer can deliver to NL only with explicit matching source evidence.
EU membership, merchant domicile or an unscoped shipping amount prove nothing.
The item reader, ProductWatch, BestPriceEvent, exact trackers and price history keep
their existing native item-price semantics.

## Settings and privacy

Telegram `/settings` separates Shopping market and Delivery to in all seven locales.
Set a country, optionally a postal code, or clear the destination. Postal normalization
changes only case/whitespace, preserves separators, rejects controls and bounds length
to 20 characters; there is no global postal interpretation or street address.
Changing delivery country clears the old postal preference unless explicitly replaced.
Existing watch markets, identities, baselines/history and alerts never change.

`GET/PATCH /api/v1/users/me/settings` returns only the current user's settings.
Nullable `delivery_country`/`delivery_postal_code` are independent from `country_code`.
`{"delivery_country":null}` clears both; postal without country fails. Raw postal text
exists only in user preferences. Quote rows use deterministic SHA-256 destination keys,
which minimize duplication but are not encryption or public identifiers. No postal text
in comparison DTOs, structured logs, runtime/coverage diagnostics, notification snapshots,
click analytics, merchant audits or feed reports.

## Explicit operations

Both POST operations require existing authenticated user context/shared rate limits.
Their `country` query parameter is the catalog market. Destination is in the body,
never a postal-bearing access-log URL. An omitted body uses saved user delivery settings;
without a saved destination the response is `invalid_delivery_context`.

```http
POST /api/v1/products/{id}/delivery-comparison?country=BE&page=0&size=10
Content-Type: application/json

{"country":"NL","postal_code":"1012 AB"}
```

This reads static/persisted evidence only. `POST /api/v1/products/{id}/delivery-quote`
with the same body explicitly requests bounded quotes. Ordinary search/product/offers
and Telegram pagination make no shipping calls. Telegram shows item price separately
from delivered cost; incomplete shipping/tax is never called free.

## Costs and evidence

Ancillary money requires finite non-negative Decimal values, at most four decimal
places and bounded NUMERIC precision. Zero is valid; floats and negatives fail.
Item Money remains positive. One domain function computes item + shipping + explicit
additional tax. INCLUDED/NOT_APPLICABLE contribute zero; ADDITIONAL requires an amount;
UNKNOWN tax or missing shipping gives no total. No VAT/customs guessing or double count.
Shipping/tax must share item native currency; currencies remain separate comparison groups.

Quotes bind one exact StoreOffer/source, item-price/URL snapshot, currency and explicit
COUNTRY or EXACT scope. BE/2000 cannot authorize BE/3500 or NL/1012; missing scope is
UNKNOWN. Destination stock is independent of general listing stock. Static and dynamic
evidence are selected as whole tuples: a current specific/newer compatible dynamic quote
supersedes static evidence. Costs/price/tax/availability/link never mix source listings.

Static ingestion and dynamic acquisition cache the domain-calculated total and its item
price binding. SQL validates policy, scope, currency, freshness and stock before ranking;
it does not repeat the arithmetic. Changing item price invalidates an old quote. Obtaining
shipping never writes PriceObservation or another shipping-history/event system.

Delivery responses add `delivery_country`, paginated `delivered_offers`, per-row
`delivered_total`, shipping/tax, destination availability and quote status/timestamps.
Each native currency group adds global `best_delivered_offer`, `delivered_price_spread`,
`delivered_offer_count`. Legacy item fields and `total_price` placeholder retain their meaning.
Each view selects one source per canonical merchant, without commission/payout ranking.

| Example | Item winner | Delivered winner |
| --- | --- | --- |
| Alpha 299 unknown shipping; Beta 309 free, tax included | Alpha 299 | Beta 309 |
| Alpha 299+25; Beta 309+0, tax included | Alpha 299 | Beta 309 |
| Coolblue/Awin 299 incomplete; Coolblue/CJ 305 complete | Awin source 299 | CJ source 305 |

Telegram displays canonical merchant names and the chosen source's own outbound link.
Delivery states are UNSUPPORTED, INCOMPLETE, STALE, FAILED and COMPLETE. A shipping
failure never deletes a product or changes general stock. Stale evidence cannot win.

## Budgets and cache

| Setting | Default | Bounds |
| --- | --- | --- |
| DELIVERY_QUOTE_LIMIT | 12 | 1–20 globally, across merchants/currencies |
| DELIVERY_QUOTE_TTL_SECONDS | 900 | 30–86400, further capped by policy |
| DELIVERY_QUOTE_TIMEOUT_SECONDS | 3 | 1–5 per candidate, including queue wait |
| DELIVERY_OPERATION_TIMEOUT_SECONDS | 10 | 1–15 for the provider stage |
| DELIVERY_QUOTE_CONCURRENCY | 4 | 1–4 |

SQL bounds fresh in-stock candidates before loading: one per merchant first, then
native-currency item-price order. Small synchronous concurrency avoids a second job
system. Existing provider limiter/refresh lease prevents concurrent duplicate acquisition.
No unbounded N+1 queries/calls; persisted comparison uses constant query count.
Latest cache is unique by offer/destination/scope/currency; failed/unsupported entries
expire after at most 30 seconds. Worker maintenance removes expired quotes in bounded
batches every 30 seconds; expiry blocks use immediately.
Only synthetic mock opts into cache persistence. Future adapters default to request-scoped
results; caching additionally requires existing catalog policy, reviewed adapter opt-in
and contractual TTL. No new policy permission is invented.

## Provider limits and migration

Only mock advertises DELIVERY_QUOTE: Alpha 299, BE shipping 25, NL unsupported;
Beta 309, BE shipping 0, NL shipping 8. Real eBay/WooCommerce/Awin/TradeDoubler/CJ
dynamic quoting stays unsupported. Awin/TradeDoubler amounts retain unknown scope.
CJ promotes only an audited matching country/currency amount without postal/region/location
qualifiers; tax remains UNKNOWN, so that shipping field alone cannot produce a total.
No new endpoint, live merchant activation or real credentials are used.

Migration `d7a3b951e620` preserves M0–M5A state including active validation leases.
Legacy shipping/tax remain unknown; listing postal text becomes an opaque key and its
duplicate column is removed. New user settings start null; no evidence is fabricated.
Downgrade refuses used delivery settings/evidence until exported/reconciled. The historical
verifier tests backfill, lease preservation, guarded rollback and re-upgrade.
Feed normalization revision `m5b-feed-v1` requires renewed technical validation before
an existing pilot resumes; it does not activate merchants.

Destination-aware watches, customs/duties, VAT guessing, weights, IP geolocation,
street addresses, shipping scraping and converted landed-cost ranking are out of scope.
