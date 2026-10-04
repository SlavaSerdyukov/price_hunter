# M5A — Public beta runtime hardening

## Baseline

M4E PR [#7](https://github.com/SlavaSerdyukov/price_hunter/pull/7) was merged before
this work. Its final head `837ffdc7c1ce34f20d5de89e54a5435bd2664848` passed hosted
[run 37213708320](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/37213708320):
752 tests, 90.94% statement coverage. This branch starts at merged main
`fab0c9faed6a5822f1a50c60ae92df439ca5648c`; its source tree matches that verified head.

Local baseline on 2026-10-04: **752 passed, 90.92%**, 242.96 seconds, no skips.
Ruff/format passed (**215 formatted files**, including **183 Python files**),
strict mypy for **119 source files**.
Alembic upgrade/check and the disposable historical migration verifier passed.
The known upstream warning is ARQ's Redis `close()` deprecation. M5A also sets
Alembic's explicit `path_separator` to avoid its configuration deprecation.

Seven required acceptance scenarios were written and executed before production
code changes: **7 failed**, 0.79 seconds. They cover external validation without
an open SQLAlchemy transaction, stale-validator fencing, incompatible schema,
shared API/Telegram budget, financial bypass, Redis outage and concurrent contexts.

## Small shared runtime contract

`RuntimePreflight` checks the packaged `EXPECTED_SCHEMA_HEAD` against the single
`alembic_version` row, Redis ping, structural production settings and reviewed
feed configuration. The whole check has a five-second default deadline. The API
lifespan, polling startup, ARQ startup and operator deployment hook use this same
service. Behind, ahead, missing and multi-head schemas cannot pass. The packaged
constant is tested against the actual single Alembic head.

Liveness uses no dependencies. Readiness repeats only local checks and emits a
two-state JSON response; logs contain a fixed safe reason. No migration, OAuth,
feed acquisition, FX fetch or Telegram API call belongs in startup/readiness.

## Durable validation ownership

Migration `b65a7012c904` adds nullable `validation_token` and
`validation_lease_until` to the existing `FeedSyncState`. Claim locks state then
program, verifies rights, snapshots the fingerprint and commits a random token.
Source version and streaming acquisition run after that session has closed, using
the existing bounded adapters, row caps, SQLite disk deduplication and unchanged
M4E quality evaluator. Completion locks in the same order and requires both the
same token and an unexpired lease, then rechecks fingerprint and reviewed rights.

A fixed lease is deliberately longer than the total acquisition deadline:
default 7,500 versus 7,200 seconds. No heartbeat transaction is needed during
remote streaming. Cancellation releases only the matching claim; process death
is recovered when that lease expires, at most 125 minutes by default. Operators
can reduce both deadlines for smaller feeds, keeping lease greater than acquisition.
A successor replaces an expired token. Older work writes no evidence at all,
including failed evidence that could supersede a newer pass. The older M4E
adapter revision, activation age, rights and quality thresholds remain unchanged.

The new downgrade refuses outstanding validation tokens. Stop validators and
allow normal completion/cancellation before rollback; never clear claims while
their processes can still run. Existing evidence/audit export guards remain.
The disposable verifier preserves M4E evidence across the new upgrade/rollback,
tests the active-token guard and reruns every older preservation check.

## External I/O audit

| Path | Boundary checked |
| --- | --- |
| Technical validation | Claim commit → bounded source calls → fenced completion transaction |
| Feed sync/dry run | Program read closes before source calls; staging batches and heartbeat use independent short transactions; publication remains atomic |
| Search/discovery/refresh | Market/rights/identity reads close before provider/OAuth calls; acceptance persists afterward |
| FX refresh | Redis lease → bounded ECB request → atomic local snapshot replacement |
| Outbound redirect | Local signature/destination validation and click insert; returning 302 makes no retailer request |
| Notifications/ordinary Telegram | Claim/user/service transactions finish before `sender.send` / reply; result commits afterward |

Architecture rule: services acquire bounded external data after closing their
SQLAlchemy read/claim scope, then open a short transaction to publish with the
existing fence and configuration/rights recheck. An async function holding a
session must not call a remote provider or Telegram sender. SQLite validation
scratch state is private local storage, not a retained PostgreSQL connection.
Acceptance instruments actual SQLAlchemy sessions at both validation source calls;
existing feed-sync/discovery/outbox concurrency tests retain their boundaries.

## Public traffic and logging

Bearer authentication checks the existing constant-shape bounded token contract,
then consumes the shared `ph:rate:user:{UUID}` budget. Telegram resolves that same
user and consumes it before ordinary commands/callbacks. The user middleware runs
once on the dispatcher parent, so traversal through
billing/comparison/main routers cannot multiply a single update. A dedicated
`/start`/callback regression ran red (one update incorrectly counted three) before
that fix. Search retains its daily plan quota and provider budget but no longer counts the minute budget a second
time. Health and signed redirects bypass the user limiter. Financial checkout,
success and refund updates bypass limiter/FSM failure handling; persistence
errors still require retry and are never silently acknowledged.

Redis limiter failures normalize to `service_unavailable`/503; ordinary Telegram
uses that localized message in all seven languages. Redis FSM failure also fails
closed. HTTP infrastructure errors contain no connection details. Provider/refresh
Redis helpers preserve the same safe failure contract.

HTTP wraps the entire Starlette error stack in a generated UUID scope. Response
headers are present for success, authentication, validation, body/host rejection
and unexpected errors. Private `/api/` and signed `/r/` routes use `no-store`.
`nosniff` and `no-referrer` are global; no CORS or HSTS policy is invented.
Invalid environment settings normalize to a fixed configuration error before
Pydantic can print raw input values. Client request IDs are ignored. Telegram scopes
contain update ID only. Worker
entrypoints scope opaque stable job correlation plus operation; exception text is
normalized before ARQ can print it. Cancellation and ARQ retry semantics remain.
Standard JSON logs include only approved correlation keys. Application Uvicorn
access logs are disabled to avoid printing search/redirect URL contents.

## Operations and lifecycle

`runtime-status` returns bounded aggregate counts and at most ten safe rejected
program IDs; configuration-matching validation freshness uses the activation
contract. No customer identities, links or tokens appear. It never changes state.
Worker startup and ordinary notification claims turn sends older than two minutes
into `uncertain`; that status is never selected for automatic delivery.

The default ARQ timeout remains 60 seconds. Feed sync retains 7,200; refresh gets
120, discovery 180. Notification jobs claim at most ten events with 20-second
Telegram calls and a 300-second job/cron timeout, replacing the old 100-event
batch that could overrun 60 seconds. Other cron jobs have bounded database batches
and at most one 20-second external FX call. See operational limits and monitoring
in [runtime-hardening](runtime-hardening.md) and [beta operations](beta-operations.md).

Container shutdown closes its Telegram/httpx clients once, and Redis/engine only
when it created them. Borrowed sessions/Redis and an injected API container remain
caller-owned. FSM storage/isolation borrow Redis. Startup failure also closes
owned resources. Framework drain/cancellation precedes cleanup.

## Verification and remaining work

Normal CI includes realistic production lifespan, absent docs/OpenAPI, a disposable
one-migration-behind database upgraded to successful boot, Redis failure,
cross-surface throttling/financial bypass, headers/context isolation, diagnostics,
recovery, owned shutdown and query/body bounds. No live credentials are required.
Final local suite: **812 passed, no skips, 91.59% statement coverage** (244.30s).
The 60 added cases preserve all 752 prior tests. Ruff lint/format passed (**224
formatted files**, including **189 Python files**); strict mypy passed for **121
source files**. Upgrade/check, full disposable migration verifier and Markdown
link/diff checks passed. The sole warning remains upstream ARQ Redis close.
Hosted final-head status is checked and linked in the delivery report before
merge readiness; see [verification](verification.md).

Public deployment, backup/restore rehearsal, load/capacity testing, provider
approvals and real merchant/Stars acceptance remain launch work. Fixed validation
crash recovery can wait for its full lease. Exact-schema checks require a maintenance
release across schema changes; M4E and M5A validators must not overlap. No new
network, merchant activation, matching/ranking/FX/billing policy or frontend was added.
