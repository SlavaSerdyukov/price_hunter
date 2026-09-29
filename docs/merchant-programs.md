# Merchant programs

Review each network/advertiser/market independently. Account/feed visibility is not a
persistent tracking/history grant. Stable Store identity uses network + advertiser ID;
market-specific programs may share it. Cross-network merchant identities remain separate.

## Onboarding

1. Confirm network membership, advertiser approval, feed ID, market, currency, merchant
   domain, affiliate hosts, attribution and actual data rights.
2. Import a reviewed JSON program through the local admin CLI. Keep secrets in `.env`,
   never in program data, feed references or review references.
3. Add returned program UUIDs to FEED_PROGRAM_IDS, configure the network secret and enable
   the network. Startup requires active approved programs for each enabled network.
4. Run merchant-program-check and feed-sync --dry-run. Inspect real identifiers, variants,
   prices and links, then sync. Verify attribution separately from price parsing.
5. Record live verification with evidence/date; do not infer it from fixtures.

This disabled example is illustrative, not a real advertiser:

```json
{
  "network": "awin",
  "external_merchant_id": "12345",
  "external_feed_id": "67890",
  "market_country": "BE",
  "display_name": "Approved merchant name",
  "domain": "example.com",
  "currency": "EUR",
  "feed_language": "en",
  "active": false,
  "approved": false,
  "policy": {
    "reviewed": false,
    "review_reference": "",
    "catalog_persistence_allowed": false,
    "price_history_allowed": false,
    "tracking_allowed": false,
    "refresh_allowed": false,
    "affiliate_allowed": false,
    "affiliate_required": false,
    "max_cache_seconds": 3600,
    "display_attribution_required": null
  }
}
```

```sh
uv run python -m pricehunter.apps.admin merchant-program-import reviewed-program.json
```

Tracking requires history rights because watch baselines retain prices. Catalog-only
merchants may appear interactively without observations or watch recommendations. Feeds
use snapshot capability, not an invented item refresh API. Review cadence against both
cache limits and network download quotas. A cache-age increase requires policy approval.

## Onboarding matrix

| Target | Status | Next requirement |
| --- | --- | --- |
| Synthetic Awin programs | Fixture tested | Not real merchants |
| Synthetic TradeDoubler programs | Fixture tested | Not real merchants |
| Coolblue | Not configured | Confirm network/program, approval, feed and rights |
| MediaMarkt | Not configured | Confirm network/program, approval, feed and rights |
| Samsung | Not configured | Confirm network/program, approval, feed and rights |
| Decathlon | Not configured | Confirm network/program, approval, feed and rights |
| adidas | Not configured | Confirm network/program, approval, feed and rights |
| ABOUT YOU | Not configured | Confirm network/program, approval, feed and rights |
| Fnac | Not configured | Confirm network/program, approval, feed and rights |
| Conrad | Not configured | Confirm network/program, approval, feed and rights |
| Zalando Lounge | Not configured | Confirm network/program, approval, feed and rights |

Names are prospective targets, not claims of network membership or live support. Record
future transitions as **approval pending**, **credential configured**, **live verified**.
No real account is required to run fixture tests.
