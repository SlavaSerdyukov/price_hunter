# Changelog

## 1.0.0 - Portfolio release

**Unreleased.** Prepared on `chore/portfolio-v1`; no release date or tag is assigned
before merge. This is a completed engineering portfolio snapshot, **not a live hosted
SaaS**. Real affiliate merchant and production payment activation were intentionally
not completed. Feature development is frozen.

### Implemented capabilities

- Async modular-monolith backend with shared domain/application services, an authenticated
  versioned REST API, seven-language Telegram client and ARQ workers.
- Exact-listing tracking, canonical product/variant identity, multi-merchant comparison,
  market/currency-specific product watches and append-only price/best-price history.
- Shared refresh/discovery work, database claims/leases/fencing, idempotent state/outbox
  commits and explicit uncertain-notification recovery.
- Subscription entitlements, immutable checkout/payment ledger, durable financial intake,
  cancellation/refund/reconciliation workflows verified with simulated Telegram transport.
- Official API/feed adapters, reviewed data permissions, canonical Merchant/Store identity,
  generational commerce staging/publication and catastrophic-feed-shrink guards.
- Source-preserving outbound links, reference FX and separate destination evidence /
  delivered-cost comparison; real dynamic quote providers remain disabled.
- Exact schema startup preflight, bounded resource/rate policies, operational diagnostics,
  real PostgreSQL backup/restore and authenticated synthetic HTTP load rehearsal.
- Automated lint/type/migration/coverage gates, historical engineering evidence and
  documented recovery/operational boundaries.

### Evidence and boundaries

The audited main baseline `c3b1788` passed **966 tests / 91.57% statement coverage**.
See [snapshot audit](docs/portfolio-audit.md), [verification](docs/verification.md) and
[local measurements](docs/m5c-rehearsal-evidence.json). Final portfolio PR CI evidence
is recorded in that PR; baseline measurements are not rewritten as new performance claims.
The heavier workflow is available but has no `workflow_dispatch` execution at the audit.

No public production hosting, live Stars lifecycle acceptance, real affiliate merchant
activation, Amazon tracking approval, real dynamic shipping, Mini App/web client or
destination-aware alert is claimed. No package is published to PyPI. No license is chosen
by this release preparation.

### Suggested GitHub release notes

> Completed backend engineering portfolio snapshot with a Telegram client and authenticated
> REST API, canonical multi-merchant pricing, recoverable async workers, audited commerce
> feeds and synthetic recovery/load verification. This is not a live hosted SaaS.
> External merchant and production payment activation were intentionally not completed.

### Owner's post-merge release steps

1. Merge the final portfolio PR after its final-head Checks are green. Verify that the
   resulting `main` commit also passes the existing Checks workflow.
2. In GitHub **Releases → Draft a new release**, choose **Create new tag: `v1.0.0`**,
   targeting that verified merged `main` commit. Do not tag the feature branch.
3. Set the title to **PriceHunter 1.0 - Portfolio Release**. Use the suggested notes above
   and link this changelog, the case study and the final verified CI run.
4. Preview and publish the release when the owner is ready; the publication supplies the
   actual release date. Do not mark it a deployment or publish a package to PyPI.
5. After release, old merged feature branches may be deleted manually. Confirm each PR is
   merged and no unique work remains; PRs and commits retain the historical record.
   Feature development then remains frozen.

No tag, release, branch deletion or GitHub settings change is performed by this preparation.
