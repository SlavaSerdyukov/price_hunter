# M3B design and baseline

Baseline on 2026-09-20: M3A commit `6da2c03`, 337 tests passing, 88.51% coverage,
PostgreSQL 17 and Redis DB 15 in isolated test services. Existing Product/StoreOffer,
CatalogResolver, subscriptions, shared refresh leases and outbox remain authoritative.
Work proceeds on `feat/m3b-autonomous-discovery`; billing is not refactored.

## Design decisions

1. Derive freshness from the current UTC time, per-provider/category TTL, failures and
   quarantined prices. Fresh confirmed stock alone can be recommended. Show stale/failed
   listings, age, counts and cheapest-known values separately. Amazon has no assumed
   freshness allowance until its approved policy is configured.
2. Use SQL aggregates and ranked offer IDs for full currency summaries, with bounded
   paginated offer bodies. Memory/query count must not grow with listing count. Index
   product/currency predicates; avoid a timestamp index without a query that uses it.
3. Add ProductDiscovery keyed by product/provider/country/currency. A bounded reconciler
   creates missing targets for scheduled watches; disabled/expired/quota-paused watches
   cannot authorize discovery. Leased SKIP LOCKED claims, token fencing, retry/backoff and
   provider-wide temporary suppression handle worker/provider failure. Provider network
   calls occur outside transactions. Cadence uses the fastest eligible interested plan.
4. Provider capabilities and DiscoveryQuery choose GTIN, brand/MPN, brand/model or
   Amazon-scoped ASIN, never a stale merchant title. All results pass through CatalogResolver;
   discovery requires its resolution to match the target Product. A bounded result batch
   commits once under its lease, then evaluates watches once. Existing listings are not
   repriced from search results; known-offer refresh remains a separate schedule.
5. ProductBestState and append-only BestPriceEvent record meaningful canonical transitions
   once per product/currency, independently of watcher count. Product locking and sequence
   uniqueness serialize history and existing outbox evaluation. A bounded expiry sweep
   records freshness-only transitions without waiting for another user search. Reads
   derive freshness at request time, never trusting a cached best beyond expiry.
6. Watch creation/resume establishes a silent baseline. Distinguish recovery of stale
   prices from genuine restock. Existing notification flags, paid gates and outbox retry
   semantics remain. History is bounded by currency, plan duration and configured retention;
   retention preserves a boundary anchor and the latest canonical state.
7. Same validated GTIN can tolerate missing optional variant attributes; explicit conflicts
   still veto. Brand/model-only matches remain strict. Opaque listing-specific variation
   identifiers and condition/size-system differences are treated conservatively. Never
   let missing evidence bridge conflicting listings in one canonical Product.
8. Add read-only operator diagnostics and duplicate reports. Automatic product merge is
   deferred: safe repair needs an audited redirect/merge ledger plus explicit reconciliation
   of duplicate watch targets, discovery leases, immutable transition histories and queued
   notification snapshots. Diagnostics must not imply that a candidate is safe to merge.
9. API/Telegram expose freshness, text best-price history and a rate-limited bounded refresh
   request that only advances existing offer schedules. It does not make synchronous
   retailer calls or create per-user refresh jobs. All text covers seven languages.

## Acceptance and rollout

Test stale cheaper offers, shared discovery for 100 watchers, concurrent claims/fencing,
provider failure isolation, duplicate batches, cheaper new merchants, freshness expiry and
recovery, optional-vs-conflicting evidence, per-currency history/retention/ownership,
Telegram flows, and fixed query counts at 100/500/1000 offers. Run the complete old suite,
>=85% coverage, Ruff/mypy, upgrade/schema checks and data-preservation migration checks.
Update docs before rollout; back up and stop local services for the additive migration.
