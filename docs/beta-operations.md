# Beta release and recovery

## Release sequence

1. Back up PostgreSQL and verify that the backup can be restored. Retain immutable
   billing, notification, merchant and publication evidence. Keep credentials outside
   images/logs/source control.
2. Build the candidate image from the reviewed commit. Schedule a maintenance window
   for this exact-schema boundary; gracefully stop/drain old API, polling and worker
   processes and operator validators. Do not overlap M4E/M5A validators. Keep Telegram
   pending updates: no `drop_pending_updates`/offset reset.
3. Run `uv run alembic upgrade head` once using candidate code and an operator process.
   Do not migrate from every replica. Existing guarded migrations remain guarded.
4. Run `uv run python -m pricehunter.apps.admin runtime-preflight` in the deployment
   environment. It exits nonzero with a fixed safe reason on mismatch/dependency/config
   failure. Resolve that reason before starting replicas.
5. Start API, polling bot (or webhook API), and worker. Every process repeats preflight.
   Webhook registration remains an explicit operator action. Provide required reviewed
   policies/keys only for enabled providers.
6. Verify `/health/ready` returns 200 and `{"status":"ready"}` through the actual
   host/TLS routing. Include `127.0.0.1` and the explicit public hostname in
   `ALLOWED_HOSTS` when using the image's localhost HTTP healthcheck. Never use `*`.
7. Smoke ordinary Telegram/API flows and, separately, the existing authorized billing
   acceptance. Confirm throttling/localized messages and safe headers. No new real
   merchant activation is part of this milestone.
8. Observe `uv run python -m pricehunter.apps.admin runtime-status`, structured logs,
   job durations, resource utilization and backlogs before opening beta traffic.

The image healthcheck uses Python stdlib against localhost readiness, with no
credentials in command arguments. Compose disables the inherited HTTP healthcheck
for bot/worker, which have no HTTP listener; their startup preflight and process/job
supervision are the relevant checks. Configure graceful termination long enough
for normal financial intake and short DB commits; interrupted longer jobs recover
through leases.

## Aggregate diagnostics

`runtime-status` is local/operator-only and read-only. It reports expected/current
schema compatibility, Redis availability, due/leased offers, due discoveries,
pending/sending/uncertain notifications, pending billing updates, due/failed feeds,
active programs, programs without current matching technical validation, expired
refresh/discovery/feed/validation leases and ten most recent quality-rejected program
IDs. No customer identity, source URL, payment data or fencing token is emitted.
The whole report has a five-second default bound; a timeout/incompatible schema
omits unavailable counts instead of making them up.

Due counts are timestamp candidates, not a promise that every row has current
eligible customer interest or reviewed provider rights. Leased/expired counts
describe recorded deadlines. Compare successive reports with job logs and cadence.
Counts never trigger deletion or re-enqueue on their own.

Suggested initial warning thresholds, to tune with beta volume:

- Any schema incompatibility or unavailable Redis: stop rollout/investigate immediately.
- Any uncertain notification or failed quality publication: review before resending or
  activating. Keep the last good catalog generation available.
- Any pending billing update across several maintenance passes: investigate durable
  intake and database connectivity; never fabricate subscription grants or refunds.
- Increasing pending notifications above 100, or due refresh/discovery candidates
  above twice their configured scheduler batch for ten minutes: inspect throughput,
  eligibility and provider budgets. Avoid interpreting inactive catalog rows as a queue.
- Expired leases persisting across two scheduler passes: inspect worker/enqueue logs;
  ordinary claims already reclaim expired work. Technical validation is operator-driven:
  rerun the existing validation command after expiry.
- Repeated feed failures across two configured sync intervals or stale validation on
  an intended pilot program: inspect the existing safe feed/program diagnostics and
  rights/configuration before retry. Other merchants may remain healthy.

## Failure recovery

Redis outage: liveness remains up, readiness and ordinary user requests fail closed.
Restore Redis, rerun preflight and observe due claims. PostgreSQL leases/outbox/billing
remain authoritative; never delete backlog or acknowledge uncertain payment persistence.
Redis minute/daily budgets are ephemeral and may reset if Redis state is lost, so keep
its durable deployment configuration and capacity appropriate to the traffic.

Worker crash: offer/discovery/feed claims become eligible after their existing lease
expires. Technical validation's fixed default lease expires after 125 minutes; its
acquisition is capped at 120 minutes. Cancellation normally releases its own claim
immediately. A stale validator cannot append passed or failed evidence over a successor.
Never clear tokens while old code/work can still execute.

Notification crash: sends older than two minutes become `uncertain` on startup/claim.
There is no automatic resend because Telegram may already have delivered. Review
the delivery/payment evidence with existing operator tools. Sent/failed/cancelled
and definite retry transitions retain their existing meaning.

Schema failure: compare `runtime-status` with the candidate revision. Upgrade a
behind schema using the operator migration process. An ahead schema also requires
a compatible application build or a reviewed schema rollback; replicas cannot
repair either state automatically.

## Rollback limits

Prefer a forward fix or restoring a verified backup into a controlled replacement
environment. Exact head compatibility prevents simply starting an older M5A binary
on a newer schema. Stop all processes/operator validation before schema rollback.
The M5A downgrade refuses any outstanding validation token; use normal completion
or cancellation to release it, and reconcile crash claims only after proving their
processes are stopped. Read-only diagnostics do not perform this reconciliation.

Older migrations guard immutable pilot/publication evidence, canonical merchant
identity/audits, market-scoped history and billing/catalog data. Export/reconcile
according to their existing instructions; never truncate production evidence to
force downgrade. Destructive export/truncate in the verifier occurs only in its
disposable synthetic database.

Beta launch still needs selected hosting/TLS and backups, restore/load rehearsal,
provider approvals/real feed/link verification and live Stars acceptance. See
[runtime limits](runtime-hardening.md) and [merchant pilot](merchant-pilot.md).
