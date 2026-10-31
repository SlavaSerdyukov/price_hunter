# Local development and interface reference

This guide preserves detailed setup and operational references for the completed
portfolio snapshot. It is not a public service deployment plan. Use the mock provider;
the default environment example disables real providers and Stars checkout.

## Environment and processes

Install Python 3.12+, uv and Docker Compose. In a fresh checkout:

```bash
cp .env.example .env
docker compose up -d --wait postgres redis
uv sync --frozen
uv run alembic upgrade head
```

Keep an existing private `.env` rather than overwriting it. The virtual environment is
`.venv`, so optional activation is `source .venv/bin/activate`; `uv run` needs no activation.
Settings load at process start.

Run these in separate terminals:

```bash
uv run python -m pricehunter.apps.api
uv run arq pricehunter.jobs.worker.WorkerSettings
# Optional, after supplying your own TELEGRAM_BOT_TOKEN locally:
uv run python -m pricehunter.apps.bot
```

The API is at `http://localhost:8000`; development OpenAPI UI is at `/docs`.
`make dev` runs dependencies, migration and a reload-enabled API; `make bot`,
`make worker`, `make migrate`, `make lint` and `make test` are shortcuts.
No retailer/payment credentials are required for API/worker mock use. Without a bot
token the worker refreshes prices but leaves pending Telegram deliveries untouched.

Dockerized Python processes are also available:

```bash
docker compose --profile app build
docker compose --profile app run --rm api alembic upgrade head
docker compose --profile app up -d
```

Compose is local-only configuration with synthetic database credentials and loopback
published ports. The Python image runs as UID 10001 and excludes `.env`. Migrations run
explicitly rather than concurrently in replicas. If ports are occupied, change
`POSTGRES_PORT`/`REDIS_PORT` and corresponding local DSNs; `API_PORT` controls HTTP.
Container-to-container service ports remain unchanged. Persistent volumes are not backups;
`docker compose down -v` removes local data.

## Mock tracking and comparison

In Telegram, `/start` then send `https://mock.pricehunter.test/products/headphones` and
choose **Track price**. The initial EUR 100 advances to 95, 90 and 85, then stabilizes.
The first scheduler pass occurs within 30 seconds; subsequent mock checks default to
60 seconds. Mock URLs are never fetched over HTTP. Resolving a known listing reuses its
stored state rather than restarting the finite price sequence.

The default one-hour notification cooldown suppresses repeated ordinary drops.
`NOTIFICATION_COOLDOWN_SECONDS=0` can expose each step in a local demo. Targets, restock
and historical-low rules remain plan-gated and retain their normal cooldown semantics.
Other mock products are `coffee-machine`, `sneakers-42`, `sneakers-44`, `camera` (USD)
and `keyboard` (GBP).

For multi-merchant comparison, set market Germany in `/settings`, then
`/search Sony WH-1000XM6`. **All offers** opens exact-offer tracking; **Track best**
creates a canonical ProductWatch. Exact trackers stay in `/my`; watches in `/watches`.
Profile changes never rewrite saved watch markets or baselines. Unknown costs stay unknown.

Seven Telegram languages are implemented: English, French, German, Spanish, Italian,
Polish and Russian. A new user takes the supported Telegram language, otherwise English;
the saved language survives later Telegram language changes and `/start`. REST settings
accept `en`, `fr`, `de`, `es`, `it`, `pl` and `ru`.

## API authentication and representative flow

```bash
uv run python -m pricehunter.apps.admin create-api-user --label local
```

The command intentionally prints a new `api_key` once; only its SHA-256 digest is stored.
Use `Authorization: Bearer …` or **Authorize** in `/docs`, never a URL parameter.
API-only users have no Telegram alert destination. With a bot token configured, the
Telegram sender cancels their unsupported alert deliveries.

For an existing Telegram account after `/start`:

```bash
uv run python -m pricehunter.apps.admin issue-api-key --telegram-user-id YOUR_ID
uv run python -m pricehunter.apps.admin revoke-api-key KEY_UUID
```

In the API UI, set `{"country_code":"DE"}` with `PATCH /api/v1/users/me/settings`.
Search `Sony WH-1000XM6` in DE for a canonical multi-store comparison. Resolve the mock
headphones URL with `POST /api/v1/products/resolve`, then create an exact tracker using
the returned offer `id` as `store_offer_id`.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/v1/products/resolve` | Resolve a supported URL to a source offer. |
| `GET /api/v1/search?q=…&country=BE` | Canonical comparison products and partial provider failures. |
| `GET /api/v1/products/{id}?country=BE` | Canonical item and native-currency groups. |
| `GET /api/v1/products/{id}/offers?page=0&size=10&country=BE` | Bounded offer pages with full eligible summaries. |
| `GET /api/v1/products/{id}/history?offer_id=…` | Explicit listing history, without mixing stores/currencies. |
| `GET /api/v1/products/{id}/best-price-history?currency=EUR&country=BE` | Canonical best-price transitions. |
| `POST /api/v1/products/{id}/refresh?country=BE` | Schedule bounded stale-price refresh; HTTP 202. |
| `GET/POST /api/v1/trackers` | List/create exact-listing subscriptions. |
| `PATCH/DELETE /api/v1/trackers/{id}` | Rules, target, enabled state or idempotent removal. |
| `GET/POST /api/v1/product-watches` | List/create market/currency-specific canonical watches. |
| `PATCH/DELETE /api/v1/product-watches/{id}` | Watch rules, target, enabled state or removal. |
| `POST /api/v1/products/{id}/delivery-comparison?country=BE` | Read persisted/static destination evidence; no remote quote call. |
| `POST /api/v1/products/{id}/delivery-quote?country=BE` | Explicit bounded quote request; synthetic mock only. |
| `GET /api/v1/subscriptions/me` | Effective rights, quotas, counts and renewal state. |
| `GET/PATCH /api/v1/users/me/settings` | Language, market, reference currency, timezone and private delivery preferences. |
| `GET /r/{token}` | Optional signed source-specific outbound redirect. |
| `GET /health/live`, `GET /health/ready` | Process and local database/schema/Redis readiness. |

Amounts are decimal strings, timestamps UTC; private operations check ownership. Search,
comparison/history/refresh need an explicit or saved country. Postal destination input
belongs in the body, not URLs. See [delivery contract](delivery-context.md).

## Configuration and operator references

[.env.example](../.env.example) is the complete setting reference.

| Group | Meaning |
| --- | --- |
| `DATABASE_*`, `REDIS_URL` | Pool/query bounds and ephemeral coordination. |
| `TELEGRAM_*`, `ALLOWED_HOSTS` | Polling/webhook client, authentication and trusted hosts. |
| `MOCK_*` | Deterministic local catalog; forbidden in production mode. |
| `PROVIDER_DATA_POLICIES` and provider flags/keys | Explicit reviewed source capabilities/rights; no automatic commercial permission. |
| `FREE_*`, `PRO_*`, `POWER_*`, `PLAN_FEATURES` | Shared tracker/watch quotas, cadences, history/search limits and paid rules. |
| `STARS_BILLING_ENABLED`, `BILLING_*`, `CHECKOUT_*` | Disabled-by-default, versioned simulated billing boundary. |
| `USER_REQUESTS_PER_MINUTE`, `PROVIDER_*` | Shared rate/concurrency/time budgets. |
| `DELIVERY_*` | Global quote bounds, evidence TTL and operation deadlines. |
| `NOTIFICATION_*`, `HISTORY_RETENTION_DAYS` | Cooldown and opt-in observation retention. |
| `PUBLIC_BASE_URL`, `REDIRECT_*`, `OUTBOUND_CLICK_RETENTION_DAYS` | Source-preserving signed redirects and bounded click records. |
| `FX_*` | Optional timestamped reference display. |

Webhook setup requires a bot token, random 32–256 character URL-safe secret, explicit
trusted hosts and TLS routing. Registration is an explicit operator operation; do not
run polling concurrently. Financial updates use the durable inbox in both modes.
See [runtime](runtime-hardening.md) and the preserved [operational procedure](beta-operations.md).

Free/Pro/Power sample settings are 0/250/750 Stars per 30 days, 2/50/250 shared
tracker/watch slots, and 12h/2h/1h real-store refresh intervals. Expiration preserves data
but only eligible quota-bound subscriptions are scheduled. Automated tests use simulated
Stars and spend nothing. [Billing operations](billing.md) retain lifecycle, upgrade,
refund/reconciliation and support commands; live activation was intentionally not pursued.

The adapter contract is documented by the existing `StoreProvider` implementations:
exact URL allowlists, fixed official API hosts, validated DTOs, public-IP-pinned transport,
bounded redirects/responses/time and permission-gated capabilities. There is no arbitrary
URL scraper. [Integration guides](index.md#integrations) preserve source-specific details.

## Migration, retention and recovery

For an existing local database, stop/drain processes, back up and verify compatibility,
then migrate once. The legacy evidence indexer is resumable:

```bash
uv run alembic upgrade head
uv run alembic check
uv run python -m pricehunter.apps.admin catalog-backfill
```

Repeat backfill until it reports zero before restarting old catalog deployments.
Do not discard immutable data to force a downgrade. Exact schema checks run at startup.

History is append-only by default. Opt-in pruning retains offer lifetime aggregates and
tracker baselines, removes at most 5,000 observations per invocation and never removes
payment evidence. Review retention consequences explicitly before enabling it.

```bash
# Requires a deliberately configured positive HISTORY_RETENTION_DAYS:
uv run python -m pricehunter.apps.admin prune-history
uv run python -m pricehunter.apps.admin outbox
uv run python -m pricehunter.apps.admin runtime-preflight
uv run python -m pricehunter.apps.admin runtime-status
```

Use the [backup/restore guide](disaster-recovery.md); volumes/AOF are not backups.
Uncertain delivery requires reconciliation, not automatic resend or history deletion.

## Full quality gates

Integration tests truncate application tables. Use a newly created dedicated disposable
database ending `_test` and Redis **15**; recovery/operator acceptance also owns Redis
**14**. Never share either Redis DB with an application or another concurrent test run.
Matching `pg_dump` and `pg_restore` majors must be first on PATH; Compose/CI use 17.

```bash
docker compose exec postgres createdb -U pricehunter pricehunter_test
export TEST_DATABASE_URL=postgresql+asyncpg://pricehunter:pricehunter@localhost:5432/pricehunter_test
export DATABASE_URL="$TEST_DATABASE_URL"
export TEST_REDIS_URL=redis://localhost:6379/15
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run alembic upgrade head
uv run alembic check
uv run python scripts/verify_m3b_migration.py
uv run pytest --cov=pricehunter --cov-report=term-missing --cov-fail-under=85
```

Without `TEST_DATABASE_URL` integration tests skip; that is not a full verification.
The final operator acceptance cases also require `TEST_REDIS_URL`. Normal CI supplies
PostgreSQL/Redis and runs all gates. HTTP fixtures and a simulated Telegram transport
replace retailer/payment calls. `uv run pytest tests/unit` is useful only as a narrower
local check. See [verification evidence](verification.md) and [load profiles](load-rehearsal.md).
