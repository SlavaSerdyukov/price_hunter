# Affiliate links and outbound privacy

`ProductOfferData` and StoreOffer separate direct_url from affiliate_url/network and
bounded affiliate_metadata. At least one safe HTTPS destination is required. For
legacy adapters, `url` is a compatibility provider locator; for an affiliate-only
source it may be the supplied deep link. It is never represented as an invented
direct merchant URL. The old affiliate_click_id column is preserved for schema
compatibility, remains unused, and never receives a user-specific reference.

OutboundLinkService selects links for API/Telegram offer cards, comparison cards,
tracker views and notifications. Approved active affiliate integrations use their
returned affiliate URL; otherwise a safe direct link is used. An affiliate-only
offer with inactive permission has no open button. URLs and commissions are not
inputs to price order, canonical matching or best-price notification decisions.
The seven-language `/help` disclosure explains commissions and ranking independence.

## eBay Partner Network

Configure an approved ten-digit `EBAY_EPN_CAMPAIGN_ID`. The adapter sends the official
`X-EBAY-C-ENDUSERCTX` affiliateCampaignId context and stores returned itemAffiliateWebUrl
as network `ebay_epn`. itemWebUrl remains separate. No campaign means existing direct
URL behavior. No custom affiliateReferenceId is sent or stored. Review data and
affiliate permission separately from API access.

Optional EBAY_DELIVERY_COUNTRY and EBAY_DELIVERY_POSTAL_CODE supply contextualLocation
only when both are explicitly configured. This deployment-level context is groundwork,
not a guess at a user's address or an exact shipping quote. It is not inferred from
watch market, Telegram language or IP. See the official
[Browse guide](https://developer.ebay.com/api-docs/buy/api-browse.html).

## Optional signed redirect

Set both PUBLIC_BASE_URL (your HTTPS origin/path) and a random REDIRECT_SIGNING_SECRET
of at least 32 characters. Add the public hostname to ALLOWED_HOSTS and configure
TLS at your reverse proxy. Never commit the signing secret. Default redirect token
lifetime is 86400 seconds; configurable range is 60 seconds to 30 days. With neither
setting, authorized store links are returned directly and no click is recorded.

`GET /r/{opaque-token}` is public but accepts only signed, expiring server-generated
tokens. Tokens encode offer UUID, expiry, a bounded surface/market and random
reference, with HMAC-SHA256. They contain no destination URL or user ID. Signature,
expiry, store supported/active flags, current policy, cache age and destination host
are checked on every click. Query-string destinations are rejected. HTTPS, no
credentials, no IP literals/local hosts and provider-appropriate hosts are mandatory.
Responses are 302 with no-store and no-referrer headers. Unknown/expired/tampered
tokens fail; failed redirects do not create analytics events.

Pending notification delivery resolves the current offer and creates a fresh link.
Rotating the signing secret invalidates old tokens; Telegram users can reopen the
current card after expiry. Repeated valid clicks create separate events; this is
click counting, not unique-user attribution or proof of a completed sale.

## Click records and retention

OutboundClick stores only offer/store IDs, optional network, surface, optional market,
timestamp and an opaque random reference. It stores no IP, User-Agent, Telegram ID,
username, email or account association. No public click analytics endpoint exists.
The worker deletes clicks older than OUTBOUND_CLICK_RETENTION_DAYS (default 30,
range 1–365) in its commerce maintenance schedule. Deleted catalog offers detach
their click foreign key; analytics never require preserving expired product content.

Reverse-proxy/access-log retention is a separate deployment setting. Avoid logging
full redirect paths unnecessarily, and do not add user identifiers to outbound URLs.
Live EPN attribution/commission receipt has not been verified by fixture tests.
