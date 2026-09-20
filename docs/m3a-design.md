# M3A implementation design

The existing search matcher and persistence disagree for products without GTINs.
Keep the modular monolith and existing exact-offer Tracker; introduce the following
bounded extensions without changing billing policy.

- A deterministic identity domain normalizes identifiers, brand/model and variants.
  Conflicting identifiers or variants reject a match. Titles never authorize a merge.
  ProductIdentifier stores indexed evidence; CatalogResolver owns matching, while the
  repository only writes rows. Transaction advisory locks on shared evidence plus unique
  listing keys serialize concurrent discoveries. Existing product IDs/history remain intact.
- Persist search results first, then assemble comparisons by the actual Product ID.
  Offers display persisted Store name/country. Recommendations and spreads are per
  currency, prefer confirmed stock, and never estimate shipping, tax or FX.
- ProductWatch represents one canonical product in one selected currency. Watches and
  exact-offer trackers share the existing plan quota, oldest-enabled scheduling policy
  and worker. All known eligible offers of a scheduled watch are refreshed.
- Refresh acceptance serializes changes for the same canonical product. A watch stores
  its previous best offer/price, and creates a snapshot event in the existing notification
  outbox when its best changes. No duplicate price-observation stream or delivery worker.
  Outbox uniqueness and transactional watch state make replayed refreshes idempotent.
- Telegram gets comparison cards, bounded offer pages, exact-offer buttons and best-price
  watch management. REST exposes the same comparison and owned watch services. New text
  is translated into all seven languages.

Migration is additive and preserves all existing billing and tracking records. Legacy
catalog identifiers are indexed by a resumable local backfill command before enabling
comparison in an existing deployment; ambiguous old products are not silently merged.
Acceptance tests, migration checks and >=85% coverage precede marking M3A complete.
