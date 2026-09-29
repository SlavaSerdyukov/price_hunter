# PriceHunter

International price tracking backend with a Telegram client. Python 3.12+, FastAPI,
aiogram 3, PostgreSQL, SQLAlchemy async, Redis and ARQ. This release implements the
**M0–M4B: tracking, Stars subscriptions, discovery, international commerce and merchant feeds**. Billing is tested with
a simulated Telegram transport; real Stars purchases/renewals/refunds remain manual checks.

## What works

- Send a URL, inspect a product card and track it; paid plans add absolute/percentage targets.
- Paginated `/my`, pause/resume/delete, price history and changes since tracking began.
- Price-drop, target, restock and historical-low rules, with one prioritized alert per
  observation and a configurable cooldown.
- Onboarding in the user's Telegram language; English, French, German, Spanish, Italian,
  Polish and Russian messages; country, preferred currency and IANA timezone.
- Deterministic mock catalog and an eBay Browse adapter with fixture-based tests.
- eBay Belgium/EU marketplaces and a read-only production access diagnostic; live
  Production OAuth and Belgian search/lookup/refresh verified on 2026-09-19.
- Keyless PINE64 EU / RaspberryPi.dk / Hemptees / Western Shop Bruxelles via public
  WooCommerce Store API: search, tracking and stock status. `/stores` lists enabled shops.
- Size/color selection buttons for WooCommerce product links, with exact variant tracking.
- eBay multi-variation listing links prompt size/configuration selection; refreshes keep
  the exact selected variation, including Belgian and German listings.
- Amazon Creators API adapter with fixture tests, disabled pending access and tracking approval.
- Versioned authenticated REST endpoints using the same services as the bot.
- Shared offer refreshes, persisted scheduling, retries, anomaly quarantine and outbox.
- Market-scoped offers, comparisons, watches and best-price history; global canonical products.
- Reviewed provider data policies, official eBay EPN support and authorized search snapshots.
- Credential/policy-gated Rakuten Product Search, signed outbound links and minimal click records.
- Awin CSV/gzip and TradeDoubler feed adapters, disabled until credentials/programs are configured.
- Merchant-specific permissions, bounded generational sync and indexed local feed search.
- Optional timestamped ECB reference conversion; native-currency rankings stay authoritative.
- Durable PostgreSQL state, migrations, Docker, CI, health checks and operator commands.
- Free/Pro/Power entitlements; recurring Stars checkout, expiry, upgrades, cancellation,
  refunds, durable payment intake and operator reconciliation. `/plans` and `/subscription`.

## Compare a product across stores

Choose Germany in `/settings`, then send `/search Sony WH-1000XM6` with the mock provider
enabled. One comparison card shows matching stores, the best fresh confirmed in-stock
offer and spread **per native currency within the selected market**.
Stale and failed prices remain visible with their age, but cannot be current best.
**All offers** opens paginated exact-offer tracking; **Track best** creates a canonical
product watch that can follow a different merchant when it becomes cheapest.
Manage watches in `/watches`; existing exact trackers remain in `/my`. Watches periodically
search configured providers for new matching offers. **History** shows best-price transitions;
**Refresh prices** schedules a bounded background update. All seven languages are supported.
See [discovery and freshness operations](docs/discovery.md) for cadences, settings and diagnostics.

Watches and trackers share the plan quota. Search/comparison/history/refresh require an
explicit or saved country. Profile changes never move an existing watch, its history,
refresh action or notification links. Unknown shipping/tax stay unknown. Optional
[ECB reference conversion](docs/fx.md) is approximate and never changes native ranking.
See [M4A configuration and upgrade steps](docs/international-commerce.md).
See [M4A.1 market and snapshot semantics](docs/m4a1-market-correctness.md) for migration
of legacy global history and repeat-search behavior.
For an existing database, stop application services, back up, migrate and run
`uv run python -m pricehunter.apps.admin catalog-backfill` until zero before restarting.

## Quick start

Install Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/) and
Docker with Compose. From this directory:

```bash
cp .env.example .env
docker compose up -d --wait
uv sync --frozen
uv run alembic upgrade head
```

Set `TELEGRAM_BOT_TOKEN` in `.env` to the token from BotFather. Retailer credentials
are unnecessary for the mock catalog. A real Telegram connection requires a bot token;
all automated tests use a local Telegram test double.

Start these in separate terminals:

```bash
uv run python -m pricehunter.apps.bot
uv run python -m pricehunter.apps.api
uv run arq pricehunter.jobs.worker.WorkerSettings
```

The API serves `http://localhost:8000`; development documentation is at `/docs`.
`make dev`, `make bot`, `make worker`, `make migrate`, `make lint` and `make test`
provide shortcuts. `make dev` starts dependencies, migrates and runs the API with reload.

For Dockerized Python services, migrate before starting them:

```bash
docker compose --profile app build
docker compose --profile app run --rm api alembic upgrade head
docker compose --profile app up -d
```

Compose is a **local development configuration**, with example database credentials and
loopback-only published ports. The image runs as UID 10001 and never copies `.env`.
Migrations run explicitly, not concurrently in every application replica.

If another project uses port 5432 or 6379, set `POSTGRES_PORT` / `REDIS_PORT` in
`.env`, and update the port in your local `DATABASE_URL` / `REDIS_URL` as well.
`API_PORT` controls the published API port. Container-to-container connections keep
using the internal service ports and do not need changes.

## Try the complete tracking flow

1. Open the bot and send `/start`.
2. Send `https://mock.pricehunter.test/products/headphones`.
3. The card shows **EUR 100**. Tap **Track price**.
4. On Pro/Power, optionally set a target such as `96` or `10%`; Free uses basic drop alerts.
5. With the worker running, the first scheduler pass occurs within 30 seconds. The
   mock sequence is **100 → 95 → 90 → 85**, then stays at 85. Subsequent mock checks
   use `MOCK_CHECK_INTERVAL_SECONDS` (default 60). No mock URL is fetched over HTTP.
6. The outbox delivers an alert. `/my` shows the new price and target; use its buttons
   for history, pause, resume or removal. Restarting processes preserves all state.

The default one-hour cooldown suppresses repetitive ordinary-drop alerts. A first
target/restock crossing can still notify during that cooldown; repeated crossings of
the same type cannot. Set `NOTIFICATION_COOLDOWN_SECONDS=0` **for a local demo only**
to see every mock price step. Settings are read when each process starts.

Other mock products: `coffee-machine`, `sneakers-42`, `sneakers-44`, `camera` (USD),
`keyboard` (GBP). `/search Headphones` also finds the mock item. Re-resolving a known
item reuses its stored price; it does not reset the mock sequence. The mock sequence
is deliberately finite, not an endlessly oscillating source of demo alerts.

## Languages

New users start in their **Telegram app language** when supported; otherwise, English
is used. Regional codes such as `fr-BE` use the matching base language. Tap
**🌐 Language** in the main menu or open `/settings` to choose **English, Français,
Deutsch, Español, Italiano, Polski or Русский**. The selected language is saved in
PostgreSQL and applies to menus, errors, notifications, subscriptions and payment messages.
Existing language preferences survive updates and restarts; `/start` does not reset them.
The Telegram language initializes a new account only; later Telegram language changes
do not overwrite the saved choice. Use `/settings` to change an existing account's language.

English is also the fallback for unsupported locales or missing translations. Prices
use the selected language's number format and retain the retailer's original currency.
The API accepts `en`, `fr`, `de`, `es`, `it`, `pl` and `ru` in
`PATCH /api/v1/users/me/settings` (`language_code`).

## Real stores without keys

For electronics and clothing without retailer credentials, enable the reviewed shops
with a reviewed `PROVIDER_DATA_POLICIES` entry per shop and `WOOCOMMERCE_STORES=["pine64_eu","raspberrypi_dk","hemptees_be","westernshop_be"]`. See the
[public stores guide](docs/public-stores.md) for tested links, variant selection,
native currencies and diagnostic commands. These integrations work independently
of eBay approval. Real Free-plan trackers refresh every 12 hours by default.

For clothing, send a [Hemptees product link](https://hemptees.be/product/short-sleeve-tee-max/)
and choose a size/color in the bot. Amazon remains disabled pending API access and a
separate tracking agreement; see [Amazon setup](docs/amazon-setup.md).

## Merchant feeds: Awin and TradeDoubler

[The feed engine](docs/commerce-feeds.md) imports approved merchant catalogs into separate
PostgreSQL staging. Search and shared watch discovery select relevant items for the
existing canonical catalog. The customer sees the merchant name, not the network name.
Each merchant/market has its own reviewed catalog, tracking, history and affiliate rights.

Both networks default disabled and are fixture-tested; no Awin/TradeDoubler merchant is
claimed live. Start with [merchant onboarding](docs/merchant-programs.md), then configure
[Awin](docs/awin-setup.md) or [TradeDoubler](docs/tradedoubler-setup.md), approved program
UUIDs in `FEED_PROGRAM_IDS`, and run `feed-sync PROGRAM_UUID --dry-run`. Secrets stay in
local environment configuration. Existing direct providers require no MerchantProgram.

## Architecture

```mermaid
flowchart TD
    Bot[Telegram / aiogram] --> Services[Application services]
    API[FastAPI / v1] --> Services
    Worker[ARQ workers] --> Services
    Services --> Domain[Money, identity, tracking rules, plan policies]
    Services --> Providers[Provider registry / normalized offer DTOs]
    Services --> DB[(PostgreSQL)]
    Services --> Redis[(Redis: rate limits, FSM, queue)]
    Providers --> Retailers[Official retailer APIs]
    FeedSources[FeedSource / approved network feeds] --> Staging[(MerchantFeedItem staging)]
    Staging --> Providers
    DB --> Outbox[Notification outbox]
    Outbox --> Telegram[Telegram sender]
```

`src/pricehunter/apps` contains process entrypoints; `domain` is independent of
Telegram/database/HTTP libraries. `services` own transactions and business workflows.
`db/repositories` contains catalog persistence; focused service queries remain direct
SQLAlchemy rather than generic CRUD abstractions. REST DTOs live under `schemas`.

A canonical `Product` identifies a real product/variant. A `StoreOffer` identifies a
listing within a marketplace. Many `Tracker` rows share that offer; `ProductWatch`
tracks a Product with a durable market country and native comparison currency. Schedulers claim
due offers with `SKIP LOCKED`; short database leases and fencing tokens recover stale
workers and lost enqueue operations. A Redis lease prevents concurrent duplicate
fetches of the same claim. External I/O happens outside database transactions.

An accepted observation, updated aggregate statistics and resulting notification
events commit together. Every refresh and notification has a unique identity.
See [architecture decisions](docs/architecture.md) and [roadmap](ROADMAP.md).
Executed checks and remaining live-integration checks are listed in the
[verification record](docs/verification.md).

## API authentication and use without Telegram

Create an API-only account locally:

```bash
uv run python -m pricehunter.apps.admin create-api-user --label local
```

It prints `user_id`, `key_id` and a random `api_key` **once**. Store the key privately;
only its SHA-256 digest is persisted. Put it in the `Authorization: Bearer …` header
(or use **Authorize** in `/docs`). Do not put it in a URL.

To use an existing Telegram account after `/start`:

```bash
uv run python -m pricehunter.apps.admin issue-api-key --telegram-user-id YOUR_ID
uv run python -m pricehunter.apps.admin revoke-api-key KEY_UUID
```

| Endpoint | Purpose |
| --- | --- |
| `POST /api/v1/products/resolve` | Resolve `{ "url": "https://…" }` to an offer |
| `GET /api/v1/products/{product_id}` | Canonical product and native-currency offers |
| `GET /api/v1/products/{product_id}/history?offer_id=…` | Offer history; explicit offer prevents mixing stores/currencies |
| `GET /api/v1/search?q=…&country=BE` | Persisted comparison `products`; partial provider failures reported |
| `GET /api/v1/products/{product_id}/offers?page=0&size=10` | Offer pages with full per-currency summaries |
| `GET /api/v1/product-watches?page=0&size=10` | Current user's best-price watches |
| `GET /api/v1/products/{id}/best-price-history?currency=EUR` | Bounded canonical best-price transitions |
| `POST /api/v1/products/{id}/refresh` | Schedule a bounded stale-price refresh; HTTP 202 |
| `POST /api/v1/product-watches` | `{ "product_id": "…", "currency": "EUR", "target_price": "310.00" }`; target optional |
| `PATCH /api/v1/product-watches/{id}` | Target, notification flags or enabled state |
| `DELETE /api/v1/product-watches/{id}` | Idempotent stop watching |
| `GET /api/v1/trackers?page=0&size=10` | Current user's trackers |
| `POST /api/v1/trackers` | `{ "store_offer_id": "…", "target_price": "90.00" }` |
| `PATCH /api/v1/trackers/{id}` | Target/rules/enabled; `target_price: null` clears target |
| `DELETE /api/v1/trackers/{id}` | Idempotent stop tracking |
| `GET /api/v1/subscriptions/me` | Effective plan, expiry, renewal state, quotas, counts and features |
| `PATCH /api/v1/users/me/settings` | Language, country, currency, timezone |
| `GET /r/{token}` | Optional signed HTTPS store redirect; no arbitrary destinations |
| `GET /health/live`, `GET /health/ready` | Process and PostgreSQL/migrations/Redis readiness |

Amounts in JSON are decimal strings, never binary floats. Dates are UTC. Pagination
and history limits are bounded. Private tracker operations always check ownership.
API-only accounts have no Telegram delivery destination; their alert rows are cancelled
by the Telegram sender until a future delivery channel is implemented.

## Configuration

All settings are documented in `.env.example` and validated by Pydantic Settings.
No external API credentials are required to boot the API/worker. Without a Telegram
token the worker refreshes prices but leaves pending Telegram deliveries untouched.

| Setting group | Behavior |
| --- | --- |
| `DATABASE_URL`, `REDIS_URL` | Async PostgreSQL DSN and Redis connection |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_MODE` | Polling client or API webhook mode |
| `TELEGRAM_WEBHOOK_SECRET`, `ALLOWED_HOSTS` | Webhook authentication and host allowlist |
| `MOCK_PROVIDER_ENABLED`, `MOCK_CHECK_INTERVAL_SECONDS` | Development catalog; forbidden in production |
| `EBAY_ENABLED`, `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `EBAY_MARKETPLACES` | Credential-gated Browse integration |
| `EBAY_BELGIUM_LOCALE` | Belgian search locale: `nl-BE` or `fr-BE` |
| `WOOCOMMERCE_STORES`, `PROVIDER_RATE_LIMITS` | Reviewed public catalogs and per-provider operation limits |
| `FREE_*`, `PRO_*`, `POWER_*`, `PLAN_FEATURES` | Tracker quotas, intervals, search/history limits and feature gates |
| `STARS_BILLING_ENABLED`, `BILLING_PRICE_VERSION`, `CHECKOUT_TTL_SECONDS` | Versioned Stars checkout; disabled by default |
| `PRO_PRICE_STARS`, `POWER_PRICE_STARS`, `SUPPORT_CONTACT` | Prices per 30 days and payment/refund support |
| `USER_REQUESTS_PER_MINUTE`, `PROVIDER_*` | Shared limits, concurrency and total timeout |
| `MAX_RESPONSE_BYTES`, `REFRESH_LEASE_SECONDS`, `BATCH_SIZE` | Request/worker bounds |
| `NOTIFICATION_COOLDOWN_SECONDS`, `HISTORY_RETENTION_DAYS` | Alert suppression and optional retention |
| `PROVIDER_DATA_POLICIES` | Required reviewed permissions for each real provider |
| `RAKUTEN_*`, `EBAY_EPN_CAMPAIGN_ID` | Gated Product Search and official EPN context |
| `PUBLIC_BASE_URL`, `REDIRECT_*`, `OUTBOUND_CLICK_RETENTION_DAYS` | Optional signed redirects and click retention |
| `FX_ENABLED`, `FX_REFRESH_SECONDS`, `FX_MAX_AGE_DAYS` | Shared ECB reference snapshots and display age limit |
| `AMAZON_*` | Existing Creators adapter, pending access and tracking approval |
| `BESTBUY_API_KEY`, `STRIPE_SECRET_KEY`, `WALLET_PAY_API_KEY` | Deferred integration inputs |

For webhook mode, run the API with `TELEGRAM_MODE=webhook`, a random secret of at
least 32 characters and the bot token. Configure Telegram's `setWebhook` for
`https://YOUR_HOST/telegram/webhook`, passing that same `secret_token`. Use a trusted
TLS reverse proxy and explicit `ALLOWED_HOSTS`. Do not run polling simultaneously.
Webhook registration is an operator step; startup never silently changes production
Telegram webhook settings. Financial updates use durable PostgreSQL intake in both webhook and polling modes;
webhook persistence failures return an error for Telegram to retry.

## Migrations and retention

```bash
uv run alembic upgrade head
uv run alembic check
uv run alembic revision --autogenerate -m "describe the change"
```

Review generated migrations before applying them. The schema has UUID identities,
unique marketplace/listing pairs, tracker ownership constraints, refresh/event keys,
and deliberate due-offer, offer/history and outbox indexes.

History is append-only by default. At 200,000 offers checked twice daily this means
about 400,000 rows/day: monitor growth before choosing retention. Lifetime aggregate
min/max/sum/count and tracker baseline remain available even after pruning. Charts
receive bounded retained observations and the configured retention window.

```bash
# Set HISTORY_RETENTION_DAYS to a positive value first.
uv run python -m pricehunter.apps.admin prune-history
uv run python -m pricehunter.apps.admin outbox
```

Pruning removes at most 5,000 rows per invocation. It never removes payment records.
Volumes persist restarts; `docker compose down -v` deletes local data. Production
needs tested PostgreSQL backups and restore procedures, not only persistent volumes.

## Tests and quality checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest tests/unit
```

Integration tests require a **dedicated disposable database whose name ends in
`_test`**. They truncate application tables between tests. With Compose running:

```bash
docker compose exec postgres createdb -U pricehunter pricehunter_test
export TEST_DATABASE_URL=postgresql+asyncpg://pricehunter:pricehunter@localhost:5432/pricehunter_test
DATABASE_URL="$TEST_DATABASE_URL" uv run alembic upgrade head
TEST_REDIS_URL=redis://localhost:6379/15 uv run pytest --cov=pricehunter --cov-report=term-missing --cov-fail-under=85
```

The optional `TEST_REDIS_URL` must use disposable Redis database **15**; the ARQ test
clears it. Without it that test uses fakeredis and stubs only its unsupported startup
INFO log. Without `TEST_DATABASE_URL`, integration tests are explicitly skipped.
CI provisions real PostgreSQL and Redis, runs both migrations and all checks.

Tests include stored eBay fixtures, URL/DNS rejection, variants, anomalies, plan quota
races, concurrent resolution, shared refreshes, stale leases, notification retry and
ambiguous-delivery handling, payment-ledger idempotency, REST ownership, and the entire
aiogram acceptance flow with simulated Telegram transport. Retailer/Telegram network
calls and paid API credentials are never needed by CI.

## Adding a store provider

1. Implement `StoreProvider` in `providers/`: exact URL domains, resolve, search and
   refresh via `OfferReference`; return validated `ProductOfferData` only.
2. Use an official API/feed where available. Enable HTML/browser access only after
   permission and policy review; there is no scraping/bypass engine in this release.
3. Parse listing IDs locally, call fixed API hosts through `ProviderHTTP`, and use the
   public-IP transport. No automatic redirects, environment proxies or arbitrary hrefs.
4. Record its reviewed ProviderDataPolicy; register only when permissions, feature flag
   and credentials permit. Capabilities must describe documented operations.
5. Add stored HTTP fixtures plus malformed/timeout/identity/marketplace tests. Choose
   provider-specific quota values within the retailer's approved access limits.

eBay supports keyword/GTIN search, legacy listing lookup, variation IDs, normalized
prices/images/availability and country selection. Belgium and EU setup, key registration,
access approval and activation commands are documented in the
[eBay connection guide (Russian)](docs/ebay-setup.md). Run `make check-ebay` to check
production OAuth, search, lookup and refresh without writing data or sending messages.
Missing keys leave the provider disabled. Production OAuth, Belgian search, lookup and
refresh were verified separately from fixture tests on 2026-09-19. Current official references:
[Browse overview](https://developer.ebay.com/api-docs/buy/api-browse.html),
[OAuth credentials](https://developer.ebay.com/api-docs/static/oauth-credentials.html),
[marketplaces](https://developer.ebay.com/api-docs/buy/ref-marketplace-supported.html).

The [Amazon Creators API adapter](docs/amazon-setup.md) supports OAuth LwA 3.x,
GetItems/SearchItems and OffersV2. It is disabled until both API access and separate
Amazon approval for tracking are obtained. Live access remains unverified. Best Buy
and full CJ/Awin feeds remain future contracts. [Rakuten Product Search](docs/rakuten-setup.md)
is implemented and fixture-tested, gated by credentials, partner MIDs and reviewed permissions.
All real providers require explicit storage/history/tracking/affiliate policy configuration.

## Telegram Stars subscriptions

| Plan | Stars / 30 days | Trackers + watches | Real-store checks | Search / day | History |
| --- | --- | --- | --- | --- | --- |
| Free | 0 | 2 | 12 h | 3 | 7 days |
| Pro | 250 | 50 | 2 h | 30 | 90 days |
| Power | 750 | 250 | 1 h | 100 | 365 days |

All plans include ordinary price-drop alerts. Pro/Power add targets, historical lows
and restock alerts. Limits and capabilities are configurable. On expiry, data remains
stored; only the oldest enabled trackers and watches within the shared current quota are scheduled.

`/plans` generates recurring XTR invoice links; `/subscription` shows access and lets
users cancel renewal. Upgrading requires cancelling the old renewal first, then buying
the new plan at full price without proration. Paid access is retained until its expiry.
`/paysupport` uses `SUPPORT_CONTACT` (configured contact: @slavasham).

Enable `STARS_BILLING_ENABLED` after migration to accept checkout. Payment events and
periods commit atomically, duplicate charges never extend twice, and expiry is checked
on every authorization. Refunds append ledger events. Stripe and Wallet Pay remain disabled.

Read [billing operations](docs/billing.md) for configuration, lifecycle, price versions,
refund/reconciliation CLI commands, safe migration order and manual Telegram checks.
Automated payment tests spend no Stars; no real payment verification is claimed.

## Future integrations

Offers separate nullable direct URLs from affiliate URLs/network metadata.
[Outbound links and optional signed redirects](docs/affiliate-links.md) centralize selection
and record clicks without Telegram/IP/User-Agent data. Ranking does not use commissions. Referral registration supports `?start=ref_CODE`, with unique user
codes and one-time referrer attribution; reward/conversion workflows remain later work.

## Important limitations

- Telegram cannot guarantee true exactly-once `sendMessage` delivery after an ambiguous
  network failure. Logical events are idempotent; a timeout or crashed in-flight send
  becomes `uncertain` and is not automatically resent. Monitor/reconcile these rows.
- Search and prices are native-currency values, excluding any guaranteed total shipping,
  tax or checkout FX. Optional ECB amounts are reference displays only. Availability may
  be unknown. No title-only catalog merges.
- An extreme drop requires a repeated observation. A currency change is quarantined
  indefinitely until reviewed; it never silently rewrites the historical currency.
- WooCommerce and eBay variant buttons show up to 100 concrete combinations, eight per page,
  and expire after 15 minutes. Clothing search returns up to five offers per shop.
  Amazon is implemented but disabled and unverified live; Best Buy and full Awin/CJ feeds
  remain deferred. Rakuten/EPN monetization still needs live account validation.
- Free has limited search/history and basic drop alerts. Existing paid-only preferences
  remain stored after downgrade and resume when the user has the required entitlement.
- Shared offers use the fastest eligible tracker/watch interval. Future commercial
  policies can additionally govern per-tracker alert cadence.
- No production load test, Amazon live access validation, production deployment,
  automated backups, live Stars lifecycle verification or public account signup is claimed here.
  
## 👤 Author

**Slava Serdiukov**
Machine Learning / Backend Engineering Portfolio Project
