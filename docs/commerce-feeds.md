# Commerce feeds

M4B keeps one catalog pipeline. FeedSource handles remote formats and yields bounded
FeedProductData streams. FeedStoreProvider searches local PostgreSQL staging and passes
selected ProductOfferData through CatalogResolver and SnapshotUpdater. Affiliate behavior
is metadata/policy; the unused AffiliateProvider hierarchy was removed.

## Generations and materialization

MerchantProgram identifies an approved network/advertiser/market and a stable Store.
MerchantFeedItem is searchable acquisition data, not a canonical Product. Importing a
100k-row feed does not create 100k canonical Products. Search/discovery materializes only
bounded relevant candidates; canonical identity still requires deterministic evidence.

FeedSyncState owns a PostgreSQL SKIP LOCKED claim, token, lease, heartbeat, source version,
generation, counters and retry schedule. A duplicate job cannot start an executing claim.
Expired owners cannot publish or overwrite a newer owner's state. Lost enqueues recover
on lease expiry. Network requests do not hold database transactions.

Each attempt batches candidates into FeedPendingItem. A fenced completion transaction
upserts the entire accepted generation and inactivates missing items. Current staging IDs
stay stable. Failed/truncated/schema-invalid/interrupted/changed-source attempts preserve
the previous generation. Identical duplicates are idempotent; conflicting duplicates fail
an attempt. Bulk completion updates PostgreSQL statistics to avoid stale tiny-table plans.

Retry resumes the durable workflow by restarting/replaying acquisition, not by guessing
an HTTP byte-range cursor inside a compressed file. Completed generations survive worker
restarts. Materialized listings catch up in bounded batches through the M4A.1 snapshot
path. Background updates require refresh permission checked in the locked transaction;
catalog-only updates happen on explicit search. Expired staging cannot renew a snapshot. FeedRevalidationContext is constructed from
the approved program and current active row of the last completed generation, under
the program lock. It also verifies that incoming data exactly matches normalized staging.
Search/discovery and background materialization pass this explicit context to SnapshotUpdater.
The updater has no network-name conditions.
Disappearance expires a listing without inventing stock or price observations and
without deleting Product, StoreOffer, watches, trackers or history.

## Search and policy

GTIN/EAN/UPC normalize to a common identifier. Exact manufacturer keys and PostgreSQL
GIN full-text retrieval have dedicated indexes. Text relevance may retrieve a candidate
but cannot authorize a merge. Size, color, capacity and material remain explicit variant
evidence. Parent IDs do not replace purchasable identity. Commission is excluded from
search/matching/price ranking. Markets and native currencies stay separate; FX is display-only.

PolicyResolver uses current merchant policies for SQL and object decisions. EXISTS queries
avoid an OR clause for each advertiser. Existing direct providers keep deployment policies.
Network enablement grants no merchant rights. Revocation hides prohibited comparison,
history and outbound surfaces without silently deleting previously authorized history.

## Bounds and operations

Remote requests use PublicHTTPTransport, fixed HTTPS hosts, no redirects, shared pacing
and safe errors. Limits cover compressed/decompressed bytes, rows, fields, metadata,
headers, pages, JSON nesting and logical CSV records. Incremental gzip rejects truncation,
extra archive members and decompression bombs. No ZIP extraction or merchant scraping.

Dry runs use a temporary on-disk index and read-only PostgreSQL queries. Reports include
parsed/valid/invalid/duplicate counts, identifier/variant/availability coverage, currencies
and proposed inserts/updates/deactivations. Logs include safe IDs/counts/error codes, never
credential URLs, raw remote errors or customer PII. Shared rate-limit waiting is separate
from the page request timeout.

Retention removes old abandoned pending rows and inactive staging in bounded batches.
Expired catalog-only staging also respects the reviewed cache limit; protected canonical
history remains separate. An unchanged source version skips downloading only while all
expected current rows remain cached. Cache eviction forces reacquisition on the next sync.
An unchanged version check by itself does not renew presence. M4B.1 uses **model B**:
skip downloading only while the previous full generation and all expected active rows
remain within the merchant cache limit. At expiry (including equality), download the full
feed again even if its version did not change. Next sync is capped by that expiry; a
successful skip cannot push the deadline later. This consumes full-download quota, so
operators must reconcile their permitted cache duration with actual network limits.

Active cache eviction makes the affected program immediately due in the same transaction.
Retention takes sync-state locks before deleting rows, skips active leases and rechecks
eligibility after locking. Cleanup of old inactive rows does not trigger another download.
The existing scheduler cadence still applies; this removes the multi-hour interval gap.

## Content versions and presence revalidation

`MerchantFeedItem.seen_at` means presence confirmed by a successfully completed full
generation. It advances along with generation/active state for every accepted present row,
even if fingerprint or `source_updated_at` is equal or older. Product data, fingerprint
and search-index fields only advance when content is eligible: equal/older real source
versions cannot overwrite newer content. Failed/partial generations confirm nothing.

An authoritative materialization can reactivate an identical StoreOffer and set its
`last_checked_at` to that confirmed `seen_at`, retaining the newer content timestamp and
price. This adds no PriceObservation and no price-drop event for an unchanged price.
Replaying the same current generation retains the same confirmation time; copied generation
IDs with altered payloads, obsolete generations and incomplete generations are rejected.
Generic SEARCH_SNAPSHOT inputs without that verified context still reject stale/equal
source versions without renewal. Acquisition time and content version are separate clocks.

```sh
uv run python -m pricehunter.apps.admin merchant-programs
uv run python -m pricehunter.apps.admin feed-list awin
uv run python -m pricehunter.apps.admin merchant-program-check PROGRAM_UUID
uv run python -m pricehunter.apps.admin feed-sync PROGRAM_UUID --dry-run
uv run python -m pricehunter.apps.admin feed-sync PROGRAM_UUID
uv run python -m pricehunter.apps.admin feed-status PROGRAM_UUID
uv run python -m pricehunter.apps.admin feed-diagnostics PROGRAM_UUID
uv run python -m pricehunter.apps.admin feed-search PROGRAM_UUID "Sony WH-1000XM6"
```

See [merchant onboarding](merchant-programs.md), [Awin](awin-setup.md),
[TradeDoubler](tradedoubler-setup.md), [design](m4b-design.md) and
[verification](verification.md). Fixtures do not establish live retailer coverage.
