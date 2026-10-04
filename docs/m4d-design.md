# M4D — Canonical merchant identity

Baseline: merged main `3fa77a686e71acddee53098a33e47d2be21c128a` (PR #5, M4C.1),
inspected on 2026-10-04 before creating `feat/m4d-canonical-merchants`.
Local baseline: **655 passed, no skips, 90.48% statement coverage**, 151.12s.
Ruff lint/format (198 Python files), strict mypy (114 source files), Alembic upgrade/check
and the complete disposable migration verifier passed. The only warning is the existing
upstream ARQ Redis close() deprecation. The verified previous hosted measurement is 90.49%.
All runs use a dedicated test PostgreSQL database and Redis DB 15; no live merchant API.

## Existing boundaries inspected

Store and MerchantProgram identify acquisition sources and reviewed network/market grants.
StoreOffer and PriceObservation retain exact source snapshots. ComparisonReader currently
counts/paginates raw StoreOffers with three bounded summary queries. ProductWatchService
and BestPriceService currently interpret a changed offer ID as a merchant transition.
NotificationService rechecks current source permissions and signs that source's outbound
link. OutboundClick records source attribution. CoverageDiagnostics reports network-specific
raw acquisition counts. CatalogResolver/ProductMatcher own product identity independently.

## Smallest safe extension

Add Merchant, a mandatory Store.merchant_id, versioned append-only MerchantAudit and
canonical merchant snapshots on watch/best/history/click state. Initially every Store gets
its own Merchant: no domain/name/provider reconciliation. Source creation has the same 1:1
default, including ORM, Core SQL and program onboarding. Merchant slugs are stable UUID-based
source identities; operator-created slugs are explicit and immutable.

Explicit local operator commands preview and confirm linking, check both endpoint versions
and the current source assignment, and audit both sides. Unlink creates a new identity;
there is no destructive row merge. Disabling is reversible and does not change contracts.

A shared SQL row_number selection partitions by Product, Merchant, market and native
currency **after** surface policy eligibility. It prefers fresh, then stale/failed,
availability, native price, confidence, observation time and stable source/offer IDs.
Commission, network preference and affiliate-link availability do not enter selection.
All comparison summaries and pages use effective rows; raw records remain source-specific.
Materializing the representative rank CTE prevents repeated whole-window execution under
underestimated PostgreSQL statistics. Load tests check WindowAgg Actual Loops = 1 as well
as query count. Equal-priced different retailers use canonical display/name UUID ties.
The representative's own URL/policy accompanies its price. The reader retains bounded query
count and bounded returned rows. Exact Trackers remain tied to their original StoreOffer.

Watch and history state keep both canonical Merchant and exact offer IDs. An identical-price
source switch updates provenance without a fake event; a same-merchant price change uses
price-change semantics. Real merchant changes retain existing idempotent alert semantics.
Operator reconciliation rebases current identity references without rewriting past snapshots.
It locks affected Products before source/merchant identities, sharing the evaluation boundary
so an in-flight watch/history transaction cannot overwrite rebased pointers.

Acceptance tests are written before implementation, including linked Awin/CJ acquisition,
freshness, source versus real merchant switching, independent source policies and
100/500/1000 raw offers with query-count and pagination assertions.
The baseline acceptance run produced seven failures and one positive-control pass before
implementation. The original 100/500/1000 pagination regression now uses distinct default
Merchants to retain its existing visible-row assertions; new duplicate-source fixtures map
five raw sources to each Merchant. No assertion or coverage threshold was weakened.
Existing eBay discovery expectations now count one effective marketplace row while checking
two retained source listings and ordinary price-drop semantics. The click privacy contract
adds only canonical Merchant attribution and verifies its known source relationship.
