# Feed quality and publication safety — M4E

Rights reviewed, technically validated, activated, successfully published and live
verified describe separate evidence. None implies the next step.

`FeedQualityEvaluator` is shared by inactive validation, sync dry-run and actual
publication. Existing FeedSource adapters and FeedHTTP bounds, host allowlists,
rate limiter, archive checks and timeouts are used in all acquisition paths.
Only complete `full` feeds are supported. Advertiser/feed identity and configured
currency are checked by adapters; explicit Awin country Primary Region mismatches
are rejected. A broad region such as EU is not a country. CJ advertiser domicile
is not catalog market evidence. The operator must review the selected catalog's
market; validation does not invent a market field where the upstream lacks one.

## Defaults and hard gates

| Setting | Default | Meaning |
| --- | --- | --- |
| `MERCHANT_VALIDATION_MAX_AGE_SECONDS` | 604800 | Seven-day activation evidence lifetime |
| `FEED_MIN_VALID_ROWS` | 1 | Minimum distinct accepted external IDs |
| `FEED_MAX_INVALID_RATIO` | 0.25 | Maximum malformed / parsed rows, inclusive |
| `FEED_SHRINK_GUARD_MIN_PREVIOUS_ROWS` | 1000 | Apply shrink comparison at this published size |
| `FEED_MAX_SHRINK_RATIO` | 0.50 | Maximum fraction of previous unique rows lost, inclusive |

Ratios use Decimal arithmetic. A first generation has no shrink baseline but must
pass all structural and minimum/invalid-ratio gates. A 5 → 3 boutique catalog can
publish; a 100000 → 2000 complete candidate is rejected as `quality_shrink`.
A 1000-valid/9000-malformed candidate is `quality_invalid_ratio`. Exact duplicate
rows count as duplicates; conflicting duplicate content is a systemic error.
Duplicates cannot inflate the distinct row count used by minimum/shrink guards.
Setting minimum rows to zero explicitly permits an empty feed; production defaults
reject it. Do not lower safeguards to work around an unexplained source incident.

Unauthorized HTTP, schema/login/HTML/truncation/archive/size/pagination failures,
wrong advertiser/feed/currency, future timestamps and invalid destination hosts
remain hard errors. No override bypasses them. Source versions are opaque and
stored as SHA-256 digests; changed versions still require quality checks. Upgrading
from M4D causes one normal reacquisition of an old plaintext source version.
The existing unchanged-version cache/presence checks remain; stale, missing or
expired current rows force reacquisition.

## Metrics and drift

Reports contain parsed/valid/invalid/duplicate counts, valid ratio, currency counts,
GTIN/MPN/model, known/unknown stock, affiliate/direct links and checked destinations,
images, size/colour/capacity/parent variants, source timestamps, delivery cost and
original price coverage, and streaming price min/max. Coverage describes normalized
input rows, including exact duplicates; published row count is distinct IDs.
All rows are aggregated; `sampled_rows=0` means no row sampling. Quantiles and raw
row samples are deliberately absent. Fashion and electronics can pass with very
different identifier/size coverage.

Generation reports include distinct row delta, valid-ratio delta, per-signal
coverage ratio deltas and currency distribution changes. These are review signals,
not universal vertical thresholds. Rejection categories are a fixed small set;
unknown text maps to `invalid_row`. No URLs, feed payloads or raw remote error text
enter evidence, warnings or audit. Warning codes and metrics have constant bounds.

## Rejected candidates

Publication evaluates quality while holding the existing sync-state/program fence,
before deactivating or upserting the current generation. Rejection preserves current
generation, source-version digest, published report and row count. Materialized
prices, stock, confirmed time, observations and watches are untouched. Their normal
cache/freshness expiry still applies. Only that attempt's pending rows are removed.
Program-local failure count/backoff applies; no network-wide outage is inferred.

`feed-diagnostics PROGRAM_UUID` / `feed-status PROGRAM_UUID` show last immutable
validation/status/age/fingerprint match, current generation/active row count,
published report and `rejected_report` with candidate counts, attempted generation,
timestamp, error and failure kind. `publication_quality_failed`,
`technical_validation_failed` and `network_failed` are separate diagnostics.

## Deliberate one-shot shrink exception

After investigating and independently reviewing a legitimate retailer reduction:

```sh
uv run python -m pricehunter.apps.admin feed-sync PROGRAM_UUID --allow-shrink --reason "Reviewed retailer catalog migration" --confirm
```

This operator CLI requires confirmation and a nonblank 1..500-character reason.
It bypasses only shrink, for this call. Dry-run cannot apply an exception. If the
shrink guard actually fires and publication succeeds, a `FeedPublicationAudit`
stores program/generation, previous/candidate/invalid counts, guard, reason and
timestamp atomically with publication. No audit claims an override on a failed or
unchanged generation. No persistent bypass flag exists; the next sync is guarded.
Both audit and validation history reject database UPDATE/DELETE. Downgrade refuses
to discard evidence until explicitly exported/reconciled by an administrator.
