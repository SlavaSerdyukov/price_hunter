# M2 implementation design

The existing monolith, provider registry, offer scheduler and notification outbox remain.
Billing is implemented in application services with a Telegram transport adapter.

- **Authority:** subscriptions and their paid, unrefunded periods determine access.
  Remove the unused `users.subscription_plan` column in a new migration: only three
  readers exist and no M1 checkout writes it. Existing subscription records are retained.
  `EntitlementService` evaluates UTC boundaries on every authorization; cleanup is optional.
- **Catalog and checkout:** persist immutable versioned catalog prices and opaque UUID
  checkout intents. `ph2:<uuid>` contains no client-controlled plan or price. A finite
  checkout lifetime and pre-checkout approval protect first purchases. Recurring charges
  reuse the paid intent, but must have distinct charge IDs and recurring semantics.
- **Ledger:** purchase/refund events are append-only, unique by provider/event identity
  and charge/event type. One subscription per intent, with individual paid periods so
  refunding an old period cannot revoke a later paid period or accidentally fill a gap.
  All ledger, period and subscription changes commit under a per-user PostgreSQL lock.
- **Provider:** `createInvoiceLink` with XTR, one price and 2592000-second recurrence;
  no provider token. Installed aiogram 3.31 exposes invoice, refund, renewal-management,
  transaction-history and balance methods. No Stripe/Wallet Pay checkout is activated.
- **Renewal:** prefer Telegram's expiration date. Never increment it for duplicate
  delivery. A missing expiration uses the payment's timestamp plus the configured
  Telegram period and is identified in metadata. Process delayed events without losing
  later periods. Bot cancellation preserves paid access; renewal status describes only
  the last known Telegram state, not a guarantee about the user's current settings.
- **Upgrade:** require confirmed cancellation of existing lower-plan renewal before a
  new subscription is purchased. The checkout UX explains the full new-period price,
  immediate start, overlap and lack of proration. Paid lower-plan periods remain intact.
- **Durability:** persist minimal payment update data before acknowledging it; worker
  retries transient processing failures. Polling advances offsets only after durable
  intake; webhook persistence failures return an error. No secrets/order contact data
  enter billing metadata. Pre-checkout has a bounded deadline and never grants access.
- **Downgrade:** keep all trackers, preferences and history. Schedule the oldest enabled
  trackers up to the effective quota. Compute current intervals and allowed alert rules
  from entitlements, including at dispatch. A later upgrade resumes eligible trackers.
  Search has configurable daily/result quotas; history access is bounded by plan.
- **Operations:** refunds/cancellations use persisted operations, bounded claims and
  external I/O outside database transactions. Ambiguous refunds await reconciliation;
  no automatic repeated money movement. Reconciliation is paginated and dry-run by
  default, never invents entitlement from transactions lacking authoritative expiry.
  Explicit application may record fully matched refunds. CLI diagnostics remain local.
- **Verification:** isolated PostgreSQL/Redis and simulated Telegram, including concurrent
  duplicates, early renewal, delayed updates, exact expiry, refunds, upgrades, feature
  gates and bot acceptance. No real Stars purchase/refund is performed during development.

Official references checked 2026-09-19:
[digital payments](https://core.telegram.org/bots/payments-stars),
[invoice links](https://core.telegram.org/bots/api#createinvoicelink),
[successful payment](https://core.telegram.org/bots/api#successfulpayment),
[renewal management](https://core.telegram.org/bots/api#edituserstarsubscription),
[refunds](https://core.telegram.org/bots/api#refundstarpayment),
[transaction history](https://core.telegram.org/bots/api#getstartransactions).
