# PriceHunter roadmap

The first release is M0 + M1. Later milestones are extension contracts, not advertised features.

## M0 — Foundation — implemented

- [x] Python 3.12, uv, typed modular monolith, settings and structured logging.
- [x] PostgreSQL, Redis, Alembic, non-root Dockerfile and local Compose.
- [x] Versioned authenticated API, liveness/readiness, CI workflow and deterministic tests.

## M1 — Telegram price tracker — implemented

- [x] English/Russian onboarding, URL cards, tracking, target FSM, pagination, pause/delete.
- [x] Shared offers, persistent history, safe mock provider and credential-gated eBay adapter.
- [x] Batch scheduling, fenced database leases, anomaly quarantine and notification outbox.
- [x] Restart/retry, ownership, concurrency and failure-path tests.

See [verification record](docs/verification.md) for executed checks and unverified live integrations.

## M2 — Monetization

- Activate Free/Pro/Power commercial entitlements and configurable prices.
- Telegram Stars checkout, verified successful payments, 30-day renewal lifecycle.
- Expiration, reconciliation, refunds, support and billing operational tests.

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
