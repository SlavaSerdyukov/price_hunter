# PriceHunter roadmap

M0, M1, M2, M3A, M3B, M4A and M4A.1 are implemented. Live Stars payment verification remains an explicit operator check; later milestones retain their individual integration gates.

## M0 — Foundation — implemented

- [x] Python 3.12, uv, typed modular monolith, settings and structured logging.
- [x] PostgreSQL, Redis, Alembic, non-root Dockerfile and local Compose.
- [x] Versioned authenticated API, liveness/readiness, CI workflow and deterministic tests.

## M1 — Telegram price tracker — implemented

- [x] Telegram-language onboarding with English/French/German/Spanish/Italian/Polish/Russian
  selection, URL cards, tracking, target FSM, pagination, pause/delete.
- [x] Shared offers, persistent history, safe mock provider and credential-gated eBay adapter.
- [x] Batch scheduling, fenced database leases, anomaly quarantine and notification outbox.
- [x] Restart/retry, ownership, concurrency and failure-path tests.

See [verification record](docs/verification.md) for executed checks and unverified live integrations.

## M2 — Monetization — implemented (automated lifecycle verified)

- [x] Authoritative subscription periods and central Free/Pro/Power entitlements.
- [x] Versioned configurable Stars prices, persistent checkout intents and recurring invoices.
- [x] Pre-checkout validation, durable successful-payment intake and concurrent deduplication.
- [x] Provider-authoritative renewal deadlines and expiration independent of cleanup timing.
- [x] Tracker/search/history limits and paid feature gates, preserving data on downgrade.
- [x] `/plans`, `/subscription`, support, safe upgrade and cancellation of future renewal.
- [x] Append-only purchase/refund ledger and idempotent operator refunds.
- [x] Paginated dry-run reconciliation, explicit matched-refund application and balance diagnostic.
- [x] PostgreSQL migration, simulated Telegram acceptance, CI-equivalent checks and operator docs.
- [ ] Manual live/test-environment Stars purchase, genuine recurring renewal, cancellation,
      refund and transaction-history confirmation before public commercial launch.

See [billing operations](docs/billing.md). Stripe and Wallet Pay remain disabled in Telegram.

## M3 — Multi-store

- [x] PINE64 EU / RaspberryPi.dk through public WooCommerce Store API, live lookup/search/refresh.
- [x] Hemptees / Western Shop Bruxelles and paginated size/color selection in Telegram.
- [x] Amazon Creators API adapter, OAuth 3.x, OffersV2 and fixture tests.
- [ ] Obtain Amazon API access and tracking agreement; adapt storage/display to that agreement and verify live.
- [x] eBay Belgium/EU markets, Belgian locales, native-price normalization and access diagnostic.
- [x] Verify Production OAuth and live Browse search/lookup/refresh for Belgium with operator-owned credentials.

## M3A — Comparison Core — implemented (acceptance verified)

- [x] Committed environment example, runtime docs and README link checks.
- [x] Shared canonical resolution, persisted identifiers, conservative normalized matching.
- [x] PostgreSQL concurrency protection; conflicts and variants remain separate.
- [x] Persisted comparison search/API, currency separation and availability-aware best/spread.
- [x] Seven-language comparison cards, offer pagination and exact-offer tracking.
- [x] Canonical ProductWatch, shared quotas/scheduling and idempotent outbox notifications.
- [x] Multi-store mock and existing eBay/WooCommerce/Amazon fixture acceptance.
- [x] Migration preservation/backfill, quality checks and coverage above the 85% CI floor.

See [comparison semantics](docs/comparison.md) and [verification](docs/verification.md).

## M3B — Autonomous discovery and comparison operations — implemented

- [x] Configurable provider freshness; stale/failed listings never win current-best ranking.
- [x] Product/provider/country/currency discovery shared by all eligible watches.
- [x] Capability-based queries, bounded leases, fencing, retries and provider suppression.
- [x] Single canonical resolver, missing optional GTIN metadata and explicit conflict checks.
- [x] Idempotent new-merchant alerts through the existing outbox.
- [x] Canonical transition history, bounded per-currency API and seven-language Telegram views.
- [x] Bounded asynchronous manual refresh, SQL summary/load checks at 100/500/1000 offers.
- [x] Operator diagnostics and read-only duplicate candidates.
- [x] Migration preservation, history retention and full regression checks.
- [ ] Reviewed product merge: deferred for audited redirects, conflicting watch settings and
      immutable outbox/history reconciliation. Never merge duplicate candidates automatically.

See [discovery operations](docs/discovery.md) and [verification](docs/verification.md).

## M4A — International commerce — implementation and fixtures

- [x] Durable watch market country, country picker, same-currency market coexistence.
- [x] Legacy country migration, nullable direct URLs, merchant IDs and delivery placeholders.
- [x] Reviewed provider permissions and bounded search-only catalog cache eviction.
- [x] Official eBay EPN context and returned affiliate URLs; no user-specific shared reference.
- [x] Gated Rakuten tokens/XML/pagination/merchant/price/UPC normalization; honest capabilities.
- [x] Central outbound policy, signed expiring redirects, minimal clicks and retention.
- [x] Shared persisted ECB reference rates, approximate displays, staleness and native ranking.
- [x] Seven locales, migration preservation and fixture acceptance.
- [ ] Live Rakuten account/partner/currency/link verification and EPN attribution/commission check.
- [ ] Operator review/configuration of each real provider's actual data-use agreement before activation.

See [international commerce](docs/international-commerce.md) and [verification](docs/verification.md).

## M4A.1 — Market correctness and snapshot ingestion

- [x] First-class offer market and merchant/external-ID/market uniqueness.
- [x] Market-scoped search, comparison, watch evaluation/scheduling, history and refresh.
- [x] Country-preserving Telegram callbacks, redirects and notification validation.
- [x] Legacy global history preserved separately; scoped baselines rebuilt without alerts.
- [x] Explicit snapshot capability, identity validation and serialized listing updates.
- [x] Repeat/stale/concurrent snapshots, current-only aggregates without history permission.
- [x] M3B → M4A → M4A.1 migration verifier; billing and affiliate/FX gates retained.

See [design and acceptance](docs/m4a1-market-correctness.md). No new networks are added.

## M4B — Delivery context and reviewed feed onboarding — proposed

- Explicit delivery country/postal input and documented shipping/tax/stock by destination.
- Missing-cost semantics and separately labeled delivered-cost comparison when complete.
- Authorized Awin/CJ feeds with identifier/fashion-variant provenance and contract retention.
- Preserve native currency groups and deterministic matching; no automatic product merge.

## M5 — Platform

- Mini App, web/mobile clients, external billing and public API credentials.
- Sale/conversion attribution, referral rewards and B2B reports.

See README for tested release capabilities and explicit limitations.
