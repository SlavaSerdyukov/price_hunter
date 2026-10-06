# PostgreSQL backup and recovery

M5C supplies local operator tools and synthetic rehearsal, not a hosting/retention service.
Use matching PostgreSQL major versions for the source server, `pg_dump`, target server and
`pg_restore`. An obsolete/mismatched client fails safely. Install the appropriate client
and place its `bin` directory first in PATH; do not start a second server to get a client.

## Create and verify

Credentials enter through environment variables, never URL command arguments. The tool
does not load `.env` implicitly. `uv run --env-file .env` can load the operator's existing
private file. Never echo credentials or put them in a manifest. The helper supports TCP
PostgreSQL URLs and optional `sslmode`; unsupported connection options fail closed.

```sh
umask 077
mkdir -p /private/operator-backups
uv run --env-file .env python -m pricehunter.apps.recovery backup-create \
  --backup /private/operator-backups/current.dump
uv run python -m pricehunter.apps.recovery backup-verify \
  --backup /private/operator-backups/current.dump
```

Choose a platform-appropriate private path. Existing backup/manifest files are never
overwritten. Schema migrations must be drained while taking a backup. Application data
writes can continue: an exported repeatable-read snapshot is shared by the streaming
manifest digests and the real custom-format `pg_dump`. The exporter stays open until
the dump completes. Later commits need not appear. The archive contains schema/data,
indexes, constraints and triggers; no SQL serialization through the application occurs.

Sidecar `current.dump.manifest.json` has format/version, UTC time, optional git revision,
packaged schema head, server/client versions, SHA-256/size, a hashed endpoint identity,
all 33 table counts, ordered primary-key and full-row SHA-256 fingerprints, schema
protection signature and observed backup duration. The inventory covers every mapped
business table and Alembic; unclassified mapped or deployed tables fail verification.
Digests stream 128 rows at a time with length framing. No raw IDs, keys, charge IDs,
postcodes, source URLs or credentials enter the manifest.

Directories must be private; dump/manifest/password files use 0600. The transient libpq
password file is removed on success/failure/cancellation. Child processes use no shell,
have deadlines, suppress unsanitized stderr and are killed/reaped on cancellation.
Passwords never appear in their arguments. Error output uses fixed codes and nonzero exit.

The checksum detects accidental corruption; it is not a signature authenticating an
attacker-controlled archive and sidecar. Restore only trusted operator backups. Encrypt
production backup storage/transport, restrict access, select retention and schedule
verification in deployment configuration. No cloud vendor is selected here. Never attach
a dump to issues, PRs, CI artifacts or reports; archives include confidential business data.

## Restore into an empty replacement

Provision an empty, isolated database separately. The tool never creates/drops a database,
uses no `--clean`, refuses any target containing user relations and requires a distinct
endpoint/database and explicit confirmation. By default names must end `_test` or
`_rehearsal`; production database names are rejected. This conservative boundary must be
reviewed with future hosting before production recovery is automated.

Supply its connection through `RESTORE_DATABASE_URL` in the process environment or a
separate private env file, then run:

```sh
uv run --env-file recovery.env python -m pricehunter.apps.recovery restore-rehearsal \
  --backup /private/operator-backups/current.dump --confirm-disposable
```

Verification precedes mutations. Restore uses real `pg_restore --single-transaction
--exit-on-error --no-owner --no-privileges`. A command failure rolls back the restore.
The restored exact schema head is checked through RuntimePreflight **before any upgrade**.
An incompatible archive is rejected; the tool never upgrades it to disguise incompatibility.
Count/identity/payload digests, user trigger/function definitions, portable constraint
definitions, valid indexes, FK/uniqueness/check consistency and nonnegative job counters
are checked. PostgreSQL's equivalent varchar/text enum-array reparse is normalized narrowly.
Normal read-only user/subscription/catalog/comparison/watch/history/merchant/program
services are exercised without retailer calls. General backups can have empty tables;
the populated synthetic rehearsal additionally proves all immutable guard rejections.

A post-restore verification failure leaves that disposable target for investigation;
never open traffic to it. Cleanup/replacement is an explicit operator decision.

## Ordered incident procedure

1. Stop/drain writes, workers and operator validators if required by the incident; preserve
   Telegram pending updates and all immutable payment/outbox/audit evidence.
2. Select a trusted backup, verify sidecar/checksum and application revision compatibility.
3. Provision an empty replacement database, separate from the failed/source database.
4. Restore using the confirmed disposable-target command.
5. Verify manifest counts/identity/payload checksums and DB-enforced protections.
6. Verify exact packaged schema compatibility before any planned migration.
7. Supply replacement DB/Redis configuration and run `apps.admin runtime-preflight`.
8. Start API/bot/worker with normal startup preflight; inspect `apps.admin runtime-status`.
9. Smoke authenticated API/Telegram reads, rights, watches/history and outbox status.
10. Open traffic only after these checks and any external financial reconciliation pass.

Never delete immutable evidence or resend uncertain notifications to force recovery.
An older backup cannot undo a payment or Telegram send already completed externally:
reconcile the backup's recovery point with authenticated external financial/delivery
evidence before enabling workers or intake. Database replay checks cannot prove external
exactly-once delivery or account settlement.

## Synthetic rehearsal

Use a migrated empty loopback database ending `_test`, an independently provisioned
empty target ending `_test`, and dedicated loopback Redis DB **14**. Set
`REHEARSAL_DATABASE_URL`, `RESTORE_DATABASE_URL`, `REHEARSAL_REDIS_URL` in the environment:

```sh
uv run python scripts/restore_rehearsal.py --report recovery-report.json
```

The fixture includes Free/Pro/Power, fake Stars purchases/refund/pending intake/checkout/
uncertain operation, catalog/identifiers, canonical cross-network sources, observed
prices, trackers/watches/history, pending/sent/uncertain outbox, reviewed synthetic
programs/audits/validation/shrink exception, staged feed/leases, FX, clicks and delivery
quotes. Normal services allocate catalog/billing UUIDs; the deterministic recipe never
regenerates those identities on restore. No Telegram/retailer request is made.

Replay known billing intake/payment, watch outbox deduplication, offer snapshots and
same feed generation/materialization; financial/audit/history payloads and all identity
digests remain unchanged. Flush/reconnect dedicated Redis and verify durable state and
readiness. API outage tests prove readiness fails until Redis returns. Rate/provider
budgets, ARQ transient scheduling and Telegram FSM state may reset; customers restart
incomplete conversations. PostgreSQL lease/outbox/billing/watch/feed state survives.

Crash probes expire recorded leases without long sleeps, reclaim with successor tokens,
and reject old refresh/discovery/feed/technical-validation commits. A possibly delivered
`sending` notification becomes `uncertain`, without auto-resend. Real cancellation and
lease deadlines retain the existing M5A semantics; production code is unchanged.

Temporary dumps and sidecars are removed even on failure; only sanitized reports remain.
Production retention is operator-owned. Observed durations are not an SLA. RPO depends
on backup frequency; RTO depends on database size, hosting and recovery infrastructure.
