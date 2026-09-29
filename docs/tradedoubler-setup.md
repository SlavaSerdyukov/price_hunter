# TradeDoubler feed adapter

## Official source audit — 2026-09-29

Source: [official publisher Products API](https://dev.tradedoubler.com/products/publisher/).
Authentication uses the account's PRODUCTS token as a query parameter. PriceHunter
uses HTTPS on `api.tradedoubler.com`; tokens never enter database rows or diagnostics.

Documented services: `/1.0/productFeeds.json` for feed discovery,
`/1.0/productsUnlimited.json;fid={id};page={page};pageSize={size}` for complete feeds,
and `/1.0/productsUnlimited/lastUpdated.json;fid={id}` for version checks. Both page
and pageSize are required when paginating. The ordinary search service has a 1,000
product cap, so it cannot establish a complete generation.

Unlimited downloads are limited to three per unchanged version in 24 hours, with
documented initial grace. HTTP 429 fails the attempt and schedules backoff. Pagination
quota accounting and allowed download cadence require account verification. Feed versions
are checked before and after pagination; a changed version prevents publication.

Products contain nested offers, identifiers, image data and custom fields. `productUrl`
is the supplied tracking link; `sourceProductId` identifies the merchant listing. Modified
timestamps may be epoch milliseconds or ISO format. No historical API prices are imported
as PriceHunter observations; unsupported sale semantics remain unset.

## Configuration

Default `TRADEDOUBLER_ENABLED=false`. Set `TRADEDOUBLER_TOKEN` and explicit
`FEED_PROGRAM_IDS`, then review each merchant's program, feed, market, currency, link
domains and data rights. Program-check verifies feed membership; dry-run validates rows.
Fixtures are synthetic representations of the documented response, not live verification.
No item refresh endpoint is claimed. Feed snapshots update materialized listings through
the existing SnapshotUpdater. Naive version strings are retained as opaque feed versions;
item timestamps need a timezone or numeric epoch to establish chronological ordering.
