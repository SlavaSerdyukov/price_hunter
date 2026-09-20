# First iteration verification

Automated checks ran locally on 2026-09-18 with Python 3.12.14, isolated PostgreSQL 15
and Redis 7.4.6. They used no production database, Telegram account or retailer API
credentials. A subsequent live Telegram connection check is recorded below.

| Check | Result |
| --- | --- |
| Ruff lint and formatting | Passed |
| Strict mypy | Passed, 60 source files |
| pytest | 65 passed, no skipped tests with test services configured |
| Alembic upgrade | All three revisions applied |
| Alembic model/schema comparison | No pending changes |
| ARQ worker process, startup cron and refresh job | Passed against real Redis/PostgreSQL |
| HTTP `/health/live` | 200, `ok` |
| HTTP `/health/ready` | 200, `ready` |
| `uv.lock` consistency | Passed |
| Python wheel build and import from extracted wheel | Passed |
| Docker Compose configuration validation | Passed |

Acceptance coverage includes the real aiogram dispatcher/FSM with a simulated Telegram
transport; REST authentication and ownership; shared refresh for 100 subscribers with
one provider call; 100 persisted notifications from the batch; no database transaction
held during that call; quota races; observation/notification replay; stale worker leases;
currency/extreme-drop quarantine; target cooldown; ambiguous and crashed delivery;
and concurrent payment-ledger deduplication.

The only test warning comes from ARQ 0.28 calling Redis's deprecated `close()` method.
Application-owned Redis cleanup uses `aclose()`. No dependency code was modified.

## Local Docker and live Telegram checks

On 2026-09-18, the operator supplied a bot token through the local, Git-ignored `.env`
(file permissions `0600`). The Docker build context excludes that file; Compose injects
credentials at runtime.

| Check | Result |
| --- | --- |
| API, bot and worker image builds | Passed with the frozen dependency lock |
| PostgreSQL 17 / Redis 7 containers | Healthy |
| Alembic upgrade in the API image | All three revisions applied |
| API, bot and worker containers | Running |
| Containerized HTTP health endpoints | Both 200 (`ok`, `ready`) |
| ARQ health check | Passed; no failed jobs at inspection |
| Real Telegram `getMe` | Passed; bot credentials accepted |
| Real Telegram `getWebhookInfo` | Passed; no webhook configured |
| Telegram polling process | Started successfully |
| Real chat, tracking and notifications | Confirmed working by the operator on 2026-09-18 |
| Notification outbox inspection | Two sent events, both with Telegram message IDs |

PostgreSQL and Redis use local ports 55432 and 56379 to coexist with other development
services. Compose supports `POSTGRES_PORT`, `REDIS_PORT` and `API_PORT` overrides;
container-to-container connections retain the default internal ports.

The operator confirmed that the live bot tracks the demo product and sends notifications.
Retailer prices in that run remained deterministic mock data; no live retailer credentials
were configured.

## eBay Belgium and EU readiness

On 2026-09-18, the adapter was expanded for BE/DE/FR/NL/IT/ES/AT/IE/PL. Tests cover
marketplace headers, Belgian domain aliases and French/Dutch locale selection,
variation lookup and refresh, native-currency prices, localized size/color attributes,
partially malformed search results, invalid OAuth responses, bounded token renewal,
and credential-safe diagnostic output for 401/403/429 responses.

- Full suite: **94 passed**, no skips, using a separate PostgreSQL 17 `pricehunter_test`
  database and Redis DB 15 alongside the running development stack.
- Ruff lint/format and strict mypy: passed (61 source files).
- `python -m pricehunter.apps.check_ebay --country BE`: correctly exits with code 2
  and names missing environment variables without printing secret values.
- The operator has no eBay API keys yet. eBay remains disabled; no live eBay price
  or API-access claim is made. Follow [the setup guide](ebay-setup.md) to obtain access.

Not executed: live eBay account access, hosted GitHub Actions run, production deployment
or load testing. Stars checkout and the M2–M5 integrations remain disabled extension
points.

Reproduction commands are in the README. Integration tests only operate on explicitly
configured disposable databases ending in `_test`; the live Redis queue test uses DB 15.

## Public physical stores — 2026-09-19

- Added PINE64 EU and RaspberryPi.dk using the public WooCommerce Store API; no
  retailer account, token or browser session was used.
- Live calls through the application's public-IP transport passed search, product URL
  lookup and refresh for both shops. PINE64 EU returned the Pinecil/power bundle at
  EUR 140.00 (out of stock); RaspberryPi.dk returned the exact Flash Drive 256GB
  variant at DKK 499.00 (in stock).
- Stored minimal public API fixtures for simple products, variable parents and a
  specific variation. All automated tests remain independent of retailer network access.
- **123 tests passed**, no skips, including variant persistence, a price refresh and
  one correctly denominated outbox event in a separate test database.
- Ruff lint/format and strict mypy passed (63 source files).
- Rebuilt and restarted API/bot/worker with both shop adapters enabled. HTTP live/ready
  checks and ARQ health passed; Telegram polling started successfully.
- Live REST verification created temporary API-only trackers for PINEPOWER Desktop v2
  (EUR 76.75) and Flash Drive 256GB (DKK 499.00). The running ARQ worker persisted a
  second observation for both. The temporary account, key and trackers were removed;
  catalog entries/history remain available. No Telegram messages were sent by this check.
- The live Telegram price-change notification path was previously confirmed; a real
  retailer price drop has not been induced or claimed during this verification.

Setup, sample URLs, selection behavior and current limitations are in
[public-stores.md](public-stores.md).

## Clothing and Amazon — 2026-09-19

- Added Hemptees and Western Shop Bruxelles, enabled in the local runtime. Their
  public APIs passed search, lookup and refresh. Verified Hemptees Short Sleeve Tee
  Max, XS / Pomelo Red (variation 6263, EUR 40, in stock), and Western Shop Speed Shop
  (14662, EUR 49, in stock). Prices are observations at verification time.
- Hemptees requires `parent=0` during slug lookup to exclude variation rows sharing
  the parent slug. Its EUR API prices use zero decimal places; normalization follows
  the supplied currency precision instead of assuming cents.
- Telegram's dispatcher/FSM was tested with simulated transport: variant labels,
  pagination, selected offer persistence, tracking and rejection of stale/foreign buttons.
- Full suite: **161 passed**, no skips; Ruff lint/format and strict mypy passed
  (65 source files). The existing ARQ dependency deprecation warning remains.
- Rebuilt API/bot/worker. Live/ready endpoints returned 200; Telegram polling started.
  A live REST check created temporary API-only trackers for the two clothing offers.
  The running worker increased both observation counts from 1 to 2. Temporary user,
  key and trackers were removed; catalog/history remain. No Telegram messages were
  sent by this verification.
- Intermittent Hemptees timeouts were observed. A complete-operation timeout now maps
  to `provider_unavailable` (HTTP 503), releases the provider slot, and never saves a
  zero price. The live REST check succeeded on one bounded retry.
- Amazon Creators API is implemented using LwA 3.x and OffersV2, tested with synthetic
  fixtures. No Amazon credentials or tracking agreement are available. Amazon remains
  disabled; its diagnostic exits 2 without network requests. Live access and
  agreement-specific storage/display behavior remain unverified prerequisites.

## eBay credential check — 2026-09-19

The operator added Production-formatted App ID / Cert ID to the ignored local `.env`.
The production token endpoint returned **HTTP 401, `invalid_client`**. No Browse
catalog request succeeded; eBay stays disabled pending an accepted active keyset.
No keys or token values were printed. The diagnostic now distinguishes OAuth failures
from failures of a later Browse call.
Re-entering both credentials produced the same OAuth rejection. Configuration checks
confirmed one assignment per key, no process environment override, no whitespace and
matching values between `.env` and Settings. A regression test verifies that OAuth
rejection is reported separately without making a Browse request.
After that diagnostic change, the complete test suite passed: **162 tests**, no skips.

### Subsequent eBay check — successful

On the operator's next requested check, Production OAuth succeeded. Browse search
also returned products, exposing a missing alias in the application's allowlist:
the live Belgian response uses `www.benl.ebay.be`. Added that exact host and its
French counterpart `www.befr.ebay.be`, including correct locale selection.

- Production diagnostic: OAuth, BE search (10 usable results), lookup and refresh passed.
- The verified listing 407227767909 returned its native USD price (176.65 at the
  time of the check) and in-stock status. No FX-converted price was stored as native.
- Regression cases cover both www Belgian aliases. **164 tests passed**, no skips;
  Ruff and strict mypy passed.
- Enabled eBay in the local `.env`; secrets remain ignored and were never printed.
  The prior `invalid_client` rejection is resolved for the current credentials.
- Rebuilt and restarted API/bot/worker with eBay enabled. API readiness returned 200
  and Telegram polling started. A temporary API-only tracker resolved the real listing
  through the running API; the running worker increased its history count from 1 to 2.
  The temporary account, key and tracker were deleted; catalog/history were retained.
  No Telegram notification was sent by the verification.

### eBay listing groups — user-reported link regression

The operator's link `https://www.ebay.de/itm/167526377039` returned Browse HTTP 400
with code 11006: the legacy listing represents a group, not one purchasable variation.
The previous adapter mapped that response to a generic provider-unavailable message.

- Added bounded extraction of numeric HTTP 400 provider error codes. For eBay 11006
  without an explicit variation, request the fixed `getItemsByItemGroup` endpoint,
  validate group/variation identities and populate the existing per-user choice buttons.
  Error messages and arbitrary response hrefs are never used as request destinations.
- Real API returned 11 shoe sizes. Selecting UK 5.5 (variation 467150657985) resolved
  and refreshed at EUR 239.95. Selected shoe-size aspects and variation identity are
  retained; generic seller size ranges are excluded from the selected size.
- Regression coverage includes full aiogram parent-link → selection → card → tracker →
  refresh, no offer persisted before selection, exact variant retention, mismatched
  variants/groups, unrelated/malformed/oversized errors, and untrusted error hrefs.
- **175 tests passed**, no skips. Ruff and strict mypy passed. The existing ARQ
  dependency deprecation warning remains.
- Rebuilt and restarted API/bot/worker. The running API returned `variant_required`
  for the parent link and resolved variation 467150657985 with the selected UK 5.5
  size in its title. A temporary API-only tracker was refreshed by the running worker:
  its history count increased from 1 to 2. The temporary user, API key and tracker
  were removed; catalog/history remain. No Telegram notification was sent by this check.
- Telegram polling is running. Amazon remains disabled while the operator awaits
  approval; eBay and the four configured public WooCommerce stores remain enabled.


## M2 Stars billing — 2026-09-20

The M0/M1 services and retailer adapters were preserved. Added authoritative subscription
periods, versioned catalog/checkout contracts, a durable financial inbox, billing services,
Stars provider, central feature gates, bot UX and local operator reconciliation/refunds.

| Check | Result |
| --- | --- |
| Ruff lint | Passed |
| Ruff format check | Passed, 103 Python files |
| Strict mypy | Passed, 76 source files |
| Full pytest with PostgreSQL / real Redis DB 15 | **230 passed**, no skips |
| Statement coverage | **86%** overall; billing service 93%, entitlement service 98% |
| Alembic upgrade head / check | Passed; no pending schema operations |
| Fresh migration chain with existing M1 records | Passed |
| M2 downgrade / re-upgrade before M2 payments | Passed; old user, subscription and ledger preserved |
| Frozen Docker API/bot/worker builds | Passed |
| Operator CLI help / mocked service execution | Passed |

Executed equivalents of CI:

```bash
PYTHONPATH=src .venv/bin/ruff check .
PYTHONPATH=src .venv/bin/ruff format --check .
PYTHONPATH=src .venv/bin/mypy
# DATABASE_URL points to the disposable pricehunter_test database for these checks.
PYTHONPATH=src .venv/bin/alembic upgrade head
PYTHONPATH=src .venv/bin/alembic check
# TEST_DATABASE_URL is the disposable database; TEST_REDIS_URL uses DB 15.
PYTHONPATH=src .venv/bin/pytest --cov=pricehunter
```

Local test endpoints were PostgreSQL `localhost:55432/pricehunter_test` and Redis
`localhost:56379/15`. A separate `pricehunter_m2_migration_test` database was created
for the migration preservation check and removed afterwards. Tests never use live
Telegram/retailer credentials or spend Stars. The existing ARQ Redis.close deprecation
warning remains; strict mypy and existing tests were not disabled or weakened.

New coverage includes concurrent identical purchases (one event/period/subscription),
concurrent distinct pre-checkout attempts (one approval), recurring deadlines T1 → T2,
old-price renewals after catalog changes, checkout expiry/ownership/currency/amount,
delayed initial delivery and renewal arriving before the first update, durable retries,
refund idempotency and refunded-period gaps, upgrades and cancellation, uncertain-refund
reconciliation, cleanup/renewal concurrency, paginated scans and CLI diagnostics.
The exact required expiry case uses `2026-10-19T12:00:00Z`, an `active` row and a read
at `2026-10-19T12:00:01Z`: effective plan is Free.

EN/RU acceptance tests exercise the actual dispatcher through `/plans`, invoice-link
creation, pre-checkout, successful payment, `/subscription`, cancellation and support.
They also verify refund service messages, webhook database-failure retry behavior and
pre-checkout answering despite an occupied Redis conversation lock. Feature tests cover
tracker quotas/scheduling, shared paid offers, Free search/history bounds, queued paid
alerts after expiry, and API subscription metadata. Existing target-flow tests now
explicitly seed paid subscriptions, and the Free quota race expects the new limit of 2.

Operator-approved prices: **Pro 250 Stars / Power 750 Stars per 30 days**; payment support
**@slavasham**. Local billing settings were configured accordingly. External Telegram
digital checkout remains disabled; Amazon remains disabled while awaiting approval.

### Local deployment and runtime checks

- Rebuilt and started the API, bot and worker containers with M2 enabled. PostgreSQL
  and Redis remained healthy.
- Before upgrading the live local database, stopped application containers and saved
  `.local/backups/pre-m2-20260919T231138Z.dump` with file mode `0600` in a `0700` directory.
- Applied migration `0bc58a691a8b`; `alembic check` reported no pending operations.
  Before/after counts matched: 2 users, 2 trackers, 19 store offers, 192 price observations,
  0 subscriptions and 0 payment events.
- Both `/health/live` and `/health/ready` returned HTTP 200.
- Using a temporary API-only account, `/api/v1/subscriptions/me` returned Free with
  2 trackers, 7 history days, 3 searches and target alerts disabled. Attempting to create
  a target-price tracker returned HTTP 403 with `feature_requires_upgrade`.
- Removed the temporary account and API key after the check; no Telegram messages or
  invoice links were sent. Read-only Telegram `getMe` succeeded for `@hunterpr_bot`.
- Worker logs show successful recurring billing-maintenance jobs and `j_failed=0`.
  All three application containers remained running after the checks.

Not performed: real Stars purchase, automatic future renewal, live refund/cancellation,
live Star balance/history calls or hosted GitHub Actions. These are manual operator
checks described in [billing operations](billing.md), not claimed by mock transport tests.

## Seven interface languages — 2026-09-20

- Added French, German, Spanish, Italian and Polish alongside English and Russian:
  111 messages per language, covering tracking, stores, errors, notifications and billing.
- Initially, new Telegram accounts started in English; this default was subsequently
  replaced with Telegram-language detection (see below). Existing preferences remain
  saved. The main menu has a Language button; settings show
  all seven native language names with the selected language marked.
- The API accepts all seven language codes. Currency formatting uses each language's
  locale without currency conversion. Unknown locales and missing messages fall back
  to English. Payment confirmations and pre-checkout errors read the saved preference.
- **270 tests passed**, no skips; statement coverage **86%**. Ruff lint/format passed
  (105 Python files), strict mypy passed (77 source files).
- Tests verify catalog completeness, format placeholders, escaped values, Telegram HTML,
  invoice lengths, number formatting, English-first onboarding, persisted selection,
  notifications, settings API and the full Stars flow in all seven languages. Telegram
  payment tests deliberately use a profile language different from the selected language.
- No database migration or dependency change was required.
- Rebuilt and restarted local API/bot/worker containers. Confirmed all seven complete
  catalogs are present in the installed package, and both health endpoints return 200.
  The running API persisted each language on a temporary API-only account, which was
  then removed together with its key. No Telegram messages or payments were sent.

## Telegram language as the onboarding default — 2026-09-20

- New accounts initialize their interface language from Telegram's `language_code`.
  All seven supported languages work; regional codes such as `fr-BE` and `de-DE`
  map to the base language. Missing, empty and unsupported codes fall back to English.
- Saved language preferences remain authoritative on subsequent updates and `/start`.
  Telegram profile changes do not overwrite a user's language selection in the bot.
  Existing users can change their saved language in `/settings`.
- Pre-checkout rejection messages also use Telegram's language when there is no local
  user yet, without creating an account. Existing payment flows retain the saved language.
- **282 tests passed**, no skips. New dispatcher tests cover first contact in all seven
  languages, regional/fallback codes, and preservation of manual selection after a
  Telegram language change. Ruff lint/format and strict mypy passed.
- Docker Desktop was initially stopped; it was started and PostgreSQL/Redis readiness
  confirmed before the successful full test run. All three application images were rebuilt.
- API, bot and worker were restarted with the update. The installed package includes
  Telegram-language initialization; `/health/live` and `/health/ready` both returned 200.
- No database migration or dependency change was required.
