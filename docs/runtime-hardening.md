# Runtime contracts

All runtime entrypoints share the same read-only schema/Redis/config preflight.
The application schema must equal its packaged Alembic head; startup never
migrates. Health excludes retailer availability. `/health/live` is dependency-free;
`/health/ready` is deadline-bounded and returns only `ready` or `not_ready` (503).
Internal reasons are fixed codes: `schema_mismatch`, `database_unavailable`,
`redis_unavailable`, `configuration_invalid`, `dependency_timeout`.

Production rejects mock providers, wildcard/empty hosts, incomplete webhook
credentials, enabled credential providers without their keys and enabled feeds
without explicit program UUIDs. Preflight checks the corresponding database rights.
Providers are optional; eBay-only and reviewed WooCommerce-only settings are valid.
Enabled provider catalog rights are still required by the existing composition root.

## Request limits and failures

`USER_REQUESTS_PER_MINUTE` covers authenticated API operations and ordinary
Telegram commands/callbacks using one internal user UUID. Limit exceeded returns
`429 {"error":"rate_limit"}` with `Retry-After: 60`, or a localized Telegram reply.
Search's daily plan quota and provider limits are additional independent budgets.
Health and signed redirects are exempt from the user budget. Financial updates
retain durable intake and bypass throttling/conversation isolation; storage failure
requires transport retry. Never acknowledge uncertain financial persistence.

Redis failure returns safe `503 {"error":"service_unavailable"}` to API users and
a localized Telegram message. Readiness fails while liveness answers. Unexpected
API failures stay `500 {"error":"unexpected_error"}`; bearer failures remain
`401 {"detail":"Unauthorized"}`. No token/key/URL/exception text is returned.

Generated `X-Request-ID`, `nosniff` and `no-referrer` accompany HTTP responses.
Private and signed redirect routes use `Cache-Control: no-store`; redirect signatures
and destination checks remain unchanged. Context is scoped and restored on success,
error and cancellation. Logs contain correlation/operation/entity IDs, no request
body, names, user text, Authorization, digest or payment payload.

## Resource and timeout policy

| Setting | Default | Meaning |
| --- | --- | --- |
| `DATABASE_POOL_SIZE` | 10 | Persistent connections per composition root/process |
| `DATABASE_MAX_OVERFLOW` | 10 | Temporary connections beyond the pool |
| `DATABASE_POOL_TIMEOUT_SECONDS` | 10 | Maximum connection checkout wait |
| `DATABASE_POOL_RECYCLE_SECONDS` | 1800 | Retire aged idle pooled connections |
| `DATABASE_COMMAND_TIMEOUT_SECONDS` | 300 | asyncpg command deadline for runtime SQL |
| `RUNTIME_PREFLIGHT_TIMEOUT_SECONDS` | 5 | Whole local preflight/readiness/status deadline |
| `VALIDATION_TIMEOUT_SECONDS` | 7200 | Total remote technical acquisition deadline |
| `VALIDATION_LEASE_SECONDS` | 7500 | Fixed ownership deadline; must exceed acquisition |

Pool limits multiply across API/bot/worker replicas; reserve PostgreSQL connections
for maintenance and migrations. SQL's five-minute safety bound accommodates
known large feed publication; it is not a target user request duration. Ordinary
provider operations keep their existing 20-second default bound (maximum 40).
Readiness has its own shorter deadline. No global two-hour HTTP timeout was added.
Alembic uses a separate `NullPool` engine and does not inherit runtime command/pool
settings; schedule long migrations explicitly. Changing operational feed deadlines
must retain `lease > acquisition` and enough completion time.

ARQ default stays 60 seconds; overrides: feed 7200, refresh 120, discovery 180,
notification batch/cron 300. Notification batch is ten, not 100. Cron maintenance
uses bounded database work; watch elapsed time under realistic data volume because
SQL command limits alone do not guarantee every multi-query job fits its deadline.
Cancelled refresh/discovery/feed jobs leave durable leases reclaimable after expiry.

Framework shutdown stops/drains work before closing clients. Container closes owned
HTTP/Telegram, Redis and engine once. Borrowed database/Redis clients and injected
API containers are caller-owned. FSM borrows shared Redis without closing it.
Worker/polling/API startup failures also clean owned resources.

## Notifications and leases

Pending events can be claimed. A sending event older than two minutes becomes
uncertain on worker startup or the next claim. Uncertain events are never resent
automatically; review delivery evidence before any operator resolution. Definite
retry/permanent failure semantics and sent/cancelled history remain unchanged.

Validation uses short state/program claim and completion transactions around remote
acquisition. It checks random token, unexpired lease, fingerprint and rights before
appending evidence. Expired older work adds no report; cancellation releases only
its own token. Process death waits at most 125 minutes under defaults. Lease
diagnostics omit tokens; no CLI silently clears live ownership.

See [release and recovery](beta-operations.md), [design](m5a-design.md) and
[verification evidence](verification.md).

M5C adds [real PostgreSQL backup/restore](disaster-recovery.md) and
[authenticated HTTP load rehearsal](load-rehearsal.md) without changing these runtime
budgets or business semantics. Synthetic acceptance covers lease succession, Redis loss,
uncertain notification non-resend, pool pressure, shared-rate isolation and post-load
integrity. Hosting-specific capacity, encrypted backup transport/retention and live beta
acceptance remain deployment work; no production RPO/RTO or throughput SLA is promised.
