# Product comparison — M3A / M3B

A `Product` is one canonical purchasable product/variant. A `StoreOffer` is a listing
in a persisted `Store` marketplace. `Tracker` watches one listing; `ProductWatch`
watches a canonical product's best available price in one selected currency.

## Canonical identity

URL resolution, search and autonomous discovery all use `CatalogResolver`. Search persists provider DTOs
first and groups by the resulting Product UUID, so search and later product reads
cannot invent different temporary groupings. The repository only writes catalog rows.

Resolution indexes evidence in `ProductIdentifier`: validated GTIN/EAN/UPC (padded to
14 digits), brand plus manufacturer model/MPN, Amazon-scoped ASIN, and merchant listing
identity. The unique `(product_id, kind, value)` constraint deduplicates evidence;
identifiers are not globally unique because variants and inconsistent seller metadata
can share them. Index source records the first contributing store. Match decisions have
`matched`, `confidence`, `method` and `reasons`; public results expose only confidence.

- NFKC, case folding and whitespace normalization apply throughout. Common separators
  in model codes are ignored: `WH1000XM6`, `WH-1000XM6` and `WH 1000 XM6` compare equally.
  Meaningful `+` and `/` remain. Known language aliases normalize colors, sizes and
  capacity; explicit UK/EU/US dimensions and unknown attributes retain their meaning.
- Conflicting trade identifiers, brands, manufacturer codes or Amazon ASINs veto a
  match. Identical validated GTINs tolerate missing optional color/size/capacity metadata,
  but explicit conflicting values always veto a match. Missing opaque variation IDs,
  condition or size-system attributes remain conservative barriers. Brand/model-only
  matches still require complete compatible variants.
- Compatible GTIN evidence scores 1.00; brand/model or brand/MPN evidence scores 0.95;
  a compatible Amazon ASIN match scores 0.98. Without sufficient evidence the listing
  keeps its own Product. An initial isolated listing is not proof of a cross-store match.
- Title similarity never authorizes a merge. Explicit size/capacity conflicts in titles
  can veto one. Retailer SKU is not treated as manufacturer MPN.
- An incoming listing must match **every** existing listing's saved evidence. Prefer a
  unique strongest candidate; ambiguous candidates stay separate. This prevents a weak
  brand/model listing from bridging incompatible GTINs.
- Stable identity hashes exclude titles. PostgreSQL transaction advisory locks on sorted
  evidence and unique product/listing keys serialize concurrent provider discoveries.
  New observations lock the Product before updating offers/watch state.

Existing listing resolution reuses stored prices and identity; the refresh pipeline
owns price changes. M3B adds diagnostics, but does not automatically merge legacy Products.
The resumable backfill indexes their evidence without changing IDs, trackers or history.
Provider-specific opaque variation IDs remain conservative barriers to cross-store
matching; missing/incompatible metadata can intentionally leave duplicates.

## Comparison and ranking

`GET /api/v1/search` now returns `products`, replacing the old `groups` array. Every
comparison has an `id`, canonical attributes, `offers`, `store_count`, `offer_count`,
`currencies`, `currency_groups` and `match_confidence`. Search limits count canonical
products, not individual offers. Successful providers remain usable when another fails;
`unavailable_providers` reports failures. At most 100 results per provider are persisted
per search; each adapter may impose a smaller result limit.

`GET /api/v1/products/{id}` returns ten offer previews. The `/offers?page=0&size=10`
endpoint pages up to 50 offers and includes summaries across **all** eligible known
offers. Merchant identity/name come from `Store`, not a guessed hostname. Market comes
from provider evidence when supplied, otherwise Store.country. Disabled,
unsupported and policy-ineligible offers are excluded. Offer DTOs add `freshness` (`fresh`, `stale`, `failed`),
`age_seconds`, and `stale` (true for any non-fresh value), alongside `last_checked_at`.
Currency summaries add fresh/stale/failed counts and `cheapest_stale_offer`.
Product DTOs include bounded provider/country/currency `discovery` status entries.

Recommendations and spread are calculated separately for each native currency:

- `best_available_offer` is the lowest priced **fresh** confirmed `IN_STOCK` listing; null if none exists.
- `UNKNOWN` and `OUT_OF_STOCK` never displace a confirmed available recommendation.
- `cheapest_known_offer` includes all availability states and is labelled separately.
- `price_spread` is highest minus lowest **fresh in-stock** item price; one available listing
  gives zero, no available listings gives null.
- With multiple currencies, top-level best/spread are null. Use `currency_groups`;
  native EUR is never compared numerically against native USD. Optional timestamped
  [ECB reference conversions](fx.md) are display values and do not change ranking.
- Unknown `shipping_price`, `delivery_country`, `tax_included` and `total_price` remain
  null. Listed item price is not a promise of final delivered cost.

Products rank by exact model/GTIN relevance, availability, confidence, number of stores,
then stable title/UUID ties. Offer pages sort by currency, freshness, availability, price, store and ID.
SQL counts and window ranks build whole-product summaries in three queries; a complete
comparison uses a bounded number of queries regardless of 100, 500 or 1000 listings.
M3B's core aggregation used six; M4A adds account context and, when enabled, a shared
persisted FX snapshot read, without per-offer queries or network calls.
At most 50 paginated offer bodies and three summary offers per currency are materialized.
No cross-currency price ranking or affiliate commission influences the result.

## Watches and notifications

Create a watch with `POST /api/v1/product-watches`:

```json
{"product_id": "PRODUCT_UUID", "market_country": "BE", "currency": "EUR", "target_price": "310.00"}
```

Omit the optional target for an ordinary watch. Currency must have a known eligible
offer. Creation snapshots the current best without sending an alert; repeated creation
returns the same user/product/market/currency watch. An omitted market uses the saved
account country; without either, creation requires country selection. Profile changes
preserve existing watches and their discovery markets. `GET` lists owned watches, `PATCH /{id}`
changes target/flags/enabled, and `DELETE /{id}` is idempotent and ownership scoped.
`target_price: null` clears the target. `scheduled` distinguishes manual pause from
quota pause. The Telegram card has **Track best** per currency; **All offers** retains
exact-offer tracking. `/watches` lists best-price watches; `/my` lists exact trackers.
Target/notification-flag editing for watches is currently via REST.

Watches and exact trackers share the existing plan's stored-record quota and oldest-
enabled scheduling policy. A watch consumes one quota slot, while all known eligible
offers in its chosen currency participate in the shared refresh worker. The fastest
eligible subscriber's interval wins. A new/resumed watch can advance a slower existing
schedule. Known-price refresh and autonomous discovery have separate schedules. Discovery
shares one target per Product/provider/country/currency; newly attached offers participate
immediately in comparison, watch evaluation and the normal refresh pipeline.

Each accepted observation or discovery batch atomically updates the watch's best state and emits at most
one prioritized event: target crossing, return of an available best, merchant change,
ordinary best-price drop. `notify_on_new_best` controls merchant/restock alerts;
`notify_on_price_drop` controls ordinary drops. Targets and restock alerts retain the
existing paid entitlements, checked again before delivery. Recovery after freshness expiry
uses `best_prices_refreshed`, not a stock claim; that event is available to Free watchers. Ordinary drops obey the
configured cooldown; merchant changes and target/restock crossings have priority.
Disabling a watch removes pending events; resuming resets its baseline without a
catch-up alert. Disabling notification flags suppresses future event creation.

Events use the existing outbox, with an immutable current/previous price and merchant
snapshot and unique `(watch UUID, evaluation sequence)` deduplication key. Replaying a
refresh or concurrent discovery does not repeat a logical event. Offer observations
remain the exact-listing history source. `ProductBestState` and `BestPriceEvent` store
only meaningful per-currency best transitions. The bounded, authenticated endpoint
`GET /api/v1/products/{id}/best-price-history?currency=EUR&limit=50` returns chronological
points, current best and the minimum observed best in the plan window. Free/Pro/Power
default to 7/90/365 days. See [history and retention](discovery.md#history-and-retention).
`POST /api/v1/products/{id}/refresh` returns HTTP 202 with `accepted` and `queued_count`;
it schedules stale/failed listings without waiting for retailer HTTP calls. Ambiguous Telegram sends retain the existing `uncertain` delivery semantics.

## Deterministic demo

With the mock provider enabled, search `/search Sony WH-1000XM6`:

- One black product has Demo Alpha EUR 329, Demo Beta EUR 345, cheaper unavailable/
  unknown listings and a separate USD group. EUR available spread is 16.
- The white variant and conflicting-GTIN item remain separate products.
- Choose **Track best** in EUR. On refresh Alpha becomes EUR 349 and Beta EUR 319;
  the watch follows Beta and emits `merchant_became_cheapest` once. Depending on refresh
  completion order an intermediate legitimate best may be observed.
- `/search ZX200` demonstrates brand/model matching without GTIN and separate 128/256 GB
  variants. Demo prices are fixtures, not retailer quotes.

Adapter fixtures additionally cover eBay marketplaces, Amazon ASIN/variant matching,
and WooCommerce's insufficient SKU/title evidence. Amazon remains disabled pending
access/approval; these tests make no live Amazon claim.

## Upgrade an existing installation

Stop API, bot and worker first, take a PostgreSQL backup, then run with the new code:

```bash
uv run alembic upgrade head
uv run python -m pricehunter.apps.admin catalog-backfill
# Repeat catalog-backfill until it prints "Indexed 0 legacy products".
uv run alembic check
```

For Docker use `docker compose --profile app build`, then
`docker compose --profile app run --rm api` before each command above, omitting `uv run`.
Restart application services after backfill. New installations simply migrate an empty
schema. Readiness checks require the comparison/discovery tables and refresh-request column.
M3B revision `2872920b653a` adds discovery, best-state/history tables, nullable refresh-request
and watch-absence columns, and product/currency indexes. Existing records are preserved.
The worker initializes history for existing watched products in bounded batches; it does
not invent past best-price transitions from listing observations. An M3B downgrade refuses
to discard best-price history. Restore a backup or export and explicitly remove that
history before a reviewed downgrade.

Revision `124441561d5e` adds identifiers, Product MPN, saved offer identity/confidence,
ProductWatch and outbox watch reference/snapshot. A CHECK requires exactly one outbox
subject (Tracker or ProductWatch). Existing billing/offer/tracker/history data is kept.
Downgrade refuses to discard existing watches: export and explicitly remove them first,
or restore the pre-upgrade backup; never force a downgrade over live watch data.

## Limits and next milestone

Matching deliberately favors duplicates over false positives. Existing WooCommerce
payloads often omit global/manufacturer identifiers, so many unrelated stores cannot
be reliably matched yet. ASIN is Amazon-specific. Rakuten adds a credential/policy-gated
source without changing deterministic matching. Summaries use bounded SQL aggregation
and paginated offer bodies.

Operator diagnostics and read-only duplicate candidates are available; merging is deferred
until audited identity redirects and watch/outbox/history reconciliation are designed.
See [operations and limitations](discovery.md). [M4A](international-commerce.md) adds
durable markets, affiliate links and timestamped reference FX. M4B should add confirmed
delivery/shipping/tax context without weakening native-currency comparisons. Amazon activation
retains its separate approval gate.
