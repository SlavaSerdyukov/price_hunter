# M5C: recovery and load rehearsal

Baseline: merged main `0447353d87f8f6a44d0e3652e508c0efabb0225e`.
All baseline gates passed locally: Ruff, formatting, strict mypy, migration upgrade/check,
M3B verifier, 920 tests, 91.95% statement coverage (393.12 seconds). Hosted baseline:
[Checks](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/37354932864), 91.96%.
Platform rounding does not change the 85% gate.

This branch adds operator primitives, synthetic fixtures and acceptance checks. It does
not change commerce matching, ranking, item/delivered winner rules, watches or billing.

## Reviewed durability inventory

The executable inventory is `operations/durability.py`. Both mapped and deployed table
inventories must match it; introducing a table without classification fails verification.
All classes are included in the archive and both identity and full-row digest verification.

| Class | Tables | Consequences of loss |
| --- | --- | --- |
| A: authoritative | users, api_keys | Account/access identities and preferences must survive. |
| A | billing_products, checkout_intents, subscriptions, payment_events, subscription_periods, billing_updates, billing_operations | Rights, immutable contracts, refunds, intake and uncertain external-operation identities cannot be rebuilt safely. |
| A | merchants, stores, merchant_audits, merchant_programs, merchant_program_audits | Reviewed commercial identity, permissions and audit history must survive. |
| A | products, product_identifiers, store_offers, price_observations | Catalog identity, source provenance and historical observations cannot be replaced by a fresh scrape. |
| A | trackers, product_watches, product_best_states, best_price_events, notification_events | Baselines, sequences, history, deduplication and pending/sent/uncertain delivery state must survive together. |
| A | product_discoveries, feed_sync_states | Job identities, generation pointers, counters and leases remain durable. Losing them changes replay behavior. |
| A | merchant_program_validations, feed_publication_audits | Immutable pilot and exception evidence must survive. |
| A | outbound_clicks | Attribution/audit records have no reliable external reconstruction. |
| A | alembic_version | Exact packaged schema compatibility, checked before any upgrade. |
| B: durable, reconstructable | merchant_feed_items | Reviewed-source reacquisition may recover staging, but generation/freshness and availability can change. No automatic rebuild during recovery. |
| B | feed_pending_items | Incomplete candidate staging can be discarded only through existing expired-attempt cleanup; restarting acquisition has an explicit cost. |
| B | fx_rates | Reference rates can be reacquired; old conversion context and freshness would be lost. |
| B | delivery_quotes | Can be reacquired only through permitted explicit quote requests; freshness, destination evidence and delivered results may change. |
| C: ephemeral | Redis only | Rate counters, provider concurrency/health budgets, ARQ scheduling and Telegram FSM/conversation state can disappear. Durable jobs, billing and outbox remain in PostgreSQL. Budgets may reset; an in-progress conversation must restart. |

## Snapshot, security and restore contract

Use official `pg_dump --format=custom`, not an application SQL serializer.
A read-only repeatable-read transaction exports a snapshot. Ordered streaming identity
and full-row SHA-256 digests and `pg_dump --snapshot` share that snapshot while it remains
open. No claim that later commits are included. Drain schema migrations during backup.
See [pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html) and
[snapshot synchronization](https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION).

Use a private directory and 0600 files, transient 0600 libpq password file, sanitized fixed
error codes, no shell and bounded subprocesses. Neither credentials nor row identities
are written to a manifest/report. Dumps remain confidential. The manifest authenticates
integrity against accidental corruption, not an attacker able to replace both files;
deployment must protect/encrypt storage and authenticate transport.

The conservative version policy requires matching PostgreSQL server/dump/restore major
versions. Restore uses a single transaction, exit-on-error, no owners/ACLs and no `--clean`.
Only explicitly confirmed, distinct, empty `_test`/`_rehearsal` targets are accepted.
The operator tool never creates/drops databases. Test fixtures own and clean up only their
randomly named synthetic databases. Deployment recovery needs a separately provisioned
replacement database; changing this guard is a future reviewed deployment decision.

Restore checks the exact RuntimePreflight schema contract before any migration, then
manifest digests, every user trigger definition, constraints/index validity, FK/check/
uniqueness/counter consistency and normal read-only domain services. Populated synthetic
rehearsals additionally execute rollback-only immutability probes and replay normal services.

## Load contract

Typed bounded profiles use authenticated HTTP through the normal API middleware and
routes. The operator harness serves the normal ASGI application on an ephemeral loopback
port; CI acceptance also uses HTTPX ASGI transport. Requests cover comparison, pagination,
search, preferences, subscription, history and persisted delivery comparison. No remote
delivery or retailer APIs are needed. Heavy workloads are manual; ordinary CI gates
completion, safe statuses, bounds, integrity and pool return, not latency percentiles.

Backup/restore and load measurements describe a synthetic local/CI run. RPO depends on
production backup cadence; RTO depends on database size, hosting and recovery infrastructure.
Hosting, encrypted transport/retention, live Stars acceptance and a real reviewed merchant
pilot remain separate beta-launch blockers.
