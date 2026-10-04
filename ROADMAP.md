# PriceHunter roadmap

M0, M1, M2, M3A, M3B, M4A, M4A.1 and M4B are implemented. Live Stars payment verification remains an explicit operator check; later milestones retain their individual integration gates.

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

## M4B — Commerce feed engine, Awin and TradeDoubler — implemented

- [x] One StoreProvider catalog pipeline; bounded FeedSource acquisition and separate staging.
- [x] Stable merchant identities, market programs and central per-merchant policy resolution.
- [x] Leased/fenced generational sync, safe restart/replay, dry runs and bounded retention.
- [x] Indexed GTIN/MPN/brand-model/text search and shared autonomous discovery.
- [x] Existing snapshot semantics, variant separation and native market/currency comparison.
- [x] Credential-gated Awin CSV/gzip and TradeDoubler paginated adapters; fixture tests.
- [x] Operator CLI, 1k/100k streaming/batched replay and migration preservation checks.
- [ ] Real network credentials, advertiser approvals, rights review and live price/link verification.

See [feed operations](docs/commerce-feeds.md), [onboarding matrix](docs/merchant-programs.md)
and [verification](docs/verification.md). Named onboarding targets are not live integrations.

## M4C — Coverage engine, CJ and safe merchant onboarding — implemented

- [x] Official CJ schema audit; disabled-by-default bounded GraphQL feed adapter and fixtures.
- [x] Pending imports, deliberate review/approval/activation, version conflicts and audit trail.
- [x] Confirmed operator mutations, fail-closed candidates/templates and reversible disable.
- [x] Per-item search isolation, internal outcomes, deduplication and bounded global work.
- [x] Provider health, market/product coverage and possible duplicate-merchant diagnostics.
- [x] Four-network strong identity/native market prices, twenty-program and shared-rate tests.
- [x] New version/audit migration with preservation and guarded downgrade verification.
- [ ] Real account configuration, approvals, rights review and live feed/link verification.

See [coverage engine](docs/coverage-engine.md) and [CJ setup](docs/cj-setup.md).

## M4D — Canonical merchants and cross-network deduplication — implemented

- [x] Mandatory source → canonical Merchant identity and safe 1:1 backfill.
- [x] Explicit preview/dry-run/confirmed linking, version receipts and append-only audit.
- [x] SQL effective offers before summaries/pagination with policy/freshness/stock ranking.
- [x] Canonical counts, source-aware watch/history semantics and exact outbound attribution.
- [x] Reversible retailer disable, safe unlink and raw/canonical coverage diagnostics.
- [x] Migration preservation/guards and 100/500/1000 raw-row query-count acceptance.

See [merchant identity](docs/merchant-identity.md) and [verification evidence](docs/verification.md).

## M4E — Merchant pilot gate and feed publication safety — implemented

- [x] Append-only technical validation of inactive reviewed programs, independent of rights.
- [x] Fresh configuration-matching activation/reactivation evidence and concurrency fencing.
- [x] Shared streaming metrics, invalid-ratio/minimum/shrink publication gates and preserved current generation.
- [x] Confirmed one-shot shrink override with atomic immutable audit and program-local diagnostics.
- [x] Pending watch cancellation after canonical Merchant reassignment; rename/legacy compatibility.
- [x] 100k bounded-memory acceptance and preservation/downgrade migration checks.
- [ ] Operator-approved first live merchant pilot and independent real feed/link verification.

See [pilot steps](docs/merchant-pilot.md), [quality gates](docs/feed-quality.md) and
[M4E evidence](docs/m4e-design.md).

## M5A — Public beta runtime hardening — implemented

- [x] Recoverable technical validation lease, token fencing and network outside PostgreSQL transactions.
- [x] Exact packaged Alembic head, shared API/bot/worker startup preflight and bounded local readiness.
- [x] API/Telegram shared user budget, financial bypass and safe Redis outage responses.
- [x] Scoped HTTP/update/job correlation, private cache/security headers and safe infrastructure errors.
- [x] Read-only operational backlog/lease diagnostics and stale notification recovery without resend.
- [x] Explicit pool/query limits, operation-specific worker timeouts and owned-resource shutdown.
- [x] Production lifespan and disposable stale-schema CI acceptances; operator release/recovery docs.
- [ ] Public beta hosting, restore rehearsal, load test and real merchant/Stars acceptance.

See [M5A design](docs/m5a-design.md), [runtime contracts](docs/runtime-hardening.md)
and [beta operations](docs/beta-operations.md).

## Approved merchant pilot and delivery context — proposed

- Onboard a small approved BE/DE merchant set; verify real feed completeness, quotas,
  identifiers, variants, update cadence, permissions and affiliate attribution.
- Explicit delivery country/postal input and documented shipping/tax/stock by destination.
- Missing-cost semantics and separately labeled delivered-cost comparison when complete.
- Preserve native currency groups and deterministic matching; no automatic product merge.

## M5 — Platform

- Mini App, web/mobile clients, external billing and public API credentials.
- Sale/conversion attribution, referral rewards and B2B reports.

See README for tested release capabilities and explicit limitations.
