# M4C — coverage engine, CJ and merchant onboarding

Design before implementation, 2026-10-03. Start: merged M4B/M4B.1 main
`4de62c5e9ce033f7ec375294498f133aa4cb9751`, branch `feat/m4c-coverage-engine`.
Baseline: **583 passed, 90.51% coverage**. Ruff lint/format, strict mypy, Alembic
upgrade/check and the complete disposable migration verifier passed. Tests use dedicated
PostgreSQL/Redis and fake remote transports; no publisher credentials are needed.

## Existing boundaries inspected

MerchantProgram, MerchantProgramService, PolicyResolver, FeedSource, FeedStoreProvider,
FeedSyncService, SearchService, ProviderRegistry, CatalogResolver, SnapshotUpdater,
Awin/TradeDoubler/Rakuten adapters and the operator CLI were inspected. CJ will use the
same acquisition/staging/materialization pipeline and stable network/advertiser Store
identity. Advertiser country is not a delivery-market grant. Wire formats end at the adapter.

## Merchant lifecycle

Imports create pending programs with a fail-closed policy. Re-import of an existing
identity cannot change reviewed state or configuration; operators use explicit commands.
Review and approval are a deliberate recorded operation, followed by activation.
Metadata, feed reference, policy and disable operations require an expected integer
version and reason under a row lock. Stale writes fail. Feed-reference changes invalidate
approval and presence so the old generation cannot authorize data from the new feed.
Append-only audit rows store field names and versions, not raw payloads or credentials;
database triggers reject UPDATE/DELETE. Disable preserves catalog and historical records.

## Search outcomes and bounds

ProviderSearchOutcome distinguishes SUCCESS, EMPTY, PARTIAL and NETWORK_FAILED.
Each listing persists in its own transaction. Rejections produce safe diagnostic codes,
not user-visible outages. Only failed provider requests are unavailable. Candidate,
global persistence and comparison caps bound work independently of entitlement output.
Deterministic listing-identity deduplication includes provider/store/market/variant.
Redis search health uses bounded expiring aggregate state without user/query data;
feed health is derived from existing sync states.

## CJ acquisition

Audit current official authentication, query/reference and publisher schema before
implementing requests. Use fixed bounded GraphQL selections, per-feed advertiser filters,
Decimal money and a shared network limiter. Require PAT, company ID and link PID when
enabled. Check pagination completeness before publishing a generation. Individual invalid
nodes may be rejected; unresolved request/merchant/pagination errors must prevent
publication and preserve the previous generation. Defaults keep CJ disabled.

## Coverage and fairness

Operator-only reports aggregate markets/networks/programs/staging/materialized products,
freshness and current tracking/history permissions. Product diagnostics reuse bounded
provider retrieval, never grant policy or merge weak candidates. Possible duplicate
merchants use normalized domain/name for diagnostics only. Due feed claims use stable
ordering, leases and backoff; twenty-program acceptance checks ensure failed merchants
do not starve others. Network pacing is shared across all program requests.

## Acceptance and verification

Start with stale-operator-write, partial-result, four-network same-GTIN and twenty-program
fairness tests. Extend with lifecycle/audit immutability, policy transitions, CJ fake-HTTP
schema/error/bound tests, bounded search/dedup and coverage diagnostics. Add a new
migration without rewriting M4B revisions; verify existing IDs/history and guarded
downgrades. Keep all CI gates and >=85% coverage, and record hosted success only after
the final commit passes. Real activation, delivery/tax ranking and frontend are later work.
