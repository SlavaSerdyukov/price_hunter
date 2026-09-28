# Rakuten Advertising Product Search

Implemented with fake-HTTP/XML acceptance tests. **No live Rakuten verification is
claimed.** Activation requires an approved publisher account, API credentials,
partner advertisers and a reviewed data policy. Default: disabled.

Use [Rakuten's developer portal](https://developers.rakutenadvertising.com/) to create
an application and obtain its client ID/secret and publisher account ID. Establish
advertiser partnerships and confirm that Product Search is accessible for that
account. Review caching, catalog storage, tracking/history and channel requirements
for your actual use before setting permissions.

```dotenv
RAKUTEN_ENABLED=false
RAKUTEN_CLIENT_ID=
RAKUTEN_CLIENT_SECRET=
RAKUTEN_ACCOUNT_ID=
RAKUTEN_ADVERTISERS={"BE":["<partner-MID>"],"DE":["<partner-MID>"]}
RAKUTEN_PAGE_SIZE=20
RAKUTEN_MAX_PAGES=2
RAKUTEN_MAX_RESULTS=50
```

MIDs must be numeric; the example placeholders are intentionally not usable values.
Configure `PROVIDER_DATA_POLICIES.rakuten` as explained in
[international-commerce.md](international-commerce.md). Reviewed catalog and
affiliate permissions are required to enable the provider. Set affiliate_required
for this affiliate-only source. Leave history, tracking and refresh false unless
your agreement specifically permits those activities. Never paste secrets into chat
or commit `.env`.

The [documented token request](https://developers.rakutenadvertising.com/guides/access_tokens)
uses POST `https://api.linksynergy.com/token`, a Bearer header containing base64
client-ID:client-secret and a form body `scope=<account-id>`. The guide's prose also
mentions a password grant, but its concrete curl example contains only scope; this
adapter follows that example and does not invent publisher password fields. Tokens
are held in memory, refreshed before expiry, protected by an async lock and retried
once after a 401. They are never persisted in PostgreSQL or logged.

[Product Search](https://developers.rakutenadvertising.com/guides/product_search)
uses the fixed `/productsearch/1.0` API endpoint and XML. The adapter requests reviewed
MIDs using the documented `mid` filter. There is no fabricated network/country API
parameter: `RAKUTEN_ADVERTISERS` is the operator-reviewed market mapping. Results
with an unexpected MID are discarded. The API describes partner-advertiser results;
a full Partnerships API synchronization is deferred. Maintain and review this MID
list when partnerships change; commission eligibility still depends on the account.

Pages are at most 100 items; pages/results are capped across the whole search, in
configured advertiser order. The global page budget can exhaust before later MIDs;
keep each market's advertiser list small and deliberate. The default shared Redis
budget is 20 requests/minute, below the documented 100/minute. Every Product Search
page and auth retry consumes it, including concurrent searches and workers. Preserve
`"rakuten":20` when replacing the `PROVIDER_RATE_LIMITS` JSON. Searches also have a
whole-operation timeout. XML has bounded response bytes/node count, DTD/entities
disabled and bounded textual metadata. Invalid individual items are skipped.

The adapter maps [documented fields](https://developers.rakutenadvertising.com/guides/product_search/reference)
to merchant MID/name, market-scoped SKU or link identity, Decimal retail/sale prices,
validated UPC, category, short description, image and affiliate-only linkurl.
No direct merchant URL, manufacturer MPN or stock status is invented. Store identity
is `rakuten_<MID>`; provider identity remains `rakuten`. The link host is not displayed
as the merchant name. Same merchants from future networks are not auto-merged.

Sale price must be positive and in the retail currency. A lower sale price retains
retail price as original_price. Invalid UPC is retained only as bounded diagnostic
metadata. Shared offers never contain per-user affiliate references.

Capabilities are SEARCH_KEYWORD and SEARCH_MODEL. Autonomous discovery, when
contractually enabled, uses an exact brand/model/MPN phrase and validates returned
UPC evidence through the existing resolver. No SEARCH_GTIN capability is claimed.
There is no documented item-refresh endpoint in this adapter. Existing listing
prices are not reset by repeated search; they expire by freshness/cache policy.
Search-only cache eviction allows a later result to be ingested anew. Do not promise
continuous Rakuten price alerts or confirmed availability from this API.

Before live enablement, check token acceptance, real partner MID results, returned
link hosts, currencies, account/market mapping, rate limits and required attribution.
Fixture tests do not establish account approval, advertiser eligibility or commission
receipt. Awin/CJ remain abstract feed contracts; Best Buy remains disabled.
