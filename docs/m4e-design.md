# M4E design and verification

Branch `feat/m4e-merchant-pilot-gate` starts at merged main
`ce38e62b13633154737e5ae60dc936d9bc5590f7` (M4D PR #6).
M4D final-head hosted run 37202535512 passed: 699 tests, 90.70% coverage.

Baseline lint, formatting, strict mypy, upgrade and schema comparison pass.
Migration verifier passed. Full baseline: **699 passed, no skips, 90.68% local
statement coverage** (207.49s), one existing upstream ARQ close deprecation warning.
Merged-main hosted run [37211169576](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/37211169576)
also completed successfully before M4E implementation.

Rights review remains a separate explicit operation. Immutable technical reports
describe the exact network/advertiser/feed/market/currency/mode/language/domain
configuration. Activation checks fresh, matching passed evidence while holding the
existing sync-state/program locks. Canonical Merchant cosmetics do not affect it.

One streaming quality evaluator supplies validation, dry-run and publication.
Validation uses a transaction advisory lock and disk-backed deduplication; it
does not write catalog, staging, observations or notifications. Publication checks
unique valid rows, malformed ratio and large-catalog shrink before generation
replacement. Rejection preserves the published state and cleans its own pending
attempt. A confirmed shrink exception is a call-scoped argument with an immutable
audit written atomically with publication.

Pending watch delivery checks snapshot Merchant UUID against the current source
assignment. Same-UUID renames and legacy snapshots retain compatibility.

Six acceptance cases ran red before production changes (6 failed, 4.40s): missing/stale
validation, 100k to 2k shrink, 90% malformed rows, one-shot override, and notification
identity reassignment. Existing empty-feed regression scenarios explicitly configure
minimum valid rows to zero; production defaults protect empty catalogs.

The six cases then passed (5.90s). Feed/M4D regressions passed (73 cases); expanded
pilot/evaluator/adapter acceptance passed (84 cases, 15.13s). The complete final
M0–M4E suite passed: **752 tests, no skips, 90.92% local statement coverage**
(237.32s), with only the existing upstream ARQ warning. This adds 53 cases
(37 integration, 16 unit) to the preserved 699-case baseline. Ruff lint/format
(215 Python files), strict mypy (119 source files), Alembic upgrade/check,
scratch migration verifier, Markdown link targets and diff whitespace checks pass.
Hosted final-head checks are required separately and reported with delivery.

`MerchantProgramValidation` is append-only at the PostgreSQL trigger layer and
contains status, technical fingerprint, source-version digest, bounded counters,
warnings/metrics, safe error, adapter revision and timestamps. Latest matching
result wins; failure cannot fall back to an older pass. No rights or approval are
inferred. Expiry defaults to seven days. Source version need not match at activation.

The shared evaluator uses constant-size counters and price extrema. Identity
deduplication is disk-backed for validation/dry-run and batched PostgreSQL staging
for publication. The 100k validation test limits traced Python allocation peak to
32 MiB, with a 1 MiB SQLite page cache, without elapsed-time assertions. No product
or price list is retained; bounded report serialization stays below 8 KiB.

Migration `a54ef912b603` adds validation/override evidence and latest rejected report
state. The extended verifier snapshots old M4D commerce/billing/identity/audit/FX
data, checks unchanged rows, empty new evidence tables, append-only guards and an
evidence-preserving downgrade refusal, then proves scratch export/downgrade/re-upgrade.
No old migration or live application database is modified.

Defaults: minimum one distinct valid row; malformed ceiling 25%; shrink guard from
1000 previous rows with maximum 50% loss. Soft coverage is vertical-neutral. A
shrink exception requires reason/confirmation, bypasses only shrink, and writes
`FeedPublicationAudit` atomically only when used by a successful publication.

First-pilot procedure: [merchant-pilot.md](merchant-pilot.md). Guard and diagnostic
semantics: [feed-quality.md](feed-quality.md). All account configuration, rights,
real activation and live verification remain explicit operator work.
