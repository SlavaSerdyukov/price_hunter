# M3A comparison core

A `Product` is one canonical purchasable product/variant. A `StoreOffer` is a listing
in a persisted `Store` marketplace. `Tracker` watches one listing; `ProductWatch`
watches a canonical product's best available price in one selected currency.

## Canonical identity

URL resolution and search both use `CatalogResolver`. Search persists provider DTOs
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
  match. Structured variant dictionaries must agree, including missing attributes.
  Different sizes, colors, capacities and conditions therefore remain separate.
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
owns price changes. M3A does not automatically repair or merge duplicate legacy Products.
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
offers. Store identity/name/country come from `Store`, not a guessed hostname. Disabled
or unsupported stores are excluded. `last_checked_at` makes freshness visible.

Recommendations and spread are calculated separately for each native currency:

- `best_available_offer` is the lowest priced confirmed `IN_STOCK` listing.
- `UNKNOWN` and `OUT_OF_STOCK` never displace a confirmed available recommendation.
- `cheapest_known_offer` includes all availability states and is labelled separately.
- `price_spread` is highest minus lowest **in-stock** item price; one available listing
  gives zero, no available listings gives null.
- With multiple currencies, top-level best/spread are null. Use `currency_groups`;
  there is no FX rate and EUR is never compared numerically against USD.
- Unknown `shipping_price`, `delivery_country`, `tax_included` and `total_price` remain
  null. Listed item price is not a promise of final delivered cost.

Products rank by exact model/GTIN relevance, availability, confidence, number of stores,
then stable title/UUID ties. Offers sort by currency, availability, price, store and ID.
No cross-currency price ranking or affiliate commission influences the result.

## Watches and notifications

Create a watch with `POST /api/v1/product-watches`:

```json
{"product_id": "PRODUCT_UUID", "currency": "EUR", "target_price": "310.00"}
```

Omit the optional target for an ordinary watch. Currency must have a known eligible
offer. Creation snapshots the current best without sending an alert; repeated creation
returns the same user/product/currency watch. `GET` lists owned watches, `PATCH /{id}`
changes target/flags/enabled, and `DELETE /{id}` is idempotent and ownership scoped.
`target_price: null` clears the target. `scheduled` distinguishes manual pause from
quota pause. The Telegram card has **Track best** per currency; **All offers** retains
exact-offer tracking. `/watches` lists best-price watches; `/my` lists exact trackers.
Target/notification-flag editing for watches is currently via REST.

Watches and exact trackers share the existing plan's stored-record quota and oldest-
enabled scheduling policy. A watch consumes one quota slot, while all known eligible
offers in its chosen currency participate in the shared refresh worker. The fastest
eligible subscriber's interval wins. A new/resumed watch can advance a slower existing
schedule. Price checking does not perform repeated discovery searches; future search
results added to the same Product also participate in the watch.

Each accepted observation atomically updates the watch's best state and emits at most
one prioritized event: target crossing, return of an available best, merchant change,
ordinary best-price drop. `notify_on_new_best` controls merchant/restock alerts;
`notify_on_price_drop` controls ordinary drops. Targets and restock alerts retain the
existing paid entitlements, checked again before delivery. Ordinary drops obey the
configured cooldown; merchant changes and target/restock crossings have priority.
Disabling a watch removes pending events; resuming resets its baseline without a
catch-up alert. Disabling notification flags suppresses future event creation.

Events use the existing outbox, with an immutable current/previous price and merchant
snapshot and unique `(watch UUID, evaluation sequence)` deduplication key. Replaying a
refresh or concurrent discovery does not repeat a logical event. Offer observations
remain the price-history source; best-price state and outbox snapshots are lightweight
derived data, not another observation stream. A public best-price history chart is not
part of M3A. Ambiguous Telegram sends retain the existing `uncertain` delivery semantics.

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
schema. Readiness checks require the comparison tables.

Revision `124441561d5e` adds identifiers, Product MPN, saved offer identity/confidence,
ProductWatch and outbox watch reference/snapshot. A CHECK requires exactly one outbox
subject (Tracker or ProductWatch). Existing billing/offer/tracker/history data is kept.
Downgrade refuses to discard existing watches: export and explicitly remove them first,
or restore the pre-upgrade backup; never force a downgrade over live watch data.

## Limits and next milestone

Matching deliberately favors duplicates over false positives. Existing WooCommerce
payloads often omit global/manufacturer identifiers, so many unrelated stores cannot
be reliably matched yet. ASIN is Amazon-specific. No new retailer integrations or LLM
matching were added. Summaries currently load known offers for one product in memory;
large catalogs need SQL aggregation, query/load measurement and bounded discovery jobs.

M3B should improve structured provider identity/variant evidence, expose operator
match diagnostics and reviewed duplicate repair, add freshness/stale-offer policy and
best-price history, and measure large-catalog scheduling. Destination-aware shipping,
tax and timestamped FX remain M4. Amazon activation retains its separate approval gate.
