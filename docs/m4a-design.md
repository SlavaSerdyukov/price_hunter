# M4A design — international commerce

Recorded before implementation, 2026-09-21. Extend M3B in place; M2 billing,
deterministic matching and native-currency ranking remain authoritative.

## Baseline and branch

- Audited README, ROADMAP, comparison/discovery/verification documentation,
  watch/discovery models and services, catalog resolver, normalized provider DTO,
  provider registry, eBay, affiliate scaffold, settings and Telegram comparisons.
- M3B PR [#2](https://github.com/SlavaSerdyukov/price_hunter/pull/2) merged into main
  as `0d6d49331c738fe992b19c19132b5768654afd4f`.
- [Main CI](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/35608647734)
  passed before implementation. Branch: `feat/m4a-international-commerce`.
- Local baseline: ruff, format, mypy, Alembic upgrade/check and M3B migration
  verifier passed; **399 tests passed, 89.41% coverage**. One existing ARQ/Redis
  deprecation warning. No quality gate is relaxed.

## Market audit and migration

M3B derives discovery country from `coalesce(User.country_code, 'BE')` and watch
uniqueness omits country. Profile changes therefore move discovery interests.
Persist required `ProductWatch.market_country`; uniqueness becomes user/product/
market/currency. Only the migration backfills saved country or legacy BE. New
requests use explicit market, then saved country, otherwise CountryRequiredError.
The bot offers country selection. Profile changes do not rewrite existing watches.

Market means catalog discovery context, not proof of shipping eligibility.
Existing comparison/history groups remain product/native-currency scoped across
known merchants; market labels must not claim all offers deliver there. Prepare
separate optional delivery country/postal code and shipping/tax fields; do not
compute landed cost in M4A.

## Policy and persistence boundary

A central ProviderDataPolicy records review evidence and explicit permissions for
catalog persistence, history, tracking, refresh, affiliate use, attribution and
cache lifetime. Production integrations require an operator-reviewed configuration;
technical API access is not permission. The built-in synthetic mock is the only
automatic policy. Test fixtures explicitly grant synthetic provider permissions.
No real provider's long-term tracking permission is inferred from public API docs.

Persistence checks happen before catalog ingestion; observations and historical
best events are conditional on history permission. Tracking/refresh/discovery
respect permissions and actual capabilities. Display freshness is capped by policy
cache lifetime; expiry hides data prohibited from display. Operational retention
must also remove expired restricted catalog data rather than merely relabel it.
Policies are deployment configuration, not user-supplied API input.

## Links and affiliate identity

Normalized offers carry nullable direct_url, affiliate_url/network/metadata. At
least one validated HTTPS URL is required. The compatibility `url` denotes a
provider locator, not an assertion of a direct merchant URL. Affiliate-only
Rakuten offers keep direct_url null. Shared offers never contain a user-specific
affiliateReferenceId or Telegram identifier.

OutboundLinkService is the only selector for response cards, notifications and
redirects. Approved active affiliate URLs win; otherwise use a safe direct URL.
Affiliate-only offers without approval cannot be opened. Commission fields never
enter matching, native-price order or best-price notification decisions.

Optional redirects use an expiring HMAC token with server-side offer identity,
bounded surface/market context and random reference, never a destination URL.
Resolve store/offer and policy again at click time. Reject inactive/unsupported
stores, unsafe destinations and bad signatures/expiry. Record append-only clicks
with offer/store/network/surface/market/time/reference, no IP, UA or Telegram PII.
Configurable retention deletes old clicks. Notification delivery resolves fresh
outbound URLs so queued messages do not inherit an already expired token.

## eBay EPN

Use optional campaign ID in the official `X-EBAY-C-ENDUSERCTX` header; preserve
returned itemAffiliateWebUrl separately from itemWebUrl. No hand-built affiliate
query parameters. Explicitly configured delivery country AND postal code may
produce contextualLocation; market alone is not an exact shipping quote.
See [Browse API](https://developer.ebay.com/api-docs/buy/api-browse.html) and
[Buy requirements](https://developer.ebay.com/api-docs/buy/buy-requirements.html).
EPN campaign activation and data-use approval are separate gates.

## Rakuten Advertising

Use the documented scope-only form and encoded credentials Bearer header from
the [token guide](https://developers.rakutenadvertising.com/guides/access_tokens).
The guide's prose mentions a password grant but its concrete curl uses scope only;
do not invent publisher passwords. Async token lock, expiry margin, one safe auth
retry, no stored or logged credentials/tokens.

[Product Search](https://developers.rakutenadvertising.com/guides/product_search)
returns partner-advertiser XML. Fixed API host, safe XML parser, bounded bytes,
pages/results/descriptions and per-request shared limiter (including pagination
and retry). Conservative operating rate below documented 100/minute. Use reviewed
market-to-advertiser configuration; do not invent an undocumented network filter.
Merchant identity is source+MID; no automatic cross-network merchant merge.

Only validated UPC supplies strong canonical evidence; SKU/linkid remain merchant
identities. Decimal sale price uses matching currency and positive values. Bad
items are skipped independently. Stock remains UNKNOWN. SEARCH_MODEL represents
exact brand/model phrase search, not a GTIN filter; returned evidence must still
pass CatalogResolver. No documented item refresh endpoint: no REFRESH capability;
bounded search/discovery ingestion may update an identical listing, otherwise its
freshness expires. Full Partnerships API, Awin/CJ feeds and Best Buy stay deferred.
Field source: [reference](https://developers.rakutenadvertising.com/guides/product_search/reference).

## ECB FX

Fetch the official daily XML with the same bounded safe transport/parser. Persist
Decimal EUR-base snapshots with effective date, fetched_at and source. Shared
scheduled job with lease/backoff retains last successful snapshot on failures;
never fetch per user/search. Reject invalid/zero/negative/future rates. Use
quote/base cross rates through EUR and an explicit maximum display age to retain
weekend data without displaying old rates forever.

Comparison DTOs optionally add approximate reference amounts in the user's
preferred currency with source/date/time/rate. Native prices and currency groups
remain unchanged. No billing/settlement use. See
[ECB reference rates](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html).

## Verification and rollout

Use fixtures for Rakuten/EPN/XML/FX plus concurrent token and limiter tests,
market immutability/coexistence, affiliate-neutral ranking/matching/notifications,
signed-redirect abuse cases, retention and policy gates. Upgrade a real M3B-shaped
database and check preservation of all catalog, tracking, history and billing
tables. Keep all seven locale catalogs complete. Run unchanged full quality gates
and coverage >=85%. Docs distinguish implementation, fixtures, live checks and
credential/policy gates. Do not enable integrations or claim live monetization
without the required credentials and approvals.
