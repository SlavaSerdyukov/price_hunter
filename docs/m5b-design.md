# M5B — Delivery context and delivered cost

## Baseline and audit

Branch `feat/m5b-delivery-context` starts at merged M5A main
`b8da550ab973731a794b4e6f40e937534fcfd4a5`. PR #8 was already merged;
both final M5A Actions runs passed on `2e28fe90c81e76f2a4c7992baa150f86acede4b9`
([push](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/37221918148),
[PR](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/37221946430)).
Baseline local verification: ruff passed, 224 formatted files (not all Python),
strict mypy passed for 121 source files, Alembic upgrade/check and the preservation
verifier passed. Full baseline: **812 passed, no skips, 91.58% statement coverage**,
250.49 seconds; one existing upstream ARQ Redis close deprecation.

`ProductOfferData` has no normalized delivery fields. `StoreOffer` has unused
shipping/tax/country/postal placeholders without scope or freshness; they cannot
authorize a delivered total. `ComparisonOffer` has nullable placeholders;
`ComparisonReader` selects one item-price source per canonical merchant in SQL.
`ComparisonService` adds display FX only. `SnapshotUpdater` records item price/stock
changes, not shipping. `ProductWatchService`/`BestPriceService` consume the item
reader and remain unchanged. `FeedProductData.delivery_cost` is metadata only;
`FeedStoreProvider` reads already staged generations. User market and delivery
preferences currently have no separate representation.

## Evidence audit (2026-10-04)

No new real-provider endpoint is added. eBay's configured delivery header is not a
user quote; existing fixtures do not prove complete destination shipping/tax.
WooCommerce product endpoints contain no authoritative destination quote.
Awin `delivery_cost` and TradeDoubler `shippingCost` provide an amount without
sufficient destination scope: retain first-class amount, scope UNKNOWN, never total.
CJ's existing audited Shipping shape contains country, price/currency and optional
postal/region/location qualifiers. Only a matching configured country/currency
with no qualifiers is represented as country-scoped shipping. Tax remains UNKNOWN.
The [CJ Shipping specification](https://docs.cj.com/docs/shopping-google-format)
and [publisher schema](https://developers.cj.com/graphql/reference/Product%20Feed)
were checked; upload tax percentages are not guessed monetary tax amounts.

## Chosen architecture

Item price != delivered total; shopping market != delivery destination;
unknown cost != zero; reference FX != landed-cost ranking. Existing watches retain
item-price semantics.

`DeliveryContext` validates country with existing ISO validation, normalizes only
postal whitespace/case, rejects controls and bounds length. No global postal regex.
Nullable user fields hold the user's own destination. Postal codes never enter
comparison DTOs, diagnostics, logs, notifications, clicks or quote rows. Quote
keys are deterministic destination fingerprints, with explicit COUNTRY/EXACT scope.

Strict Decimal ancillary money accepts zero; item Money remains positive. Tax is
INCLUDED / NOT_APPLICABLE / ADDITIONAL / UNKNOWN. A single domain function computes
item + shipping + explicit additional tax, only for compatible native currency.

Static delivery evidence lives on the exact source StoreOffer. Dynamic quotes live
in a separate latest-cache table keyed by offer/destination/scope/currency, bound
to an item snapshot. Both are evaluated through one evidence interface. Quotes
have independent bounded TTL, capped by provider cache policy. No migration infers
scope, tax or availability from legacy placeholders; raw legacy postal values are
converted to fingerprints and removed from listings.

Normal comparison performs no provider quote calls. An authenticated explicit POST
requests a globally bounded set (default 12), using fresh in-stock offers, one
candidate per merchant before additional sources. It uses bounded concurrent calls
and a small operation timeout, rather than adding another job system. Existing
shared user middleware and provider limiting apply. Failed quotes affect delivery
status only. Display-only quote policy uses request-scoped evidence; no persistence
permission is invented. Only mock advertises dynamic quoting in M5B.

Delivered representatives are ranked separately from item representatives in SQL;
summaries span all eligible merchants, offer bodies stay paginated. Every delivered
row/link comes from one underlying StoreOffer. Incomplete or stale evidence cannot
beat a complete current total. Different currencies remain separate groups.
Commission, payout and network labels never enter either ranking.

The domain-calculated total is cached as derived data with its bound item price,
so SQL performs evidence/eligibility selection without a second arithmetic implementation.
Dynamic cache additionally binds the exact source URL. Provider-stage budgets are
12 candidates, concurrency 4, per-call 3s and overall 10s by default. Normal item
refresh ingests static evidence through the same normalized fields; quote-only
acquisition never creates PriceObservation. Expired quote eviction joins the existing
30-second comparison-maintenance job in bounded batches. Real adapters default to
request-scoped results; only mock explicitly opts into cache persistence.
Feed adapter revision is now `m5b-feed-v1`: preserved old validation reports remain
immutable but cannot authorize a changed normalization implementation.

## Acceptance-first record

Eight acceptance cases are authored before implementation: unknown shipping,
299+25 versus 309+0, free shipping, unknown tax, stale quote, wrong destination,
watch preservation on settings change, and different delivered source for one
merchant. Initial execution: **8 failed in 0.57s**, all rejected by the original
ProductOfferData model because delivery evidence did not yet exist. Production
implementation started only after this red run and the full baseline completed.

## Initial M5B local verification

**896 passed, no skips, 91.94% statement coverage**, 314.37 seconds. All 812
previous tests retained; 84 new cases. The sole warning is the existing upstream
ARQ Redis close deprecation. Ruff lint and formatting passed (236 formatted files,
including 199 Python files); strict mypy passed for 125 production source files.
Alembic upgrade/check and the full historical preservation verifier passed on the
final revision `d7a3b951e620`. Markdown file links and whitespace checks passed.

Acceptance covers zero/invalid money and all tax states, exact/country/cross-border
scope, independent quote age, shrinking TTL, static/dynamic whole-source selection,
request-scoped cache policy, safe failure/timeout, user settings/privacy, unchanged
watch/best events/history, real Awin/CJ canonical source identities, commission
neutrality, every locale and authenticated API budgets. Raw catalogs with 100/500/1000
offers assert constant query bounds and the global 12-call limit, without timing thresholds.
Timeout acceptance also checks no SQL transaction remains open during provider waits.

M5A merged-main hosted run also passed on the baseline merge commit
([Actions](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/37225654770)).
M5B final-head hosted run results are verified and linked in the PR description and
delivery report before merge readiness is claimed. No live merchant activation or
real credentials were used; `.env` was not read or changed.

## Delivery precedence review regression — 2026-10-05

The review identified an unconditional dynamic EXACT preference and a timestamp
comparison that could let newer COUNTRY evidence beat current static EXACT evidence.
Evidence selection now compares current non-terminal validity, destination scope,
then quote time. Static wins equal-scope/equal-time ties. Failed/unsupported lookup
results cannot replace current static evidence. All displayed fields still use one
chosen tuple; item, watch, event, tracker, commission and native-currency semantics
retain their prior behavior.

Before the fix, the new matrix produced **8 failures and 16 passes**. Its 24 cases
cover A–H, both terminal failure states, freshness in both directions, and both
equal-time scope ties. Each scenario makes the chosen source cheaper or more expensive
than a competing merchant, checking shipping, tax, total, timestamps, availability,
source URL, delivered winner, unchanged item winner and unchanged observation count.
The targeted new and existing delivery suite passed **55 tests** after the fix.
