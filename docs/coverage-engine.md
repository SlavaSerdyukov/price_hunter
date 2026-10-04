# Coverage engine — M4C

M4C extends the existing catalog and feed engine. CJ uses FeedSource → bounded acquisition
→ MerchantFeedItem → FeedStoreProvider → ProductOfferData → CatalogResolver →
SnapshotUpdater → StoreOffer. No second product/comparison database or service is added.
See [CJ's official audit and setup](cj-setup.md) and [the implementation design](m4c-design.md).

## Search outcomes

The internal ProviderSearchOutcome distinguishes NETWORK_FAILED, PARTIAL, SUCCESS and
EMPTY, with aggregate result/accepted/rejected/duplicate counts and bounded safe codes.
The existing public response remains products plus unavailable_providers; internal outcomes
are excluded from serialization. Only a failed provider request enters unavailable_providers.
An individually invalid, unauthorized, wrong-market or persistence-rejected listing cannot
hide valid results from another merchant in that same network. Each persists in its own
transaction; safe logs contain provider and code, never raw responses, queries or tokens.

Work budgets are independent of subscription output limits:

| Setting | Default | Maximum |
| --- | ---: | ---: |
| SEARCH_PROVIDER_CANDIDATE_LIMIT | 100 | 200 |
| SEARCH_PERSISTENCE_LIMIT | 200 | 1000 |
| SEARCH_COMPARISON_LIMIT | 100 | 200 |
| SEARCH_ERROR_LIMIT | 20 | 100 |

Provider candidate slices retain the provider's returned relevance order. Ordered
listing-identity deduplication keeps the first logical position and original raw provider
rank; replacement selects the newest deterministic content version without moving that
position. Identical provider/store/external-ID/market/normalized-variant identities are
processed once. Distinct variants and markets remain separate. Provider sequences are
interleaved round-robin before the global persistence cap, so one provider cannot consume
the whole budget merely because it was configured first.

M4C.1 uses two bounded ranking stages:

```text
provider retrieval order + ordered deduplication
    → round-robin bounded persistence, rejecting wrong markets/currencies
    → cheap query-evidence canonical shortlist
    → bounded ComparisonProduct construction
    → existing final comparison_rank + entitlement output limit
```

The reusable `query_evidence_rank` helper evaluates normalized, deterministic evidence:

| Priority (lower first) | Query evidence |
| ---: | --- |
| 0 | Exact valid normalized GTIN/EAN/UPC |
| 1 | Exact MPN or model code |
| 2 | Exact brand plus MPN/model code |
| 3 | Normalized exact title |
| 4 | Normalized query contained in title |
| 5 | Other provider-retrieved candidates |

Trade queries use the existing length/checksum/ASCII validation and padded trade-ID
normalization. A nonmatching/conflicting provided trade identifier cannot gain an exact
identifier-query boost from its title or model. Model codes only remove existing common
formatting separators; no fuzzy/semantic/LLM matching is introduced. This priority is
retrieval evidence only and is never passed to CatalogResolver as merge authorization.

For each successfully persisted canonical Product, search retains its **best** evidence
across merchants, then original provider rank and interleaved retrieval position as ties.
The map has at most search_persistence_limit entries. It selects at most
search_comparison_limit Products before building their expensive comparisons; it does not
build every Product to discover relevance. Canonical UUID and listing IDs never choose
this shortlist. Multiple merchant offers resolving to one Product consume one slot.
The existing final comparison_rank continues ordering the built comparisons independently.

Neither stage introduces commission, payout or network preference. Execution ties retain
deterministic round-robin retrieval order; no network receives a relevance score. The
per-provider/persistence caps still bound what can be inspected: candidates outside those
caps cannot gain inclusion. PARTIAL diagnostics identify candidate/persistence truncation,
not a provider outage. Subscription output limits still apply after final ranking.

Same-GTIN listings across four networks can share one global Product while market-specific
StoreOffers remain distinct. BE comparisons/watches use BE prices; cheaper DE offers cannot
become their baseline or notification. Weak-title retrieval does not grant a canonical merge.

## Diagnostics

Run locally as an operator; these are not public HTTP or Telegram commands:

```sh
uv run python -m pricehunter.apps.admin coverage-report
uv run python -m pricehunter.apps.admin coverage-product "Sony WH-1000XM6" --country BE
uv run python -m pricehunter.apps.admin duplicate-merchants --limit 20
```

coverage-report groups programs, currently active/usable staging, materialized offers,
canonical products, current catalog eligibility, fresh offers, tracking-eligible and
history-eligible offers by market/network. Materialized counts include retained inactive
records; separate eligibility counts show current usable coverage. Provider flags identify
registered/enabled versus configured credentials. Search health stores last success,
last failure and consecutive failures in an expiring Redis hash per provider. Feed health
is aggregated from existing sync states; last_failure_at survives later successful syncs.
A Redis health outage is diagnostic and never invalidates successful search results.

coverage-product makes bounded provider retrieval and read-only catalog queries, reporting
merchant/candidate/materialized/fresh counts and current policy eligibility. Candidate
visibility alone is not a canonical match or persistence grant. The command does not
materialize candidates or expose customer data. Local feed providers only search staging.
Direct API providers may make their normal bounded retrieval request.

duplicate-merchants checks normalized domain/name across networks. Results are possible
duplicates only, limited to 100 pairs. Stores and StoreOffers remain separate; the same
merchant/listing acquired through two networks may consequently appear twice. There is
no automatic merge or assertion that the two contracts are interchangeable.

## Lifecycle, scheduling and migration

See [merchant commands](merchant-programs.md) for explicit pending/review/approve/activate,
policy/metadata/feed-reference changes and reversible disable. Expected integer versions
and consistent sync-state → program locking prevent a stale command replacing a review.
Audit rows append field names, reason and version steps; no credentials or feed payloads.
The review operation records approval deliberately, followed by separate activation.

Claims order by due time then program UUID with SKIP LOCKED, leases and bounded backoff.
No permanent job per merchant is needed. Twenty equal-due programs with failing merchants
still all receive a claim. Every acquisition page consumes the same network Redis budget,
not a separate full quota per merchant. Execution order is unrelated to comparison ranking.

New revision `9f62c7d40a11` preserves existing rights/IDs, initializes program version 1,
adds migrated audit entries, an append-only UPDATE/DELETE guard and last feed failure time.
Old migrations are unchanged. Downgrade requires export of audit history, then existing
feed/history safeguards still apply. Administrative fixture TRUNCATE is used only by the
isolated test/verifier cleanup, never by merchant lifecycle operations.

Apply `uv run alembic upgrade head` to the intended application database before starting
updated API/bot/worker services. Automated verification upgrades only disposable test
databases; it does not migrate or deploy your running application.

## Next milestone

M4D should begin with a small explicitly approved BE/DE merchant pilot: verify account
visibility, complete generations, actual quotas, currency/variant/stock fidelity, reviewed
cache/history/tracking rights and affiliate attribution. Only after that evidence should
explicit destination/postal shipping/tax inputs and complete delivered-cost comparison be
designed. M4C introduces no real activation, shipping/tax ranking, Amazon change or frontend.
