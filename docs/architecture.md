# Architecture decisions

1. **Modular monolith.** Bot and API call the same application services. Domain rules
   contain no Telegram, SQLAlchemy or HTTP types. Provider results are validated DTOs.
   PostgreSQL is authoritative; Redis holds ephemeral rate limits/FSM/ARQ scheduling.
2. **One offer, many trackers.** The database owns `next_check_at` and a bounded lease.
   Schedulers claim batches with `FOR UPDATE SKIP LOCKED`, commit, then enqueue.
   Workers fetch outside transactions, then verify the lease token before committing
   observation + offer + outbox atomically. Expired leases repair lost enqueue jobs.
3. **No unsafe arbitrary fetcher.** User URLs are parsed against exact retailer
   allowlists. Mock URLs are never fetched. eBay IDs are sent to fixed official API
   endpoints. DNS answers must all be public; connections pin the validated numeric
   IP while preserving the original TLS hostname. Redirects are rejected; response
   size and duration are bounded. No HTML scraper is enabled. A future scraper must
   reuse these restrictions and verify retailer permissions before it can be enabled.
4. **Conservative identity.** Strong global identifiers plus variant attributes form
   a canonical identity. Without identifiers, identity is store/listing scoped.
   Titles alone never merge persisted products. Currency groups sort separately.
5. **Money and time.** Decimal throughout, numeric database columns, JSON strings for
   amounts, ISO currencies and UTC aware timestamps. Locale-aware display is a client
   concern. No implicit currency conversion. Tracker baseline is stored at creation.
6. **Delivery semantics.** An atomic outbox deduplicates logical events and replayed
   observations. Successful sends are never intentionally repeated. Telegram
   `sendMessage` has no idempotency key: a timeout/crash after sending and before
   recording success cannot provide true exactly-once delivery. Such attempts become
   `uncertain` for operator reconciliation, rather than being blindly resent. Explicit
   rate-limit responses retry; forbidden/bad-request responses fail permanently.
   This trades possible missing ambiguous deliveries for avoiding duplicate alerts.
7. **Simple auth.** REST bearer API keys are random, user-scoped, SHA-256 digested in
   PostgreSQL, revocable with CLI. Telegram users are trusted only via bot updates;
   REST never accepts an unsigned Telegram ID as identity. No shared admin API token.
8. **Entitlement authority (M2).** `Subscription` and its unrefunded paid periods are
   the sole authority. `users.subscription_plan` was removed by migration: keeping a
   second mutable plan/cache served no remaining reader after the three M1 callers
   moved to `EntitlementService`. Existing subscription rows are preserved. Effective
   plan is the highest currently paid plan, or Free. UTC time predicates apply during
   every authorization and SQL scheduling query, even when cleanup is late. All quotas,
   paid features, search and history access derive from the central policy.
9. **Growth.** History is append-only with a unique refresh key and (offer,time)
   index. A batched opt-in retention command removes old rows; lifetime min/max/count/
   total remain on the offer, while series endpoints explicitly report retention.
   Retention is disabled by default. Partitioning and daily rollups wait for evidence.
10. **Operational boundaries.** Redis uses AOF; PostgreSQL uses persistent volumes.
    Neither is a backup. Production needs backups, a TLS reverse proxy with body
    limits, egress restrictions, monitored failed/uncertain outbox rows and quotas.
    No infrastructure deployment or retailer account approval is included here.
11. **Clothing variants.** WooCommerce parent links prompt a bounded, paginated choice
    from public API attributes. Per-user FSM tokens expire after 15 minutes. Selecting
    a choice revalidates its parent/variant relationship before saving a price; later
    refreshes use the variant ID. Parent minimums are never recorded as variant prices.
12. **Amazon access gate.** Creators API uses LwA 3.x and OffersV2 through fixed endpoints.
    Configuration requires both API credentials and separately approved tracking use.
    Retailer-specific retention/display terms remain an activation prerequisite.

13. **Billing transactions.** Versioned immutable catalog prices and persistent checkout
    intents bind user/product/amount. Pre-checkout never grants access. Ledger uniqueness,
    one subscription per intent and a per-user row lock make duplicate/concurrent
    successful payments idempotent. Each charge has its own paid period; refunding one
    period never refunds another. Ledger UPDATE/DELETE is rejected at the database level.
    See [M2 design](m2-design.md) and [billing operations](billing.md).
14. **Payment delivery and external operations.** A PostgreSQL inbox is committed before
    polling acknowledgement; webhook persistence failures return an error. ARQ retries
    stored pending updates. Financial handlers bypass Redis conversation locks. Provider
    calls run outside transactions. Persisted operation claims distinguish confirmed
    refunds/cancellations from uncertainty. Reconciliation reports ambiguous purchases
    without granting access and can explicitly apply fully matched remote refunds.
15. **Downgrade behavior.** Never delete trackers/history on subscription expiration.
    Oldest enabled trackers within the effective quota remain eligible; others are
    visibly quota-paused. Shared offers use the fastest eligible plan interval. Paid
    notification rules are checked at configuration, event generation and delivery.
