# PriceHunter engineering case study

PriceHunter is a completed backend engineering portfolio project. It has a Telegram
client and an authenticated REST API, but no public production service. This case study
describes implemented boundaries and tested behavior, not a claim of commercial traction.

## Problem hypothesis

The original idea was to let a user track a product and compare prices across retailers,
initially through Telegram. A useful result would need to distinguish the same variant
from a similar title, identify the actual retailer behind affiliate sources, and avoid
calling an incomplete shipping estimate the cheapest delivered offer.

That was a product hypothesis. Market demand, distribution and willingness to pay were
not established by implementing the backend.

## Scope built

The system resolves listings, stores price observations, shares refresh work across
subscribers, compares canonical products and supports exact-listing trackers and
market/currency-specific product watches. Telegram provides onboarding, settings,
variant selection, history and notifications in seven languages. REST exposes the same
application workflows with user-scoped bearer authentication.

The backend also includes a subscription/payment ledger, bounded discovery, reviewed
merchant programs, commerce feed staging/publication, canonical retailer identity,
reference FX, explicit delivery comparison, operational diagnostics, schema preflight
and synthetic recovery/load tooling. Real affiliate merchants and production payment
activation are outside the preserved snapshot.

## Architectural approach

An async modular monolith kept transaction boundaries and domain rules in one system
while separating interfaces from application behavior. API, Telegram and ARQ entrypoints
call shared services. The domain has no Telegram, SQLAlchemy or HTTP types; provider
adapters translate external payloads into validated evidence.

PostgreSQL is authoritative for identities, rights, observations, watches, audit records,
leases and delivery state. Redis supplies transient queues, rate budgets, coordination
and conversation state. Losing a Redis queue must not erase the database's knowledge
of due work or a possibly delivered notification.

This avoids distributed business transactions across services while retaining distinct
API, bot and worker processes. It also leaves transaction ordering, database query bounds
and shared-resource contention as responsibilities to test explicitly.

## Hard engineering problems

### 1. Shared refresh with crash recovery

Refreshing one listing for every subscriber would multiply provider calls. Trackers
instead share a StoreOffer. A bounded scheduler claims due work with `SKIP LOCKED` and
commits a database lease before enqueueing. Workers perform external I/O outside the
transaction, then verify a random fencing token before accepting results. Expired claims
can be reclaimed after lost enqueue or process death; stale workers cannot overwrite a
successor.

An accepted observation, aggregate state, watch evaluation and resulting outbox evidence
commit through the existing transactional workflows. Tests cover concurrent claims,
replayed observations, lost queues and stale-token rejection.

### 2. Idempotency at an external delivery boundary

Unique logical event identities deduplicate notification generation; the payment inbox
and append-only ledger similarly protect duplicate financial updates. These guarantees
do not make Telegram `sendMessage` exactly once: a timeout after a possible successful
send leaves no reliable acknowledgement.

The outbox records that attempt as `uncertain` and does not auto-resend it. This can miss
an ambiguous alert, but avoids manufacturing duplicate sends as if they were safe retries.
Definite rate-limit failures and permanent errors retain separate retry/failure semantics.

### 3. Canonical products without unsafe fuzzy merging

A Product represents a purchasable item/variant, not a bag of similar titles. Validated
identifiers and compatible brand/model evidence can support shared identity; conflicting
identifiers, variants or ambiguous candidates keep products separate. An incoming listing
must agree with saved evidence, so a weak listing cannot bridge incompatible strong ones.

Title similarity can help retrieve/rank candidates but never authorizes a merge. This
intentionally accepts some duplicate products rather than silently combining different
sizes, capacities or conditions.

### 4. Retailer identity across acquisition networks

A Merchant is the retailer the customer recognizes. A Store is an acquisition identity;
a StoreOffer retains that source's listing, market, price, permissions and outbound link.
New sources start with their own default Merchant; reconciling multiple acquisition
identities requires an explicit, versioned, audited operation.

Effective-offer selection deduplicates a retailer across networks before summaries and
pagination. It does not borrow a cheaper source's price and another source's commission
or link. Item and delivered-price views can legitimately choose different source tuples.

### 5. Publishing large, fallible commerce feeds

Feed acquisition writes candidate staging in bounded batches. Only a complete generation
that passes structural, malformed-row and shrink checks can replace the published one.
Failed/partial acquisition preserves the current generation; retries preserve staging
identities. Technical validation and rights review are separate evidence.

A catastrophic reduction in distinct listings is quarantined rather than interpreted as
thousands of real stock changes. A deliberate one-shot shrink exception requires an
immutable audit. Synthetic acceptance includes 100,000-row import/replay and validation;
those tests are correctness/work-bound evidence, not a retailer throughput promise.

### 6. Market, currency and delivery evidence

A catalog market does not prove delivery eligibility. Best item price is computed within
one market/native currency using fresh, eligible, confirmed in-stock offers. Reference FX
can help display an approximate value but cannot authorize mixed-currency ranking.

Delivered totals require compatible destination scope, shipping and tax evidence. Missing
cost is unknown, not free. Evidence is selected as a whole tuple: current usable evidence,
then EXACT over COUNTRY scope, then newer quote time, with a deterministic static tie-break.
A failed dynamic lookup cannot hide usable current static evidence. Delivered ranking is
separate from item-price watches and history.

### 7. Recovery as a verified database round-trip

The recovery helper uses real custom-format `pg_dump`, not an application row copier.
An exported repeatable-read snapshot binds the archive and streaming table fingerprints
to one database state, even while application writes continue.

Actual `pg_restore` runs only against a confirmed distinct empty disposable target.
Verification checks exact schema compatibility before any migration, all 33 table counts,
identity/full-row digests, constraints/triggers and ordinary domain reads. Replay, Redis
loss, leased-job succession and uncertain-notification behavior are then exercised with
synthetic relationships rather than an empty schema alone.

## Important trade-offs

- Conservative identity permits duplicates; there is no automatic fuzzy product merge.
- Native-currency rankings remain authoritative; converted values are reference display.
- Unknown shipping/tax cannot produce a falsely complete delivered total.
- Ambiguous Telegram delivery is reconciled rather than blindly resent.
- Merchant linking is explicit and audited; shared names/domains are insufficient.
- Provider access and data permissions fail closed. Public API availability or OAuth
  success does not itself authorize retained history, alerts or affiliate use.

These decisions favor explainable evidence and bounded failure over optimistic inference.
They also limit apparent coverage: a source may be visible in diagnostics without being
eligible for tracking or authoritative comparison.

## Reliability strategy

The system combines short transactions, database uniqueness/locks, leases/fencing,
durable inbox/outbox state, immutable financial/commercial evidence and exact startup
schema checks. Feed publication guards protect the last known complete generation.
Operational diagnostics distinguish backlog, expiry, uncertainty and source failure.

Verification includes 966 automated tests at the audited baseline, strict typing,
migration preservation checks, real backup/restore and authenticated HTTP acceptance.
Local beta-medium evidence records 1,400 requests: 300 successes, 1,100 expected rate
limits and no unexpected failures. That is not a successful-request capacity benchmark.
The heavier manual GitHub workflow is available but had no dispatch run at the audit.

See [verified snapshot](portfolio-audit.md), [architecture](architecture.md),
[recovery](disaster-recovery.md) and [load rehearsal](load-rehearsal.md).

## Product decision

After building the system to near production-readiness in its backend controls, I
re-evaluated the distribution and monetization assumptions. A Telegram-first go-to-market
strategy did not appear strong enough for the intended European audience to justify
additional launch investment. I therefore stopped product development intentionally and
preserved PriceHunter as a completed engineering case study.

This is a product judgment, not a statistical claim about Telegram adoption or consumer
willingness to pay. No public SaaS, production Stars lifecycle, real affiliate merchant
pilot or production backup operation is claimed.

## What I would do differently

I would validate distribution earlier and test the smallest useful product surface before
investing in broad commerce infrastructure. I would concentrate on one market/provider
first, and distinguish reusable backend platform value from Telegram distribution earlier.

The implemented infrastructure made correctness and failure modes inspectable. Earlier
product validation could have established whether that depth justified a commercial launch
before widening the integration surface.

## What the project demonstrates

PriceHunter demonstrates backend domain modeling, async transaction design, conservative
identity, source provenance, idempotent event processing, recoverable workers and evidence-based
operational verification. The final result is a versioned, inspectable engineering snapshot
with explicit commercial and operational boundaries, not an ongoing feature roadmap.
