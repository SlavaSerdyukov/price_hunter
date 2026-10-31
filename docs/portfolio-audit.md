# Portfolio snapshot audit

Audit date: **2026-10-31**. This records repository facts checked before the portfolio
presentation pass. It does not establish current external account access or a live service.

## Verified baseline

- Main: `c3b1788d4bfefc592516d6aebb5bf82b666f8691`, merged recovery/load work.
- Latest main [hosted Checks](https://github.com/SlavaSerdyukov/price_hunter/actions/runs/37508892290),
  completed 2026-10-06: **966 passed, no skips, 91.57% statement coverage**.
- Ruff, format, strict mypy, Alembic upgrade/check and the historical migration
  preservation verifier all passed in that run. The coverage gate remains 85%.
- Python requirement is `>=3.12`; CI uses 3.12. Compose/CI use PostgreSQL 17 and Redis 7.
- Async modular monolith: shared services/domain, FastAPI, aiogram and ARQ entrypoints;
  PostgreSQL owns business state, Redis owns ephemeral coordination/FSM/queue state.
- The reviewed durability inventory covers **33 tables**, including `alembic_version`:
  29 authoritative and four reconstructable; no PostgreSQL business table is ephemeral.
- Packaged schema head: `d7a3b951e620`; no schema change is part of this pass.

Selected versions actually resolved in `uv.lock` at the baseline:

| Dependency | Locked version |
| --- | --- |
| FastAPI / uvicorn | 0.141.1 / 0.53.0 |
| aiogram | 3.31.0 |
| SQLAlchemy / asyncpg | 2.0.54 / 0.31.0 |
| Alembic | 1.20.0 |
| Pydantic | 2.13.5 |
| Redis client / ARQ | 5.3.1 / 0.28.0 |
| httpx | 0.28.1 |
| pytest / Ruff / mypy | 9.1.1 / 0.16.8 / 1.20.2 |

## Integration evidence and boundaries

| Integration | Implemented and verified scope |
| --- | --- |
| Mock | Deterministic offline catalog, variants, comparison and synthetic delivery quotes; default local demo. |
| eBay Browse | Adapter/fixtures; historical Production OAuth and BE search/lookup/refresh checks recorded under 2026-09-19. EU marketplace/variation handling is implemented. This does not prove current access, EPN attribution or price-history rights. |
| WooCommerce Store API | PINE64 EU, RaspberryPi.dk, Hemptees and Western Shop Bruxelles; historical live search/lookup/refresh and exact-variant checks on 2026-09-19. Runtime enablement still needs explicit reviewed data policy. |
| Amazon Creators | OAuth/OffersV2 adapter and fixtures; disabled, API access and separate tracking approval unverified. |
| Rakuten | Product Search, policies and outbound fixtures; disabled until account/advertisers/rights are configured; no live acceptance claimed. |
| Awin / TradeDoubler / CJ | Feed acquisition/normalization, staging, reviewed programs, publication and materialization fixtures; disabled, no real affiliate merchant activated. |
| Telegram | aiogram client, seven locales, polling/webhook and simulated financial lifecycle; historical live mock tracking/notification path was operator-confirmed. No production Stars purchase/renewal/refund acceptance. |
| ECB | Optional timestamped reference-rate adapter/fixtures; not authoritative converted-price ranking. |
| Dynamic delivery | Synthetic mock only; no real quote provider enabled. |

No new live calls or external credentials were used for this audit. The configuration
example starts real retailers, networks and Stars checkout disabled. Best Buy, Stripe
and Wallet Pay settings are reserved/disabled inputs, not implemented live integrations.
Historical detail remains in [verification](verification.md).

## Operational evidence

- Local synthetic evidence dated **2026-10-05**, with PostgreSQL/client 17.11 and Redis 7:
  148,313-byte real custom `pg_dump`; backup **0.291s**, restore plus verification **0.545s**.
  All 33 table identity/full-row digests, exact schema, protections and domain checks passed.
- Restore/replay, Redis loss, leased-job succession and uncertain-notification non-resend
  have automated acceptance coverage. Normal hosted CI includes bounded recovery/load tests.
- Local beta-medium TCP HTTP: 24 concurrency, 30 active synthetic users, **1,400 requests**,
  **300 successes**, **1,100 expected 429**, **zero unexpected failures**, zero checked-out
  connections; integrity/response bounds passed. Throttled requests are not successful
  throughput. Measurements are not a production capacity or RPO/RTO promise.
- Synthetic 100,000-row feed import/replay and validation/shrink checks are documented and
  covered; the validation memory bound is below 32 MiB of traced Python allocations.
- The heavier **Synthetic recovery and load** workflow exists and supports manual dispatch.
  The GitHub `workflow_dispatch` run listing contained **zero runs** at this audit. It has
  **not** been verified through hosted manual execution; local evidence is separate.
- [Sanitized recovery/load measurements](m5c-rehearsal-evidence.json) remain unchanged.

## Warnings and secret hygiene

The baseline's one pytest warning is ARQ 0.28 calling Redis's deprecated `close()`;
application-owned cleanup uses `aclose()`. GitHub action tooling also emitted upstream
Node `punycode` / `url.parse()` deprecation warnings. No dependency or gate is weakened
to hide these warnings.

Tracked-file pattern and configuration review found no production credentials. `.env`
and `.env.*` are ignored, with only `.env.example` allowed; external secret fields in
that example are empty/placeholders. Local Compose/CI database passwords are synthetic
development examples. Userinfo-bearing retailer URLs are deliberate rejection-test
fixtures, not account credentials. No database dumps are tracked. The recovery evidence
contains metrics, fixed labels and a public git revision, with no raw user IDs, API keys,
postcodes or business rows. The ignored private `.env` was not read or changed.
This was a review of tracked snapshot files, not a claim of a full historical secret scan.

Repository description, topics and homepage were empty at audit; no LICENSE or GitHub
Release was present. Licensing and post-merge tagging remain owner decisions. See
[release preparation](../CHANGELOG.md) and the [case study](portfolio-case-study.md).

## Owner-controlled repository metadata and cleanup

Suggested GitHub description:

> Production-oriented international price-tracking backend with FastAPI, Telegram,
> PostgreSQL, Redis, async workers, commerce feeds and recovery/load rehearsal.

Suggested topics: `python`, `fastapi`, `postgresql`, `redis`, `sqlalchemy`, `asyncio`,
`telegram-bot`, `price-tracker`, `backend`, `system-design`. Homepage can remain empty:
there is no public hosted service. This pass does not change repository settings.

At the audit, each remote feature head below matched its merged PR head and was an
ancestor of current main. After the final portfolio PR and release, these are eligible
for **manual** cleanup if a fresh check still shows no unique work:

| Merged PR | Remote branch |
| --- | --- |
| #1 | `feat/m3-comparison-core` |
| #2 | `feat/m3b-autonomous-discovery` |
| #3 | `feat/m4a-international-commerce` |
| #4 | `feat/m4b-commerce-feeds` |
| #5 | `feat/m4c-coverage-engine` |
| #6 | `feat/m4d-canonical-merchants` |
| #7 | `feat/m4e-merchant-pilot-gate` |
| #8 | `feat/m5a-beta-runtime-hardening` |
| #9 | `feat/m5b-delivery-context` |
| #10 | `feat/m5c-recovery-load-rehearsal` |

No remote branch or history is deleted/rewritten. PR and commit history preserve the
development record. No LICENSE is present; public source without a license remains
all-rights-reserved by default. MIT/Apache or another reuse grant requires a deliberate
owner choice; see [GitHub's licensing explanation](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository).

## Portfolio presentation acceptance

On 2026-10-31 the README mock flow was exercised over real loopback HTTP against
a separate migrated disposable PostgreSQL 17 database: settings 200, comparison 200,
URL resolve 200 and tracker creation 201. Package metadata and OpenAPI both reported
`1.0.0`. No retailer/Telegram call or real credential was needed; the temporary demo
database was removed. This verifies the documented REST demo, not a live end-user service.

All local file links/anchors across 45 Markdown files passed validation. The lock-file
comparison confirmed unchanged third-party package versions. The presentation pass
reviews 319 tracked/candidate files for secret hygiene; no production credential or
tracked database archive was found. Only the package/lock/OpenAPI version metadata changes
in executable/package files; domain code, tests, migrations and CI gates are preserved.
Final full-suite and hosted portfolio-head results are recorded in the portfolio PR.
