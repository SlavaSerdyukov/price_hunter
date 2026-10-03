# M4B — commerce feeds

Design before adapters, 2026-09-29. Starting main: `e9dde18de3d805aaaa971e534677ced766bc13ae`
(M4A/M4A.1 merged in PR #3). Baseline: **513 passed, 90.10% coverage**.
Seven new pre-implementation acceptance cases failed as expected (including 1k/100k
streaming cases). Full commands and final results are recorded in verification.md.

## Acquisition and catalog

Remove the unused AffiliateProvider hierarchy. FeedSource produces bounded, normalized
FeedProductData streams; one FeedStoreProvider per network searches local PostgreSQL
staging and produces ProductOfferData. Affiliate behavior remains policy and metadata.
All selected results use CatalogResolver and SEARCH_SNAPSHOT / SnapshotUpdater. No
per-merchant providers, automatic cross-network merchant merging, or second catalog.

MerchantProgram identifies network + advertiser + market, binds a stable Store, records
approval/review and a validated ProviderDataPolicy, and stores only a safe feed ID/config.
Store identity is deterministic from network/advertiser, independent of feed display names.
Existing direct providers keep their deployment policies without new onboarding rows.
PolicyResolver provides both object decisions and SQL EXISTS predicates over program
rows. Network enablement is only an operational gate; it does not grant merchant rights.
Offer-to-program links let comparisons, history, workers, retention and outbound handling
resolve current permissions without generating an OR clause for every merchant.

## Generations and bounds

MerchantFeedItem is current searchable staging with stable program/listing identity.
FeedPendingItem holds a leased attempt's candidates; incomplete attempts never update
current items. A fenced completion transaction upserts the complete candidate generation
and inactivates missing rows. Failed attempts retry from the beginning (safe replay rather
than unsafe byte-range resume of compressed files). Completed generation state survives
worker restarts. Materialization catches up separately in bounded batches, using existing
snapshot code. Missing listings expire without manufacturing stock or observation events.

FeedSyncState uses PostgreSQL SKIP LOCKED claims, expiring tokens, heartbeat, backoff and
bounded scheduling. There is no per-product permanent job. Pending/inactive staging has
bounded retention independent of canonical history. Dry runs use transient scratch state
and report counts without mutating persistent feed/catalog data.

Streaming HTTP reuses PublicHTTPTransport, fixed HTTPS hosts, no redirects, shared rate
limits and safe error codes. Bound headers, compressed/decompressed bytes, individual
records/fields, JSON nesting, metadata, rows and pages. Incremental gzip rejects truncated
or concatenated archives and bombs; no filesystem archive extraction. CSV supports quoted
newlines with bounded logical records. TradeDoubler pages remain individually bounded.

## Retrieval and source adapters

PostgreSQL indexes support program/identifier equality, normalized brand/model/MPN and
GIN text search. Candidate relevance cannot authorize canonical merging. Variants remain
explicit, including fashion size, color and material; parent IDs never replace identity.
Commission is excluded from retrieval, canonical matching and price ranking.

Awin uses its documented publisher CSV/gzip Create-a-Feed format and feed-list discovery.
TradeDoubler uses documented productFeeds, productsUnlimited with bounded pagination,
and lastUpdated version checks. Adapter source audits are in their setup documents.
Authentication/URLs exist only at the adapter boundary; secrets stay in environment.
Production sources default disabled. Fixtures and synthetic feeds require no approvals.

## Acceptance and non-goals

First reproduce missing support with tests for two merchants on one network, different
merchant policies, 100k-row bounded streaming, interrupted generations, BE/DE GTIN sharing
and fashion variants. Extend with security, concurrency/fencing, replay/disappearance,
dry-run, indexed search, policy revocation, commission neutrality and migration preservation.
Run existing CI gates with >=85% coverage. No new Amazon/CJ/scraping/billing/frontend work.
