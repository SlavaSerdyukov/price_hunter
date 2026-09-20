# Telegram Stars subscriptions (M2)

PriceHunter is a digital service. Its Telegram checkout uses **Telegram Stars (XTR)**.
Stripe and Wallet Pay remain disabled scaffolds. There is no external checkout route.
The defaults below are configurable; prices are Stars, not EUR equivalents.

| Capability | Free | Pro | Power |
| --- | --- | --- | --- |
| Price per 30 days | 0 | 250 Stars | 750 Stars |
| Stored tracker limit when adding | 2 | 50 | 250 |
| Real-store refresh interval | 12 h | 2 h | 1 h |
| Basic price-drop alerts | Yes | Yes | Yes |
| Target / historical-low / restock alerts | No | Yes | Yes |
| Search requests per 24 h window | 3 | 30 | 100 |
| Results per search | 3 | 15 | 50 |
| Accessible history | 7 days | 90 days | 365 days |

History access limits do not delete data. The separate operator retention policy may
make less history available. Demo offers use `MOCK_CHECK_INTERVAL_SECONDS` on all plans.
Store availability, retailer quotas and failures can delay checks; intervals are not a
promise of instant price delivery. Shared offers still use the fastest eligible tracker.

## Configuration and startup

```dotenv
STARS_BILLING_ENABLED=false
BILLING_PRICE_VERSION=v1
PRO_PRICE_STARS=250
POWER_PRICE_STARS=750
CHECKOUT_TTL_SECONDS=900
SUPPORT_CONTACT=@slavasham
```

Set `STARS_BILLING_ENABLED=true` to expose checkout buttons. A bot token is required.
Disabling checkout stops new invoices/pre-checkout approvals; it does not discard an
already-paid update, disable processing of renewals/refunds, or remove paid access.
Configure `FREE_*`, `PRO_*`, `POWER_*` and `PLAN_FEATURES` in `.env.example` as needed.
Settings are read when each process starts.

Stop old application processes before applying migration `0bc58a691a8b` because it
removes `users.subscription_plan`. PostgreSQL and Redis can remain running. Build the
new image first, back up PostgreSQL, then migrate and restart API/bot/worker together:

```bash
docker compose --profile app build api bot worker
docker compose --profile app stop api bot worker
docker compose --profile app run --rm api alembic upgrade head
docker compose --profile app run --rm api alembic check
docker compose --profile app up -d api bot worker
```

For local Python, use `uv run alembic upgrade head` and `uv run alembic check` with the
correct `DATABASE_URL`. Never point integration tests at the live application database.

## Lifecycle and user controls

- `/plans` shows actual configured prices, limits, enabled features and the current plan.
- Upgrade buttons create an opaque, persistent checkout intent and an invoice **link**.
  Recurring subscriptions use `createInvoiceLink`, XTR, one price, an empty provider token
  and a 2592000-second period. Opening the invoice does not activate access.
- Pre-checkout validates ownership, the persisted catalog/version, currency, exact amount,
  expiry and current subscription. Only one pre-checkout attempt can consume an intent;
  retrying the same query is safe. Validation is capped at 5 s, answering at 3 s, and
  financial updates bypass conversation/FSM locks. Telegram's response deadline is 10 s.
- A successful recurring payment commits the financial event, subscription and paid period
  atomically. PostgreSQL uniqueness plus a per-user lock handle concurrent duplicates.
  The initial payment must correspond to an approved checkout. A delayed delivery is
  judged using the payment timestamp, not the later processing time.
- Renewals reuse the original paid intent and catalog price. Each distinct charge creates
  one paid period, not another subscription. The Telegram expiration is authoritative;
  it is never incremented again on replay. If absent, the conservative fallback is the
  charge timestamp plus 30 days, recorded as `payment_timestamp` in event metadata.
- `/subscription` shows effective plan, expiry, stored/scheduled tracker counts, and last
  known renewal state. Telegram can change renewal outside the bot, so the UI explicitly
  describes the state as last observed rather than asserting it is still enabled.
- Cancellation uses `editUserStarSubscription` after confirmation. Paid periods survive.
  A Pro → Power upgrade requires cancelling Pro renewal first, then purchasing Power.
  Power starts immediately at full price with no proration; any Pro period stays recorded.
  If Power is refunded/ends first, still-valid Pro access can become effective again.
- Expiration is checked during authorization and scheduling, even if the maintenance job
  is late. The oldest **enabled** trackers up to the new quota remain scheduled. All other
  trackers, targets, preferences and history stay stored. `/my` marks quota-paused items.
  A later upgrade makes them eligible again. New trackers still count against the stored
  tracker limit, so old data must be removed or the plan upgraded before adding more.
- Paid alert rules are gated both when changed and when events are generated/delivered.
  Clearing an old target or turning a rule off is always allowed.
- `/paysupport` directs users to the configured support contact with date/plan/Telegram
  receipt. There is no public refund button or unauthenticated payment mutation API.

## Durable intake and reconciliation

The bot persists only necessary financial update fields in PostgreSQL before acknowledging
polling offsets. Slow product lookups run separately. Webhook intake failures propagate as
HTTP errors so Telegram can retry. Once stored, transient processing failures remain
pending with backoff; the ARQ billing maintenance job retries them every minute. Invalid
contracts become rejected and require investigation. Restarting a process does not lose
pending paid events. Confirmation messages are best-effort; `/subscription` reads durable
state even if Telegram message delivery fails.

`PaymentEvent` is append-only: database triggers reject UPDATE/DELETE, and financial
foreign keys prevent casual account deletion from erasing the ledger. Catalog prices
and checkout ownership/amount/product are also protected against mutation. Subscription
periods record refunds separately so a refund cannot erase a different paid renewal.
Logs contain lifecycle transitions and hashed charge references, never bot tokens or
complete invoice payloads.

Local operator commands (not HTTP endpoints):

```bash
uv run python -m pricehunter.apps.admin subscription USER_UUID
uv run python -m pricehunter.apps.admin stars-balance
uv run python -m pricehunter.apps.admin reconcile-stars
uv run python -m pricehunter.apps.admin reconcile-stars --offset 100 --max-pages 10
uv run python -m pricehunter.apps.admin reconcile-stars --apply-refunds
uv run python -m pricehunter.apps.admin billing-retry
uv run python -m pricehunter.apps.admin cancel-stars --user-id USER_UUID --subscription-id SUBSCRIPTION_UUID
uv run python -m pricehunter.apps.admin refund-stars --telegram-user-id TELEGRAM_ID --charge-id TELEGRAM_CHARGE_ID
```

A refund first stops future renewal, then calls `refundStarPayment`. After confirmation,
the original purchase remains and one refund event is appended. Its period stops granting
access; remaining unrefunded periods are recalculated. Duplicate local refund requests
have no second financial effect. An ambiguous refund timeout is **not** automatically
retried. Run reconciliation to check Telegram and apply a fully matched remote refund;
if no refund appears, investigate with Telegram support before changing operation state.
Cancellation is a repeatable setting operation and can safely be retried after uncertainty.

Reconciliation is dry-run by default. It reports missing local/remote payments, refunds,
unknown payloads, wrong users, amounts and products. `next_offset` permits paginated scans;
a partial scan never claims a local event is missing remotely. Offsets are not a stable
snapshot while new transactions arrive: rerun/overlap pages when investigating a mismatch.
`--apply-refunds` only records refunds that exactly match an existing purchase, owner,
product, amount and payload; it never invokes another refund API call.

Transaction history does not expose all authoritative renewal/expiration fields. Missing
purchases are therefore reported for investigation, **never automatically turned into
paid access**. Replaying a known authentic successful-payment update through the durable
intake is safe; do not manufacture expiry dates from a history row.

## Price changes

Change the price **and** increase `BILLING_PRICE_VERSION` before restarting. The catalog
stores the earlier version and amount for existing subscriptions and their renewals.
Reusing a version with a different amount is rejected. Old pending invoices cannot be
newly approved after a catalog-version change; an already-approved paid event is honored.
There is no automatic repricing of an existing Telegram recurring subscription.

## Tests and remaining live verification

CI uses a simulated aiogram transport, disposable PostgreSQL and Redis database 15; it
never spends or refunds real Stars. The same handlers/services are exercised as runtime.
See [verification](verification.md) for executed checks.

Before a public commercial launch, use a separate Telegram test environment/test bot to
verify the actual invoice interface, initial purchase, recurring delivery, bot cancellation,
refund and transaction-history mapping. A small deliberate production payment/refund is
an operator action; none was performed by this implementation. Genuine future recurring
renewal still needs observation. Telegram's [digital payments guide](https://core.telegram.org/bots/payments-stars)
and [Bot API](https://core.telegram.org/bots/api#createinvoicelink) are the provider references.

Migration rollback refuses to erase an M2 paid ledger. After accepting real payments, use
forward migrations and tested backups; do not downgrade or truncate financial tables.
