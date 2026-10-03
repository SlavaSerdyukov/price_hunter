# CJ publisher product-feed integration

**Status:** adapter implemented and fake-HTTP fixture tested. Disabled by default.
No CJ account, advertiser approval, contractual policy review or live feed verification
is claimed. Credentials prove technical access, not permission to persist or track data.

## Official audit — 2026-10-03

The adapter was implemented after inspecting these primary sources:

| Topic | Official source | Implemented interpretation |
| --- | --- | --- |
| Authentication | [API Authentication](https://docs.cj.com/docs/api-authentication) | Personal Access Token in the Bearer authorization header |
| Endpoint/access limitations | [Product Feed API](https://docs.cj.com/docs/product-feed-api) | POST `https://ads.api.cj.com/query`; publisher access excludes private feeds |
| Publisher queries, company/advertiser IDs, PID, pagination | [Product Feed Search](https://docs.cj.com/docs/productfeed-api-query) | `companyId` is the publisher company; `partnerIds` are advertiser company IDs; `adIds` select feed IDs; `linkCode(pid: ...)` requires the website PID |
| Arguments | [Query Reference](https://docs.cj.com/docs/query-parameters) | `products` accepts `page`; shopping feed summary accepts limit/offset. Advertiser country is a discovery hint, not delivery authorization |
| Returned types and fields | [Developer Portal Product Feed reference](https://developers.cj.com/graphql/reference/Product%20Feed) and its [public introspection schema](https://production-doc-pipeline-assets-store-assetsstore-1q89u4n9t6xlw.s3.amazonaws.com/docs/api/graphql/public/Product%20Feed.json) | Exact Product/Shopping, Products, ProductFeed, money, LinkCode and Shipping fields, including nullable fields |
| Streaming capability | [Subscription guide](https://docs.cj.com/docs/productfeed-api-subscription) and [subscription reference](https://docs.cj.com/docs/subscription-parameters) | Not implemented: bounded products pagination has a verified schema; no guessed streaming framing |
| Advertiser upload format | [Shopping Google Format](https://docs.cj.com/docs/shopping-google-format) | Upload names are not assumed to be publisher GraphQL output names |

The public schema is the JSON asset loaded by the official Developer Portal's published
JavaScript. Original asset SHA-256:
`b82502490627c863331ae408c86026eebcba0af8b5fb52f914a85ebbfb41a360`.
[The fixture projection](../tests/fixtures/cj/schema.json) records provenance and exact
selected type/argument declarations. The contract test checks every fixed selection,
including fragments and nested money/link/shipping fields; it never fetches documentation
or contacts CJ in CI. Product fixtures are synthetic schema-based examples, not scraped
merchant data or samples of a verified publisher account.

## Normalization and limitations

`advertiserId` maps to MerchantProgram.external_merchant_id. `adId` selects the feed;
Store identity remains `cj-ADVERTISER_ID`, with market-specific programs sharing that
Store. The reviewed merchant name is customer-visible. Different networks retain
separate Stores and listings even if their domain or GTIN agrees.

Product `id`, title, description, brand, MPN and GTIN are normalized. The schema has no
separate model, UPC or EAN output field; GTIN validation accepts supported UPC/EAN lengths
with their check digits. Price and active sale price use Decimal `amount` and `currency`;
original price is retained only for a lower, currently effective sale. Missing stock is
UNKNOWN. Product `link` is the optional direct URL; missing it permits an affiliate-only
listing when reviewed outbound policy allows that. `imageLink` is optional.

Shopping `itemGroupId` is parent metadata. Size, color, material, pattern, sizeType and
sizeSystem stay variant evidence. `productType` provides category metadata. Shipping cost
is retained only for a matching reviewed country/currency without postal/region/location
qualifiers. No delivered-price or tax ranking is introduced. `targetCountry` may reject a
wrong-market row; `serviceableAreas` and advertiser domicile never grant a new market.

Only the documented example's `kqzyfj.com` / `www.kqzyfj.com` affiliate hosts are allowed.
Other CJ tracking domains need a separate official audit and explicit allowlist change;
the adapter does not trust an arbitrary returned host. Direct URLs must match the
reviewed merchant domain. Finance/travel product types are rejected individually.

Feed summary `lastUpdated` is used as a source version where present; product `lastUpdated`
is the content timestamp. Neither substitutes for successful full-generation presence
confirmation. A changing summary version, unknown merchant/feed, unresolved GraphQL error,
truncation or transport error prevents generation publication. Scoped malformed product
nodes are rejected while valid nodes remain usable; raw remote error messages are discarded.

## Bounds and pacing

CJ documents a default 1,000 and maximum 10,000 products per query, one-based offset
pagination for the first range, and `nextPage` → `page` continuation beyond that range.
The adapter uses the common Product interface with a Shopping fragment because the audited
Products wrapper exposes `nextPage`; it omits offset when following a cursor. A missing
cursor after 10,000 consumed rows fails closed. Count/total consistency and repeated
cursors are checked before the existing generation completion transaction.

Application limits are intentionally stricter: page size 500 by default, maximum 1,000;
2,000 pages and 1,000,000 rows maximum by default; 4 MB JSON page, 8,192-byte fixed query,
4,096-character cursor, JSON depth/field limits, bounded compressed/decompressed streams,
and at most 5,000 discovered feed references. All are enforced, not remote guarantees.

No numeric Product Feed request quota was found in the audited documentation. The local
shared `feed-http:cj` budget defaults to 60 requests/minute, configurable through
`PROVIDER_RATE_LIMITS`; this is application pacing, **not a claimed CJ allowance**.
Set it lower if your account agreement requires that. All advertiser requests/pages share
one Redis budget. HTTP 429 is a failed attempt followed by the existing feed backoff,
not an unlimited immediate remote retry.

## Operator configuration

Keep secrets in local environment configuration; never paste PATs into program JSON,
review references or logs. Configure these values yourself when access is approved:

```dotenv
CJ_ENABLED=false
CJ_API_TOKEN=
CJ_COMPANY_ID=
CJ_WEBSITE_ID=
```

CJ_WEBSITE_ID is the official PID for the registered publisher website, not the company
ID or advertiser ID. Enabling CJ with incomplete PAT/company/PID fails immediately.
Discovery may be run while CJ remains disabled, with complete credentials:

```sh
uv run python -m pricehunter.apps.admin merchant-candidates cj
uv run python -m pricehunter.apps.admin merchant-program-template cj ADVERTISER_ID --feed-id FEED_ID > pending-cj.json
```

Inspect/edit domain, country, currency, feed ID and name deliberately. Advertiser country
in the template is only a hint. Follow [merchant onboarding](merchant-programs.md): create
pending, explicitly review/approve rights, activate, allowlist the program UUID, enable
CJ, inspect dry-run and explicitly confirm live sync. A fail-closed template cannot be
imported until its `REVIEW_REQUIRED` domain and missing fields are corrected. No program
is approved by discovery, credentials or template generation.
