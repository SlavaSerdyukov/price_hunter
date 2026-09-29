# M4A.1 — market isolation and search snapshots

Pre-implementation audit, 2026-09-29, branch `feat/m4a-international-commerce`,
baseline commit `6297346194226e628c92cf668d1e2a2c78cc9791`.
The unchanged full suite passed: **497 tests, 89.86% coverage**. Added regressions
before implementation for BE comparison selecting DE's cheaper price, and repeat
Rakuten search retaining its original price/link. Both failures must disappear.

## Market boundary

Canonical Product remains global. Store represents a merchant; its country remains
legacy/primary integration context. Required StoreOffer.market_country comes from
ProductOfferData.country. Listing uniqueness includes merchant, external ID and market;
the product/market/currency index supports comparison and refresh demand.
Migration uses valid metadata market, otherwise Store.country, without changing IDs.

Authenticated comparison, offers, history, refresh and search resolve explicit country,
then saved country, otherwise CountryRequiredError. ComparisonReader may remain
unscoped for internal diagnostics only. User comparisons, watch baselines/evaluation,
worker demand and manual refresh filter offer market before currency/FX handling.
Evaluation shares summaries across watchers per market, with separate catalog,
tracking and history permissions. Every originating callback/link retains its market.

Best-state/event series become product/market/currency. Preserve all old global events
with NULL market: even a winning offer's country does not prove the historical
comparison excluded other markets. Preserve old derived state IDs as inactive NULL-
market rows; scoped states do not reuse their sequences. Mark existing watch baselines
for silent rebuild, preserving watch IDs/settings and suppressing migration alerts.
Bounded maintenance initializes missing watched series and handles only due markets.

## Snapshot boundary

Add explicit SNAPSHOT_REFRESH capability and SEARCH_SNAPSHOT ingestion mode.
Ordinary discovery/REFRESH-capable search keeps worker-owned listing prices intact.
Rakuten declares snapshot capability without inventing an item refresh endpoint.
CatalogResolver locks and checks stable listing/canonical identity; a separate snapshot
updater changes only mutable fields, never product/store/external ID or identity evidence.

Product-before-offer locking serializes acceptance. Optional real source_updated_at
rejects stale/replayed versions; absent source timestamps use acceptance time under
the lock. Identical snapshot replay can renew freshness, but cannot duplicate history
or grow aggregates. Search-only policy keeps only current-value aggregates and creates
no observations, best history or tracked notifications. Authorized future feeds may
record changed price/availability snapshots with the existing history policy. No
provider-name special cases or weakened policy, matching, affiliate, SSRF or FX gates.

## Verification

Add acceptance for BE 329/340 versus DE 299/315; DE 289 affects only DE, then BE 300
affects only BE. Exercise shared-merchant markets, BE→DE→BE search, scheduling/manual
refresh, history, corrupt-delivery cancellation, callback market persistence, shared
100-watcher evaluation, policy-specific surfaces, repeat/concurrent/stale snapshots
and unchanged item-refresh authority. Extend the scratch migration verifier through
both M3B and M4A, including ambiguous history and rebuilt scoped state. Run every
existing CI gate with coverage >=85%; do not deploy or enable new integrations.

Completed: both initial regressions now pass, together with the full **513-test**
suite at **90.08% coverage** and all required quality/migration gates. The final
acceptance, migration evidence, limitations and merge assessment are recorded in
[verification.md](verification.md#m4a1-market-correctness-and-snapshot-ingestion--2026-09-29).
