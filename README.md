# PriceHunter

International price tracking backend with a Telegram client. Python 3.12+, FastAPI,
aiogram 3, PostgreSQL, SQLAlchemy async, Redis and ARQ. This release implements the
**M0 foundation and M1 tracker**. Commercial billing and additional retail integrations
are explicit later milestones, not working features disguised by placeholders.

## What works

- Send a URL, inspect a product card, track it and set an absolute/percentage target.
- Paginated `/my`, pause/resume/delete, price history and changes since tracking began.
- Price-drop, target, restock and historical-low rules, with one prioritized alert per
  observation and a configurable cooldown.
- English/Russian/EU onboarding and messages; country, preferred currency and IANA timezone.
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
- Durable PostgreSQL state, migrations, Docker, CI, health checks and operator commands.

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
4. Optionally set a target such as `96` or `10%`.
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

## Real stores without keys

For electronics and clothing without retailer credentials, enable the reviewed shops
with `WOOCOMMERCE_STORES=["pine64_eu","raspberrypi_dk","hemptees_be","westernshop_be"]`. See the
[public stores guide](docs/public-stores.md) for tested links, variant selection,
native currencies and diagnostic commands. These integrations work independently
of eBay approval. Real Free-plan trackers refresh every 12 hours by default.

For clothing, send a [Hemptees product link](https://hemptees.be/product/short-sleeve-tee-max/)
and choose a size/color in the bot. Amazon remains disabled pending API access and a
separate tracking agreement; see [Amazon setup](docs/amazon-setup.md).

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
    DB --> Outbox[Notification outbox]
    Outbox --> Telegram[Telegram sender]
```

`src/pricehunter/apps` contains process entrypoints; `domain` is independent of
Telegram/database/HTTP libraries. `services` own transactions and business workflows.
`db/repositories` contains catalog persistence; focused service queries remain direct
SQLAlchemy rather than generic CRUD abstractions. REST DTOs live under `schemas`.

A canonical `Product` identifies a real product/variant. A `StoreOffer` identifies a
listing within a marketplace. Many `Tracker` rows share that offer. Schedulers claim
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
| `GET /api/v1/search?q=…&country=BE` | Grouped search; partial provider failures reported |
| `GET /api/v1/trackers?page=0&size=10` | Current user's trackers |
| `POST /api/v1/trackers` | `{ "store_offer_id": "…", "target_price": "90.00" }` |
| `PATCH /api/v1/trackers/{id}` | Target/rules/enabled; `target_price: null` clears target |
| `DELETE /api/v1/trackers/{id}` | Idempotent stop tracking |
| `GET /api/v1/subscriptions/me` | Current policy and checkout availability |
| `PATCH /api/v1/users/me/settings` | Language, country, currency, timezone |
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
| `FREE_*`, `PRO_*`, `POWER_*` | Central tracker quotas and refresh intervals |
| `USER_REQUESTS_PER_MINUTE`, `PROVIDER_*` | Shared limits, concurrency and total timeout |
| `MAX_RESPONSE_BYTES`, `REFRESH_LEASE_SECONDS`, `BATCH_SIZE` | Request/worker bounds |
| `NOTIFICATION_COOLDOWN_SECONDS`, `HISTORY_RETENTION_DAYS` | Alert suppression and optional retention |
| `AMAZON_*`, `BESTBUY_API_KEY`, `STRIPE_SECRET_KEY`, `WALLET_PAY_API_KEY` | Future integration inputs; currently unused |

For webhook mode, run the API with `TELEGRAM_MODE=webhook`, a random secret of at
least 32 characters and the bot token. Configure Telegram's `setWebhook` for
`https://YOUR_HOST/telegram/webhook`, passing that same `secret_token`. Use a trusted
TLS reverse proxy and explicit `ALLOWED_HOSTS`. Do not run polling simultaneously.
Webhook registration is an operator step; startup never silently changes production
Telegram webhook settings. Webhook mode is prepared for M1 updates; billing still
requires the M2 durable payment intake and verification workflow.

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
TEST_REDIS_URL=redis://localhost:6379/15 uv run pytest --cov=pricehunter
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
4. Register it in the composition root only when feature flag and credentials permit.
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
and authorized Rakuten/CJ/Awin feeds remain future contracts. Retailer-specific storage,
history, display and affiliate conditions must be implemented before enabling access.

## Payments and future integrations

`PaymentProvider`, disabled Stars/Stripe/Wallet Pay adapters, a verified-event DTO and
idempotent PostgreSQL ledger provide M2 extension points. **No checkout, renewal,
entitlement activation or refund flow is enabled in this iteration.** `/plans` states
this explicitly and `/paysupport` uses configurable support contact text.

Stars (`XTR`) is the only eligible provider for Telegram digital-service checkout.
The scaffold records a 30-day period; M2 must verify current rules, successful payments,
amounts/intent, renewals, expiration, refund handling and reconciliation before enabling
sales. External payment providers cannot bypass Stars eligibility.

Offer columns separate direct URLs, affiliate URLs/network/click IDs. Ranking does not
use commissions. Referral registration supports `?start=ref_CODE`, with unique user
codes and one-time referrer attribution; reward/conversion workflows remain later work.

## Important limitations

- Telegram cannot guarantee true exactly-once `sendMessage` delivery after an ambiguous
  network failure. Logical events are idempotent; a timeout or crashed in-flight send
  becomes `uncertain` and is not automatically resent. Monitor/reconcile these rows.
- Search and prices are native-currency values, excluding any guaranteed total shipping,
  tax or FX conversion. Availability may be unknown. No title-only catalog merges.
- An extreme drop requires a repeated observation. A currency change is quarantined
  indefinitely until reviewed; it never silently rewrites the historical currency.
- WooCommerce and eBay variant buttons show up to 100 concrete combinations, eight per page,
  and expire after 15 minutes. Clothing search returns up to five offers per shop.
  Amazon is implemented but disabled and unverified live; Best Buy/feeds remain contracts.
- M1 exposes target/history features to Free users; paid feature gating starts in M2.
- Shared offers use the fastest interested tracker's interval. Future commercial
  policies can additionally govern per-tracker alert cadence.
- No production load test, Amazon live access validation, production deployment,
  automated backups, billing lifecycle or public account signup is claimed here.
  
## 👤 Author

**Slava Serdiukov**
Machine Learning / Backend Engineering Portfolio Project
