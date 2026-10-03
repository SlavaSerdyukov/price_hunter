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

## M3A comparison core — 2026-09-20

- Full suite: **337 passed**, no skips, **88.51% total coverage** (baseline: 282 tests).
  PostgreSQL 17 `pricehunter_test` and real Redis DB 15 were isolated from the live bot.
- `uv run ruff check .`, `uv run ruff format --check .`, strict mypy (83 source files)
  and `uv lock --check --offline` passed. All 127 Python files passed formatting.
- `alembic upgrade head` and `alembic check` passed at revision `124441561d5e`.
  A separately created disposable database was migrated through M2, seeded with an
  existing user/store/product/offer/tracker/history, upgraded and backfilled twice.
  IDs and pre-existing data were preserved; backfill was idempotent. Downgrade rejected
  existing watches, then succeeded after their explicit removal; re-upgrade/check passed.
- Acceptance covers concurrent strong-identity persistence; conservative conflicts,
  size/capacity variants and weak bridges; persisted REST comparisons; currency/stock
  policy; partial providers and deterministic ranking; merchant switching, replay,
  targets, restock, cooldown, discovery, shared quota races and expiry; faster scheduling;
  seven-language Telegram comparison/watch/exact-tracker flows and bounded escaped cards.
- Existing eBay, Amazon and WooCommerce fixtures go through real adapter normalization
  and the resolver. eBay marketplaces and compatible Amazon ASINs unify; WooCommerce
  SKU/title-only evidence stays separate. These are offline fixtures, not live prices.
- CI now requires `--cov-fail-under=85`, with no new coverage exclusions. This record is
  a local CI-equivalent run; no hosted GitHub Actions execution is claimed.
- The sole test warning remains ARQ's upstream use of deprecated Redis `close()`.
  No billing service refactor or real Stars transaction was performed.
- A clean archive of the committed branch installed successfully with frozen/offline
  dependencies and `--no-editable`, using only `.env.example`; all **218 unit tests**
  passed there. Packaged seven-language catalogs and API imports passed independently
  of the working tree. README local links resolve in that archive.
- Local Docker API/bot/worker images were rebuilt and restarted after a restricted
  `.local/backups/before-m3a-20260920T151416Z.dump` backup. Migration preserved 2 users,
  2 exact trackers, 19 offers and 194 observations at the upgrade boundary. Legacy
  backfill indexed 19 products, then zero on repeat. Runtime Alembic check passed.
- Updated HTTP `/health/live` and `/health/ready` returned 200; ARQ health passed with
  no failed jobs. No manual Telegram message, real payment or Amazon activation was
  performed by these checks. Existing configured tracking resumes normally.

## M3B autonomous discovery and freshness — 2026-09-21

Baseline was recorded before implementation in [m3b-design.md](m3b-design.md):
M3A `6da2c03`, **337 passing tests, 88.51% coverage**. Work remains on
`feat/m3b-autonomous-discovery`; existing billing services were preserved.

- Final full suite: **399 passed**, no skips, **89.45% statement coverage**. The 85%
  gate is unchanged. Ruff lint/format (140 Python files), strict mypy (89 source files),
  frozen/offline lock verification, `alembic upgrade head` and `alembic check` passed.
- Tests used PostgreSQL 17 `pricehunter_test` and real Redis DB 15. Retailer and Telegram
  calls used fixtures/test transports; these checks spent no Stars and sent no messages.
- Acceptance verifies stale EUR 299 cannot beat fresh EUR 329/345, discovery changes
  best to EUR 315 once, expiry/refresh returns EUR 320, and refresh restores EUR 310.
  History and notification outbox record one transition per accepted change/replay.
- One hundred watchers share one discovery target/search; concurrent claims, execution
  locks, expired tokens (including late failure reports), stale batch rollback, lost
  enqueue recovery, pause/removal, plan changes, result bounds and context validation pass.
  Provider failures/empty results/suppression are distinct; other providers continue.
- eBay summary → item details enrichment uses the actual adapter and HTTP fixtures,
  with a GTIN query and separately limited details. No identity is inferred from the
  query. Amazon ASIN discovery/currency/no-result paths are fixture-tested; live access
  and tracking approval remain pending.
- Freshness SQL and Python policy agree at exact TTL boundaries, provider/category
  overrides, zero TTL, future timestamps, failures and quarantine. Unknown/stale stock
  cannot produce a confirmed-restock claim. Current-best reads remain time-aware even
  before expiry maintenance runs.
- At 100/500/1000 offers, full comparisons use **six SQL queries**, bounded offer pages,
  complete currency summaries and deterministic paging. These checks bound query count,
  not production throughput; no wall-clock performance guarantee is claimed.
- History tests cover canonical transitions, per-currency separation, plan windows,
  bounded points, boundary anchors, retention, unchanged current state and observation
  source references after pruning. Manual refresh uses the existing shared worker,
  respects active leases/request limits, and performs no synchronous retailer calls.
- All seven Telegram languages exercise comparison, fresh/stale labels, best-price
  history, refresh acknowledgements and existing watch/exact-offer/notification flows.
  Existing billing tests and concurrency tests remain. Two old matcher expectations were
  updated specifically for M3B's approved identical-GTIN/missing-optional-metadata rule;
  explicit conflicts, opaque variation identifiers and weak matches remain rejected.
- Operator diagnostics and duplicate reports are bounded/read-only and covered. A safe
  product merge is explicitly deferred: preserving conflicting watch settings, identity
  redirects, discovery leases and immutable histories/outbox requires a reviewed design.
- `scripts/verify_m3b_migration.py` creates/removes a separate disposable database. The
  fresh migration chain and M3A → M3B upgrade preserve every pre-existing column/ID in
  users, stores, products, offers, trackers, observations, product watches, outbox,
  subscriptions and payments. Existing best-state initialization is idempotent; history
  protects downgrade; explicit test-history removal permits downgrade/re-upgrade/check.
  CI now runs this verifier before pytest.
- The only test warning remains ARQ's upstream deprecated Redis `close()` call. Hosted
  GitHub Actions, live Stars lifecycle and live Amazon behavior were not exercised.

### Local M3B deployment

- Frozen Docker API/bot/worker images were rebuilt. Application processes were stopped
  before `.local/backups/before-m3b-20260921T132549Z.dump` was created with mode `0600`
  in a `0700` directory. The backup stays outside Git/build context.
- Revision `2872920b653a` applied; runtime `alembic check` passed. Counts before/after
  the migration matched: **2 users, 1 exact tracker, 32 products, 36 offers,
  467 observations, 1 product watch, 1 outbox event, 0 subscriptions, 0 payments**.
- Restarted API, bot and worker. HTTP liveness/readiness both returned 200, and the
  ARQ health check passed. The running comparison endpoint exposes freshness fields;
  canonical history returns the correct Free window. The temporary API user and key
  used for read-only checks were removed afterwards.
- Worker logs contained six completed discovery batches, no discovery failure or
  traceback at inspection, and healthy `j_failed=0` markers. This confirms local job
  execution; bounded search completion is not a guarantee of new matching inventory.
- Amazon remains disabled. No manual Telegram message, invoice, payment or purchase
  was sent by verification; existing authorized tracking/discovery resumes normally.
- A clean Git archive installed with frozen/offline dependencies and `--no-editable`.
  All **249 unit tests** passed against that installed package (`uv run --no-sync`).
  Seven packaged locales, `.env.example` and all local README/roadmap/docs links passed
  independently of the working tree. No local `.env` or development-only source path
  was required for this package verification.

## M4A international commerce — 2026-09-28

Implemented on `feat/m4a-international-commerce`. The pre-change audit and design
are in [m4a-design.md](m4a-design.md); operating instructions are in
[international-commerce.md](international-commerce.md).

1. **M3B baseline.** PR #2 was merged as `0d6d493` and
   [main CI passed](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/35608647734)
   before implementation. The local baseline was **399 passed, 89.41% coverage**,
   with lint, formatting, strict typing, schema comparison and migration verification
   passing. No M4A hosted CI execution or deployment is claimed by this local report.
2. **Schema.** Revision `2c125500eaf6` adds required watch market, market-aware watch
   uniqueness, merchant external ID, affiliate metadata, nullable direct URL, optional
   delivery fields, minimal OutboundClick and Decimal FxRate tables. Billing schema
   and services were not redesigned. Downgrade refuses incompatible M4A data.
3. **Market migration.** The extended `scripts/verify_m3b_migration.py` successfully
   upgraded a real M3B-shaped scratch database, preserving all pre-existing columns
   and IDs in users/products/identifiers/stores/offers/observations/trackers/watches/
   discoveries/best states/history/outbox/subscriptions/payments. Existing watches
   inherit saved DE or legacy BE; new country-less accounts receive `country_required`.
   Tests verify BE/EUR and DE/EUR coexistence, separate discovery targets, unchanged
   watch country after profile edits, and country-preserving Telegram notification links.
4. **eBay EPN.** Fake HTTP exercises optional campaign context, returned affiliate
   URL persistence, direct fallback without campaign, explicit country/postal context
   and Belgian destination aliases. No per-user affiliate reference is sent or stored.
   Existing live Browse access does not establish live EPN attribution or data-use rights.
5. **Rakuten.** Fixture tests cover documented token form, cached concurrent renewal,
   one authentication retry, safe bounded XML, pagination, shared concurrent request
   limiting, MID merchant identity, market-scoped listing identity, Decimal sale rules,
   UPC validation, malformed-item isolation and partial provider failure. Stock stays
   unknown; SEARCH_MODEL does not claim GTIN filtering or item refresh. Reviewed
   search-only results create no history and expire through bounded cache eviction.
6. **Outbound links.** API/Telegram cards, comparisons, trackers and notifications use
   the central policy selector. Signed redirects resolve current server-side offers;
   tests reject tampered/expired tokens, unknown offers, inactive stores, unsafe
   schemes/hosts and query-injected destinations. Commission metadata does not alter
   native-price ranking or notification decisions. Configured attribution is rendered.
7. **Clicks and privacy.** A successful redirect appends a minimal event; rejected
   redirects append none. Schema assertions exclude Telegram/account IDs, IP, UA and
   other user metadata. Retention and foreign-key detachment are tested. Analytics
   have no public read endpoint; proxy logging remains a deployment responsibility.
8. **Reference FX.** ECB snapshots persist atomically with source, effective date and
   fetch timestamp. Tests cover Decimal direct/inverse/cross conversion, same and
   unsupported currencies, weekend reuse, stale cutoff, invalid rates/XML, shared
   fetch/cadence and failure backoff. Approximate display amounts leave native prices,
   per-currency best offers, alerts and Stars billing unchanged.
9. **Final checks.** **497 tests passed, no skips, 89.86% statement coverage**; the 85%
   gate is unchanged. Ruff lint and formatting (156 Python files), strict mypy
   (96 source files), `alembic upgrade head`, `alembic check`, migration preservation /
   guarded downgrade / re-upgrade, and local README/roadmap/docs links all passed.
   Tests use dedicated PostgreSQL `pricehunter_test` and Redis DB 15. All seven bot
   languages remain covered. The sole warning is the existing upstream ARQ call to
   deprecated Redis `close()`. Retailer and Telegram test transports sent no messages,
   made no purchases and spent no Stars.
10. **Live verification and remaining gates.** A read-only request through the real
    public HTTP transport successfully parsed official ECB daily XML at
    `2026-09-28T11:19:55.421921+00:00`: 29 currencies, effective date **2026-09-25**.
    It wrote no database data. Rakuten and EPN remain fixture-tested, credential/policy-
    gated: live token/account/MID access, campaign attribution, actual returned links,
    display requirements and commission eligibility still need account-specific checks.
    Amazon retains its existing approval gate. The running local application remains
    M3B; this work did not modify its `.env`, migrate its database or restart its services.
    Configure reviewed policies for enabled real sources before an M4A upgrade.
11. **Proposed M4B scope.** Add explicit delivery country/postal input, documented
    shipping/tax and destination availability, unknown-cost semantics and separately
    labeled delivered-cost comparison only when required components are known. Add
    reviewed Awin/CJ feed onboarding with identifier, variant, stock and retention
    provenance. Keep native ranking, deterministic matching and existing billing;
    automatic merge, frontend/Mini App/mobile, new checkout methods and ML matching
    remain outside that scope.

## M4A.1 market correctness and snapshot ingestion — 2026-09-29

Implemented on `feat/m4a-international-commerce`, starting at
`6297346194226e628c92cf668d1e2a2c78cc9791`. The unchanged baseline passed
**497 tests with 89.86% coverage**. Before implementation, two new regressions
reproduced BE comparison selecting a cheaper DE offer and repeated Rakuten search
retaining its original price/link. The design is recorded in
[m4a1-market-correctness.md](m4a1-market-correctness.md).

1. **Market schema.** Required `StoreOffer.market_country` comes directly from
   `ProductOfferData.country`. Listing uniqueness is merchant/external ID/market;
   the product/market/currency index supports comparison and refresh queries.
   `ProductBestState` and `BestPriceEvent` series include market. Product stays
   global, and Store stays a merchant identity with a legacy/primary country.
2. **Legacy history.** All pre-existing global best events retain their IDs and
   values with NULL market. They are excluded from market-specific history; no
   historical market is inferred from the winning offer. Old best-state rows remain
   inactive with NULL market. Bounded maintenance silently rebuilds enabled watch
   baselines, including watches paused by quota; repeated maintenance is idempotent
   and emits no migration notifications.
3. **Comparison.** Search, product details, offers, best history and manual refresh
   resolve explicit country, then saved country, otherwise `country_required`.
   CountryCode validation rejects unsupported codes. BE → DE → BE search retains
   one canonical Product and returns only the requested market each time. Native
   currency ranking and display-only FX conversion remain unchanged.
4. **Watches and notifications.** Creation, evaluation, scheduling, worker demand,
   history and refresh use product/market/currency. The exact acceptance scenario
   passes: BE 329/340 and DE 299/315 baseline separately; DE 289 affects only DE,
   then BE 300 affects only BE. One hundred watchers share a tracking summary and
   a history summary per market. Delivery cancels mismatched watch/offer markets.
   Seven-language Telegram callbacks and outbound links preserve the originating
   market after profile changes. Exact-offer tracking and billing remain covered.
5. **Snapshot architecture.** `SNAPSHOT_REFRESH` capability selects explicit
   `SEARCH_SNAPSHOT` ingestion. CatalogResolver validates and locks identity;
   SnapshotUpdater handles mutable listing fields under those locks. Generic search
   from REFRESH-capable providers cannot overwrite worker-owned state. Real source
   timestamps reject stale/replayed/future versions; providers without timestamps
   use serialized acceptance time. No provider-name condition chooses ingestion.
6. **Repeated Rakuten search.** Adapter fixtures verify 349/link A → 299/link B on
   the same StoreOffer, advancing freshness without duplicate products/listings,
   observations or tracking notifications. Concurrent replay is idempotent for
   historical side effects. Search-only aggregates reflect only the current value;
   an explicitly history-authorized snapshot provider records changed observations.
   Catalog, tracking and history permissions remain independently market-scoped.
7. **Migrations.** Revision `b7c21a48d903` backfills a valid metadata market, otherwise
   Store.country, then enforces non-null offer market and country format. The scratch
   verifier passes M3A → real M3B → M4A → M4A.1, preserving pre-existing IDs and data
   across catalog, tracking, history, outbox and billing, except documented derived
   baseline/state rebuild markers. Guarded downgrade, explicit removal of incompatible
   synthetic test data, re-upgrade and Alembic schema comparison all pass.
8. **Final checks.** **513 tests passed, no skips, 90.08% statement coverage** against
   dedicated PostgreSQL `pricehunter_test` and Redis DB 15; the coverage gate remains
   85%. All required `uv run` gates passed: Ruff lint, Ruff format check (162 Python
   files), strict mypy (99 source files), `alembic upgrade head`, `alembic check`,
   `python scripts/verify_m3b_migration.py`, and the full coverage suite. Offline lock
   verification, `git diff --check`, and local README/roadmap/docs links also pass.
   The sole warning remains ARQ's upstream deprecated Redis `close()` call.
9. **Limitations.** Unversioned upstream responses cannot prove their remote source
   order; the accepted ingestion order is authoritative. Legacy global history stays
   preserved but absent from scoped user history. Live Rakuten account/MID access and
   EPN attribution remain subject to existing credential and policy gates; Amazon
   approval is unchanged. No new merchant integrations or M2 redesign were introduced.
   Tests used retailer/Telegram fixtures, sent no messages and spent no Stars. The
   running M3B application, its database and `.env` were not changed by this task.
10. **Merge readiness.** Both reported correctness gaps are closed and all local
    CI-equivalent checks pass. The implementation is ready for review and merge on
    that evidence. Hosted CI for these new changes has not been run; this record
    does not claim a commit, push, merge or deployment. Apply normal branch CI and
    the documented provider-policy configuration before deploying M4A.

## M4B commerce feeds — 2026-09-29

Implemented on `feat/m4b-commerce-feeds`, from merged main
`e9dde18de3d805aaaa971e534677ced766bc13ae` (M4A/M4A.1, PR #3). The full unchanged baseline
passed **513 tests with 90.10% coverage**. Seven new architecture/acceptance cases failed
before implementation. [The design](m4b-design.md) preceded network adapters.

1. **Architecture.** Removed the unused AffiliateProvider product hierarchy. FeedSource
   streams normalized FeedProductData into separate staging; one FeedStoreProvider per
   network queries staging and sends selected ProductOfferData through the existing
   CatalogResolver and SnapshotUpdater. Actual merchant Stores remain customer-visible.
2. **Schema.** Revisions `ff5e1c895ca6` and `431e7de33df9` add MerchantProgram,
   MerchantFeedItem, FeedPendingItem, FeedSyncState, optional StoreOffer program context
   and identifier/full-text indexes. Program uniqueness is network/advertiser/market;
   Store identity is stable network/advertiser. Cross-network merging is not attempted.
3. **Policy.** Central PolicyResolver supplies SQL EXISTS and object checks for individual
   programs. Catalog, tracking, history, background refresh, affiliate, attribution and
   cache rules remain independent. Revocation is checked again under the snapshot lock.
   Direct eBay/WooCommerce/Amazon/Rakuten policies need no program rows. BE/DE sharing
   a Store may use different approved direct-link domains.
4. **Sync.** SKIP LOCKED claims, leases, heartbeat, fencing, bounded batches, backoff and
   persistent generations protect retries and lost queues. Failed/partial/changed-source
   attempts leave current data untouched. Completion preserves staging/listing IDs;
   disappearance expires offers without inventing stock or deleting canonical history.
   Restart replays acquisition from the beginning; compressed byte-range resume is not
   claimed. Dry runs check source access/version and keep deduplication on temporary disk.
5. **Awin.** Documented legacy CSV/gzip download and feed-list adapter implemented and
   fixture-tested. Normalization covers electronics, fashion sizes/colors/parents,
   identifiers, sale/original price, delivery, stock/unknown, missing fields, malformed
   rows, duplicate rows and affiliate-only links. See [source audit](awin-setup.md).
6. **TradeDoubler.** Official productFeeds/productsUnlimited/lastUpdated adapter implemented
   and fixture-tested for authentication, pagination/completeness, currency/merchant,
   prices/identifiers/links, variants, malformed rows, versions and HTTP failures. Sale
   semantics not established by the audited format stay absent; upstream historical
   prices are not imported as observations. See [source audit](tradedoubler-setup.md).
7. **Large feeds.** Synthetic **1,000 and 100,000 row** CSV streams are consumed lazily
   with at most one yielded row ahead. Database tests import/replay both sizes in batches
   of 500, keep canonical Product empty until selection, preserve staging IDs and verify
   indexed GTIN/MPN lookup. The initial import query bound is fewer than six queries per
   batch plus 40; no wall-clock or production-throughput guarantee is asserted. Explicit
   staging statistics prevent a quadratic replay plan after rapid bulk ingestion.
8. **Local validation only.** Local suite: **568 passed, no skips, 90.46% statement coverage**;
   the 85% gate is unchanged and the feed engine is included. Tests use dedicated
   PostgreSQL `pricehunter_test`, Redis DB 15 and fake retailer/Telegram transports.
   Ruff lint/format (184 Python files), strict
   mypy (110 source files), frozen/offline lock verification, Alembic upgrade/check and
   the disposable migration verifier pass. The verifier preserves pre-existing IDs and
   columns across users, billing/payments, catalog/identifiers, observations, trackers,
   watches, discoveries, best states/history, outbox, FX and outbound clicks. Populated
   merchant programs protect downgrade; explicit synthetic cleanup permits the complete
   downgrade/re-upgrade/schema comparison. Local documentation links and diff whitespace
   checks pass. An offline wheel build includes both adapters, feed services and CLI;
   local `.env` is absent from the package. The only test warning remains ARQ's upstream
   Redis `close()` deprecation.
9. **Operator configuration.** Defaults keep both networks disabled. Activation needs a
   network secret, approved/reviewed merchant-program rows, numeric advertiser/feed IDs,
   market/currency/domain and explicit FEED_PROGRAM_IDS. Startup fails usefully for missing
   credentials/programs. Run program-check and feed-sync --dry-run before importing live
   data; no production secret or real account is needed for tests.
10. **Onboarding targets.** The generic mechanism is ready for approved advertisers that
    supply the audited formats. Coolblue, MediaMarkt, Samsung, Decathlon, adidas, ABOUT YOU,
    Fnac, Conrad and Zalando Lounge are prospective targets only. Their actual networks,
    program access and fields need confirmation. The [matrix](merchant-programs.md) marks
    every real target **not configured**; only synthetic programs are fixture-tested.
11. **Live gaps.** Credentials, advertiser approvals, cache/history/tracking/attribution
    agreements, real completeness/cadence, affiliate links and commission eligibility
    have not been verified. TradeDoubler's unchanged-download quota and pagination quota
    accounting need account-specific confirmation. Shared pacing is an application limit,
    not a promise of network quota. No new network was enabled, `.env` changed, application
    database migrated or running service restarted. Amazon approval remains unchanged.
    Hosted CI, commit/push/merge and deployment are not claimed by this local record.
12. **Recommended M4C.** Onboard a small approved BE/DE pilot with evidence for prices,
    variants, completeness, cadence and attribution. Then add explicit destination/postal
    context and documented delivery/tax fields, comparing delivered cost only when all
    required components are known. Preserve native rankings, deterministic matching and
    billing; scraping, ML matching, new frontend and new payment providers remain separate.

## M4B.1 CI and feed revalidation — 2026-09-30

Starting commit: `6ccbc18d8096096a3a749f986cf1f1a35a330f79`. The earlier M4B figures
above describe local verification only. [Hosted run 36637252541](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/36637252541)
failed: **567 passed, 1 failed, 90.46% coverage**. All lint, typing and migration steps
passed; the failure was `test_search_persists_comparisons_and_api_returns_all_stores`.
Its exact JSON comparison included `age_seconds` measured separately for each request.
A local reproduction with a real 1.05-second boundary between requests fails the same
assertion. The corrected test keeps every stable field strict and separately verifies
nonnegative, current and monotonic ages. That failed hosted result is superseded by the
verified M4B.1 run recorded below.

M4B.1 implementation separates completed-feed presence from content version advancement.
Equal/older source versions retain current content while confirmed presence advances.
Unchanged feed versions use model B: a full download is mandatory at the merchant cache
deadline, with next sync capped by that deadline. Evicting active cache rows reschedules
the program immediately; cleanup respects active leases and completion lock order.
Explicit FeedRevalidationContext is verified against the current completed generation,
program policy and exact staged payload. It restores catalog activity using the recorded
confirmation time, without fake observations; ordinary stale snapshots remain rejected.

The original pagination failure was reproduced locally. Its corrected test passed
**10 consecutive runs**, each crossing a real second boundary. Of the first ten new feed
regressions, seven failed before implementation; existing generic stale-version protections
passed. Additional tests cover incomplete/forged generation evidence, partial sync, active
leases and inactive cleanup. There are **15 new feed regression cases** in total.

Final local verification: **583 passed, no skips, 90.51% statement coverage**. The 85%
gate and coverage scope are unchanged. Required `uv run` checks passed: Ruff lint,
Ruff format (185 Python files), strict mypy (110 source files), Alembic upgrade/check,
the disposable migration verifier and the full coverage suite. No schema migration
was needed; the existing migration chain still preserves catalog/history/billing/FX/click
records. Dedicated PostgreSQL `pricehunter_test`, Redis DB 15 and fixture transports
were used; the only warning is the existing upstream ARQ Redis `close()` deprecation.
Offline lock, documentation links and diff whitespace checks also passed.

**Verified hosted result:** [GitHub Actions run 36679699753](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/36679699753)
completed with **success** for commit `56b6563ae07447ef5d1a841bcea6f76f7540fb11`
on `feat/m4b-commerce-feeds`. The hosted log reports **583 passed, no skips, 90.53%
statement coverage** and the same single upstream ARQ warning. Dependency installation,
Ruff lint/format, strict mypy, Alembic upgrade/check and the migration verifier all passed.
The hosted coverage is recorded separately from the local 90.51% measurement.

This result was fetched from the completed run/job and its actual logs before this
verification record was updated. M4B.1 addresses the pagination and feed-revalidation issues.
The subsequent documentation-only commit `89e4bca` exposed another timing-dependent
assertion in [run 36681050214](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/36681050214):
**582 passed, 1 failed, 90.47% coverage**. The failed provider-order test also compared
complete SearchResult objects across separate calls; the log diff differs in age_seconds.
The first successful run is historical evidence, not the current merge-readiness decision.

The follow-up applies a shared test-only semantic comparison helper to pagination,
provider-order/partial-failure search and 100/500/1000-offer load pagination. Every field
except age_seconds stays strict; ages are separately bounded by each request's actual
time and checked for monotonicity. Real second-boundary delays exercise all three
scenarios. Production API/time behavior and query-count assertions are unchanged.
Follow-up local verification passed: Ruff lint/format, mypy, Alembic upgrade/check,
the complete migration verifier and **583 tests with 90.51% coverage**. Both tests
that previously failed in hosted CI also passed **10 consecutive repetitions each**
with real second-boundary delays.

**Verified follow-up hosted result:** [GitHub Actions run 36714978262](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/36714978262)
completed with **success** for commit `4d6881afadc12f8e2e067e4c11e5c2ac22692a34`
on `feat/m4b-commerce-feeds`. Actual hosted logs report **583 passed, no skips, 90.51%
statement coverage** and the single upstream ARQ warning. All required lint, format,
typing, Alembic and migration-verifier steps passed. This result was recorded only
after fetching the completed run and its logs. M4B is merge-ready on this evidence;
the documentation-only commit recording it must also pass the same Checks workflow.
No merge, deployment or live merchant activation is claimed.
