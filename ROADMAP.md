# PriceHunter roadmap

M0, M1, M2 and M3A are implemented and tested. Live Stars payment verification remains an explicit operator check; later milestones retain their individual integration gates.

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

## M3B — Comparison quality and operations

- Better structured identifiers/variant evidence from existing authorized providers.
- Operator match diagnostics and reviewed duplicate repair, preserving history/watch ownership.
- Explicit freshness/stale-offer policy and derived best-price history.
- Large-catalog query/load measurement, SQL summaries and bounded discovery refresh.
- Add further stores only after these comparison quality checks; Best Buy remains a future contract.

## M4 — International expansion

- More locales/marketplaces, explicit FX source/timestamps and shipping/tax context.
- Authorized Rakuten/CJ/Awin feeds, fashion variants, retention rollups.

## M5 — Platform

- Mini App, web/mobile clients, external billing and public API credentials.
- Affiliate attribution, referral conversion and B2B reports.

See README for tested release capabilities and explicit limitations.
