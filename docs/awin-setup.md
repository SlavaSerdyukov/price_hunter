# Awin feed adapter

## Official source audit — 2026-09-29

This implementation targets publisher **legacy CSV feeds**, not Enhanced JSONL.
The official [feed-list documentation](https://help.awin.com/developers/docs/product-feed-list-download)
documents `https://productdata.awin.com/datafeed/list/apikey/{key}` and per-feed
downloads on `datafeed.api.productserve.com`, with numeric feed IDs, requested columns,
CSV and gzip. The feed API key differs from a Partner API token. Returned URLs contain
credentials: PriceHunter rebuilds approved requests and never persists those URLs.

The [Create-a-Feed guide](https://help.awin.com/developers/docs/downloading-feeds-using-create-a-feed)
documents UTF-8 CSV with quoted values. [Column definitions](https://help.awin.com/developers/docs/hosting-feeds)
cover merchant product ID, prices, identifiers, links, stock and optional fashion
size/material/pattern/color. Missing optional data stays missing; timestamps without
an explicit timezone are not invented as UTC source versions.

[Publisher guidance](https://help.awin.com/developers/docs/product-feed-publisher-guide-intro)
recommends checking update information and retrying failed downloads later. It does not
state a numeric request quota for this legacy endpoint. Our shared conservative limiter
is an application limit, not a claimed Awin allowance.

## Configuration

Default `AWIN_ENABLED=false`. Configure `AWIN_FEED_API_KEY`, reviewed MerchantProgram
rows and explicit `FEED_PROGRAM_IDS` before enabling. Program rows contain feed IDs and
language only; no raw download URL or secret. Run program-check and feed-sync --dry-run
first. Approval, allowed cache duration, tracking/history rights, affiliate domains and
attribution must be verified separately for each advertiser. Synthetic fixture success
does not establish a live account, merchant approval or commission eligibility.
