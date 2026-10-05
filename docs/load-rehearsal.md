# Authenticated beta load rehearsal

The typed Python harness uses the normal API over real TCP HTTP on an ephemeral loopback
port with normal lifespan, bearer authentication, shared Redis budgets and PostgreSQL
pool configuration. It requires no real account, provider or billing credential.
CI also exercises the same HTTP API through ASGI transport for focused acceptance cases.

| Profile | Concurrency | Requests | Active synthetic users |
| --- | ---: | ---: | ---: |
| smoke | 4 | 56 | 4 |
| beta-small | 8 | 280 | 16 |
| beta-medium | 24 | 1400 | 30 |

Profiles specify work, not production capacity. Thirty-two synthetic users include Free,
Pro, Power and a refunded subscriber; ordinary workloads use currently paid accounts.
Each profile cycles comparison, offer pages, search, settings, subscription, history and
persisted delivery comparison. Default user budgets remain unchanged: 429 with the
existing `rate_limit` code is expected and counted separately. Other 4xx, 5xx, malformed
responses and transport/timeouts are unexpected failures. Route labels contain no IDs.

Provide a **migrated empty** loopback `_test` database through `REHEARSAL_DATABASE_URL`
and dedicated loopback `REHEARSAL_REDIS_URL` with Redis DB **14**. The harness resets that
dedicated Redis database; never point it at shared/production Redis. It rejects remote
hosts, production DB names, and non-mock/nonlocal providers. It does not read `.env`.

```sh
uv run python scripts/load_beta.py --profile smoke --report load-report.json
# Fresh independent empty test database for each invocation:
uv run python scripts/load_beta.py --profile beta-medium --report load-report.json
```

Reports contain total/completed requests, successes, expected 429, unexpected failures,
seconds, throughput, p50/p95/p99/max, per-logical-route status counts/latencies, bounded
response result, integrity status and checked-out connections. Status `0` means a local
transport/deadline/response failure. No API keys, raw IDs, URL paths, postcodes or payloads
are included. These are synthetic observed timings, not production p95/RPO/RTO promises.

Observed local beta-medium run on 2026-10-05: 1400 completed requests, 300 successful,
1100 expected 429, zero unexpected failures and zero checked-out connections after work.
Default minute budgets deliberately dominate this profile; total throughput includes 429
and must not be presented as successful-request production capacity. Recovery observed
148313 bytes, 0.291 seconds backup and 0.545 seconds restore/verification. See the
[sanitized machine-readable evidence](m5c-rehearsal-evidence.json).

Normal CI gates completion, zero unexpected failures, response caps, no leaked checked-out
connections and post-load DB integrity. It does not gate noisy wall-clock latency targets.
Integrity shares restore's exact schema, reviewed inventory, triggers/indexes/constraints,
FK/check/unique validation and nonnegative-counter checks. Financial/outbox/observation/
best-history payload fingerprints must remain unchanged under the ordinary read/search mix.

Additional acceptances hold a one-connection/no-overflow pool deliberately: concurrent
requests receive bounded safe 503, then recover to 200 after release with zero leaks.
Production pool defaults are unchanged. Concurrent HTTP reads plus watch evaluation and
refresh claims verify consistency. A 500-extra-offer fixture verifies bounded comparison
responses; concurrent search verifies provider candidate, persistence, comparison shortlist
and entitlement caps. One abusive user gets 429 while other accounts continue, and Redis
budget keys retain TTL. Persisted delivery reads make no remote quote calls; an independent
mock explicit quote test enforces the global quote cap.

Ordinary `Checks` runs only bounded smoke and failure acceptance. The separate
`Synthetic recovery and load` workflow is `workflow_dispatch` only: fresh Postgres17 /
Redis7 services, full synthetic restore/replay/crash rehearsal, and beta-medium HTTP on a
separate source database. After the workflow is merged to the default branch, select
the reviewed ref and run it manually. Its artifact allowlist contains only
`recovery-report.json` and `load-report.json`, with seven-day retention; dumps are never
uploaded. Runner/database/container teardown owns synthetic database cleanup.

Before public beta, repeat against the chosen deployment's realistic data size, replica/
pool totals, TLS routing and resource limits; review observed load/backlog and safe-error
rates. Hosting/TLS, production backup encryption/transport/retention/cadence, live Stars
acceptance and a real approved merchant pilot remain open.
