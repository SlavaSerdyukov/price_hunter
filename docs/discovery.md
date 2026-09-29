# Autonomous discovery and freshness — M3B

`ProductWatch` authorizes two shared schedules: refresh known StoreOffers, and search
configured providers for additional matching listings. Both use the existing provider
registry, limits, canonical resolver and notification outbox. No billing rules change.

## Freshness and current best

Freshness is derived at read/evaluation time from `last_checked_at` and
`OFFER_FRESHNESS_SECONDS`. Exact provider names override the category before the first
underscore, then `default` applies. For example `woocommerce_hemptees_be` uses
`woocommerce` unless that exact provider has an override. Thresholds are strict:
age equal to the TTL is already stale.

| Default policy | Fresh for |
| --- | --- |
| eBay | 18 hours |
| WooCommerce / other | 24 hours |
| Mock | 5 minutes |
| Amazon | 0: never assumed fresh until an approved policy is configured |

Any failed refresh, pending anomalous-price quarantine or clock more than 60 seconds
in the future is `failed`, even if the last good price is recent. The last good price
remains visible. A successful normal refresh clears failure/quarantine as appropriate.
Stock availability is separate: a fresh out-of-stock listing is not a recommendation.
Restock alerts require previously confirmed fresh out-of-stock evidence across the
whole market/currency group. Unknown or stale stock recovering uses a price-refreshed message.

Only fresh, confirmed in-stock prices can supply `best_available_offer` or spread.
If EUR 299 is stale and EUR 319 is fresh, current best is EUR 319. With no fresh stock,
current best is null. `cheapest_known_offer` includes all states;
`cheapest_stale_offer` separately identifies the cheapest timed-out observation.
DTO `stale` means non-fresh, including failed; inspect `freshness` for the exact reason.
Telegram labels freshness/age and offers a background refresh button. M4A optionally
adds [ECB reference displays](fx.md); they do not change native ranking or establish a
shipping/tax/destination guarantee. Reviewed provider cache limits cap freshness.

The reader uses three SQL queries for full summaries, bounded per-currency ranked
offer bodies, and a separate paginated query. The product/market/currency index supports
those filters. Expiry maintenance uses indexed `ProductBestState.next_evaluation_at`, avoiding
an unbounded scan of offer timestamps. Readers never rely on the sweep to reject stale data.
Maintenance locks bounded product rows and evaluates only due or missing watched market
series. Watch summaries are shared by market across users; 100 BE/EUR subscribers do
not require 100 comparison summaries. Legacy watch baselines rebuild silently.

## Shared discovery

`ProductDiscovery` has a unique `(product_id, provider, country, currency)` key.
One hundred watchers with the same context share one target and one provider search.
Country is persisted as ProductWatch.market_country; currency is the watch's native
comparison currency. Only legacy migration uses saved account country or BE. New watches
require an explicit/saved country. Profile edits never retarget existing watches.
This context is not proof of shipping availability. See [M4A](international-commerce.md).

The existing entitlement policy selects enabled watches inside the current shared
watch/tracker quota. For each context the fastest eligible plan sets the cadence:

| Plan | Default discovery interval |
| --- | --- |
| Free | 7 days |
| Pro | 24 hours |
| Power | 6 hours |
| Mock override | 10 minutes, for deterministic demos |

These are `DISCOVERY_PLAN_SECONDS` and `MOCK_DISCOVERY_SECONDS`. Downgrade/expiry uses
current entitlements, independent of billing cleanup. Faster eligible plans can advance
an old slower schedule. Paused, quota-excluded and no-longer-interested contexts do no
new work. Dormant target rows are retained and reused if interest returns.

Each discovery cron (once per minute, also at worker startup) inserts at most
`DISCOVERY_BATCH_SIZE` missing targets **per provider**, then claims up to that many
due targets overall. Defaults: 20 targets, 50 returned results, 180-second lease.
`FOR UPDATE SKIP LOCKED`, UUID fencing, lease expiry and a Redis execution lock handle
concurrent workers. ARQ jobs are temporary; the database owns the schedule. Lost enqueue
recovers after lease expiry. Discovery HTTP runs outside database transactions.

Providers declare capabilities: URL resolution, keyword/GTIN/model/ASIN search, refresh,
variants, summary-detail enrichment and supported discovery countries. Deterministic query priority is GTIN,
brand + manufacturer MPN, brand + model, then Amazon-scoped ASIN where supported.
An old merchant title is never an identity fallback. Insufficient identity is recorded
as unsupported and retried on its cadence. Existing public WooCommerce APIs often lack
strong identifiers, so safe attachment can yield no matching results.

For eBay, search summaries are enriched with full item details before matching; identifiers
are never inferred from the query. Each detail request consumes another shared provider
operation, and the entire search/enrichment batch retains its hard timeout. This follows
the [eBay Browse search → getItem workflow](https://developer.ebay.com/api-docs/buy/api-browse.html).
A listing that disappears between those requests is skipped; other detail failures back
off the target instead of pretending the search was empty.

Every bounded result goes through `CatalogResolver` and must resolve unambiguously
to the target Product. Wrong market/currency/provider, explicit conflicts and other products
are not attached. A transaction locks identifier evidence in deterministic order,
persists compatible new listings, then evaluates canonical history and all eligible
watches in the target market once. Expiry/fence checks prevent late batches from committing. Replaying a
batch cannot duplicate listings, observations, history or logical alerts. Existing
REFRESH-capable listings keep their saved price; ordinary refresh owns those updates.
For a SNAPSHOT_REFRESH provider without REFRESH, authorized discovery/search uses a
separate snapshot updater under product/listing locks. Identity stays immutable; an
optional actual source timestamp prevents stale versions overwriting newer data.
Without a source timestamp, serialized acceptance time determines order. Identical
snapshots renew freshness without duplicated history or accumulated counters.

## Failure semantics and limits

Discovery shares existing provider concurrency and request limits with resolve/search/
refresh. `PROVIDER_TIMEOUT_SECONDS` bounds network operations. Errors get exponential
backoff (120 seconds initially, plus 0–30 seconds jitter, capped at one day). Repeated
provider failures activate a Redis circuit for `DISCOVERY_SUPPRESSION_SECONDS` (one hour)
after `DISCOVERY_FAILURE_THRESHOLD` failures (five). Rate-limit rejections back off
without incrementing the provider-failure circuit. Other providers keep running.

API discovery status distinguishes `pending`, `running`, `ok`, `empty`, `unsupported`
and `backed_off`, with last success, next attempt and a safe error code. `empty` means
the bounded successful search produced no safely attachable results, not proof that a
retailer never sells the product. Failure preserves all catalog data and last success;
Telegram shows partial-search status. It never converts a failed search into “no offers”.

`POST /api/v1/products/{id}/refresh` responds 202 with `accepted` and `queued_count`.
The button/API advances up to `COMPARISON_REFRESH_LIMIT` stale/failed listing schedules
(default 20). It does no retailer HTTP and shares the normal worker. Active leases,
unsupported/disabled stores and unconfigured providers are excluded. Limits: three
requests per user/hour and one per product/five minutes, plus the existing user limit.
Requests without a watch are eligible for five minutes; sustained backlog may require
another request. Provider limits, refresh fencing and anomaly checks still apply.

## History and retention

`ProductBestState` holds current derived state and sequence per product/market/currency. Immutable
application-level `BestPriceEvent` rows record only changes: `initial_best`,
`price_changed`, `merchant_changed`, `became_unavailable`, `restored`. Product row locks
serialize changes; unique product/market/currency/sequence prevents duplicate events. Where
available, `source_observation_id` refers to the accepted listing observation.

The authenticated `/api/v1/products/{id}/best-price-history?country=BE&currency=EUR&limit=50`
returns chronological points, fresh current best and minimum observed best. The maximum
is 200 points; Telegram requests ten. History access and duration use the existing plan
policy (7/90/365 days by default), capped by configured retention when enabled.
A preceding transition may appear at the window boundary as `window_start` without
disclosing older timestamps. The minimum uses the entire permitted window, including
its anchor, even when the returned points are limited. History records observations and
evaluation transitions, not proof of continuous stock or a transaction price.

A bounded sweep every 30 seconds initializes missing watched market series and records
freshness expiry. Pre-M4A.1 global history remains stored separately with NULL market;
scoped history starts at its first evaluation and never borrows old global points.
Migrated baselines rebuild without alerts, including watches paused by plan quota, so
rebuild markers cannot repeatedly occupy maintenance batches. HTTP current-best evaluation remains correct while the worker
is delayed; event timestamps reflect evaluation time.

`HISTORY_RETENTION_DAYS=0` preserves data. With a positive value, run:

```bash
uv run python -m pricehunter.apps.admin prune-history
```

Repeat for bounded batches. Best-price pruning deletes up to 5000 older transitions,
retaining one pre-window anchor per product/market/currency and the current state. Pruning
listing observations sets event source references to null. Payments are never pruned.

## Matching and operator tools

Identical validated GTIN + missing optional size/color/capacity can match. Explicit
conflicting values reject even identical GTIN. Brand/model-only evidence remains strict;
unknown condition, opaque variation ID or size system does not become optional. Every
saved listing must be compatible, so a metadata-poor result cannot bridge conflicting
strong evidence. Title similarity never authorizes matching.

```bash
uv run python -m pricehunter.apps.admin product-diagnostics PRODUCT_UUID
uv run python -m pricehunter.apps.admin product-diagnostics PRODUCT_UUID --compare-with OTHER_UUID
uv run python -m pricehunter.apps.admin duplicate-candidates --limit 20
```

These local-only reports are read-only. Diagnostics show up to 50 offers, 200 indexed
identifiers, saved evidence, confidence, reconstructed reasons, freshness and reference
counts. Reasons compare against the first saved listing; they are not an original
decision audit. Duplicate reports group shared GTIN/brand-model/Amazon ASIN evidence,
with at most 100 candidate groups and ten sample product IDs per group. Explicit variants
may legitimately share an identifier: candidates always require review.

**Product merging is deferred for data safety.** A reliable repair needs audited identity
redirects plus reconciliation of duplicate watch settings, discovery leases, immutable
history, Tracker ownership and pending outbox snapshots. There is no automatic merge
or mutation behind these diagnostic commands.

Structured logs include discovery start/completion/failure/result, canonical offer
addition, stale evaluation, best-price/merchant change and duplicate candidates. They
contain safe IDs/provider/store/error codes, never credentials or provider response bodies.

## Deployment and checks

Follow [comparison upgrade steps](comparison.md#upgrade-an-existing-installation): stop
app processes, back up PostgreSQL, migrate, check schema, then restart. The migration is
additive; no existing Product/offer/watch/observation/payment IDs change. M3B downgrade
refuses to discard populated best-price history. Restore a backup or export and explicitly
remove the history before a reviewed downgrade.

With `TEST_DATABASE_URL` pointing at a dedicated database ending `_test`, the disposable
migration verifier creates its own database on that server and removes it afterwards:

```bash
PYTHONPATH=src uv run python scripts/verify_m3b_migration.py
```

Acceptance covers the old stale EUR 299 vs fresh EUR 329/345, discovery at EUR 315,
expiry/recovery through EUR 320 then EUR 310; one transition/alert per accepted change.
See [verification](verification.md) for executed checks and remaining live integration gates.
Amazon remains disabled while approval is pending; fixture tests do not claim live access.

## M4A provider permissions

Discovery requires reviewed tracking permission; refresh scheduling additionally needs
refresh permission and a real REFRESH capability. Rakuten uses exact brand/model phrase
search with returned validated UPC evidence, never an invented GTIN query. It has no
item refresh capability. Repeated Product Search results can renew the existing snapshot;
search-only policy creates no observations or watch alerts, and expired unreferenced
offers are evicted under their cache policy.
Pagination and retries consume the shared per-request Rakuten budget. See
[Rakuten setup](rakuten-setup.md) and [provider policy](international-commerce.md).
