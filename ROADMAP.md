# PriceHunter roadmap

M0, M1 and M2 are implemented and tested. Live Stars payment verification remains an explicit operator check; later milestones retain their individual integration gates.

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
- [ ] Expand comparison quality and shipping/destination context.
- Implement Best Buy contracts.
- Identifier matching review and product comparison UX.

## M4 — International expansion

- More locales/marketplaces, explicit FX source/timestamps and shipping/tax context.
- Authorized Rakuten/CJ/Awin feeds, fashion variants, retention rollups.

## M5 — Platform

- Mini App, web/mobile clients, external billing and public API credentials.
- Affiliate attribution, referral conversion and B2B reports.

See README for tested release capabilities and explicit limitations.
