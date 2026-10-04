# Merchant programs

M4D separates the reviewed network/market `MerchantProgram` and acquisition `Store` from
the canonical customer-visible `Merchant`. Every newly onboarded Store receives its own
distinct Merchant by default. Matching names/domains never reconcile retailers. Once retail
identity has been reviewed, use the separate [canonical merchant commands](merchant-identity.md)
to link sources with current version receipts, dry-run and explicit confirmation.
Each program's rights, activation, feed IDs and provenance remain independent after linking.
Canonical disable hides all its sources without editing any program contract.

Review each network/advertiser/market independently. Account/feed visibility is not a
persistent tracking/history grant. Stable Store identity uses network + advertiser ID;
market-specific programs may share it. Cross-network merchant identities remain separate.

## Onboarding and updates

1. Keep the network disabled. Configure approved account credentials locally, then run
   `merchant-candidates NETWORK` (Awin, TradeDoubler or CJ). Visibility is informational.
2. Generate `merchant-program-template NETWORK ADVERTISER_ID --feed-id FEED_ID` into a
   local JSON file. It has active=false, approved=false and a fail-closed policy. Correct
   the REVIEW_REQUIRED domain and verify market, currency, merchant/feed IDs and name.
   Remote advertiser country is not a grant to sell or track in that market.
3. Import with --dry-run, then --confirm. Generic re-import returns the existing identity
   without overwriting review, approval, policy, active state or feed reference.
4. Write a separate policy JSON based on the actual signed/approved contract. Specify
   reviewed=true and a meaningful non-secret review_reference, permitted catalog/history/
   tracking/refresh/affiliate use, attribution and cache age. Run merchant-program-review
   with current expected version, reason, --dry-run, then --confirm. This deliberate
   operation records review **and approval**; it does not activate the merchant.
5. Activate with current expected version/reason/confirmation. Add the program UUID to
   FEED_PROGRAM_IDS and enable its network only after credentials and approvals are ready.
   Startup requires active reviewed programs for each enabled network.
6. Inspect merchant-program-check and feed-sync --dry-run. Confirm the live sync separately,
   then verify completeness, variants, native prices, real destinations and attribution.
   Record date/evidence; fixtures do not prove live access or merchant permission.

Use your actual identifiers and current version; the following names are placeholders:

```sh
uv run python -m pricehunter.apps.admin merchant-candidates cj
uv run python -m pricehunter.apps.admin merchant-program-template cj ADVERTISER_ID --feed-id FEED_ID > pending-program.json
uv run python -m pricehunter.apps.admin merchant-program-import pending-program.json --dry-run
uv run python -m pricehunter.apps.admin merchant-program-import pending-program.json --confirm
uv run python -m pricehunter.apps.admin merchant-program-review PROGRAM_UUID --expected-version 1 --policy-file reviewed-policy.json --reason "Contract reviewed" --dry-run
uv run python -m pricehunter.apps.admin merchant-program-review PROGRAM_UUID --expected-version 1 --policy-file reviewed-policy.json --reason "Contract reviewed" --confirm
uv run python -m pricehunter.apps.admin merchant-program-activate PROGRAM_UUID --expected-version 2 --reason "Approved pilot" --confirm
uv run python -m pricehunter.apps.admin feed-sync PROGRAM_UUID --dry-run
uv run python -m pricehunter.apps.admin feed-sync PROGRAM_UUID --confirm
uv run python -m pricehunter.apps.admin merchant-program-history PROGRAM_UUID
```

Each successful change increments version and appends an audit entry; stale versions
return version_conflict without mutation. --dry-run checks input/current version without
changing state. Every mutating CLI command displays its planned action and requires
--confirm to apply. Never put credentials in program/policy files or review reasons.

Additional explicit operations:

- merchant-program-policy: --policy-file, --expected-version, --reason, --confirm.
- merchant-program-disable: expected version/reason/confirm; retains Store, offers,
  observations, canonical products, trackers, watches and price events. It revokes
  eligibility, cancels in-flight ownership and invalidates cached staging presence.
- merchant-program-metadata: --display-name plus version/reason/confirmation. The
  source Store name is updated; shared market programs retain their own labels. Established
  canonical customer display is changed separately with merchant-metadata.
- merchant-program-feed-reference: --feed-id plus version/reason/confirmation. A changed
  feed clears approval/policy and staging eligibility and requires another review.

Policy revocation immediately affects comparison, tracking, outbound and refresh through
PolicyResolver; retained history is never deleted. Disabling affiliate use leaves price
ranking unchanged and allows the reviewed direct URL fallback. Tracking revocation stops
future watch selection. Catalog revocation hides offers interactively. Re-activation after
disable does not revive cached listings: a successful complete feed must reconfirm presence.

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
