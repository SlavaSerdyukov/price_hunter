import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.methods import EditUserStarSubscription, RefundStarPayment
from aiogram.types import StarTransaction
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BillingOperation,
    BillingProductRecord,
    BillingUpdate,
    CheckoutIntent,
    PaymentEvent,
    PriceObservation,
    Subscription,
    SubscriptionPeriod,
    Tracker,
)
from pricehunter.domain.billing import StarsPayment, checkout_payload
from pricehunter.domain.errors import (
    BillingOperationPendingError,
    BillingUnavailableError,
    FeatureRequiresUpgradeError,
    PaymentRejectedError,
    RateLimitExceededError,
    RenewalCancellationRequiredError,
    SubscriptionConflictError,
    SubscriptionLimitReachedError,
)
from pricehunter.domain.subscriptions import Plan
from pricehunter.schemas.api import TrackerCreate, TrackerPatch
from pricehunter.services.billing_intake import BillingIntake
from pricehunter.services.billing_service import BillingService
from pricehunter.services.entitlement_service import EntitlementService
from tests.support import grant_plan
from tests.telegram import FakeTelegramSession

pytestmark = pytest.mark.integration


@pytest.fixture
async def billing(container):
    container.settings.stars_billing_enabled = True
    transport = FakeTelegramSession()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=transport)
    container.payment_provider.bot = bot
    return container, transport


async def checkout(billing, telegram_id=123, plan=Plan.PRO):
    c, _ = billing
    user = await c.users.telegram(telegram_id)
    order = await c.billing.create_checkout(user.id, plan, title="Plan", description="30 days")
    payload = checkout_payload(order.intent_id)
    await c.billing.precheckout(
        telegram_id, payload, "XTR", order.product.stars, str(order.intent_id)
    )
    payment = StarsPayment(
        telegram_id,
        "charge-" + uuid4().hex,
        payload,
        "XTR",
        order.product.stars,
        utcnow(),
        utcnow() + timedelta(days=30),
        True,
        True,
    )
    return user, order, payment


async def purchased(billing, telegram_id=123, plan=Plan.PRO):
    user, order, payment = await checkout(billing, telegram_id, plan)
    await billing[0].billing.process_successful_payment(payment)
    return user, order, payment


async def test_concurrent_purchase_is_atomic_idempotent_and_restart_safe(billing):
    c, _ = billing
    user, order, payment = await checkout(billing)
    assert (await c.entitlements.for_user(user.id)).plan == Plan.FREE
    results = await asyncio.gather(
        *(c.billing.process_successful_payment(payment) for _ in range(8))
    )
    assert sum(result.applied for result in results) == 1
    async with c.sessions() as s:
        for model in (PaymentEvent, Subscription, SubscriptionPeriod):
            assert await s.scalar(select(func.count()).select_from(model)) == 1
        assert (await s.get(CheckoutIntent, order.intent_id)).status == "paid"
    fresh = EntitlementService(c.sessions, c.policy)
    assert (await fresh.for_user(user.id)).plan == Plan.PRO
    assert (await fresh.for_user(user.id)).valid_until == payment.expiration
    again = BillingService(c.sessions, c.settings, c.catalog, fresh, c.payment_provider)
    assert not (await again.process_successful_payment(payment)).applied


@pytest.mark.parametrize(
    "mutate",
    [
        {"currency": "EUR"},
        {"amount": 1},
        {"amount": True},
        {"telegram_user_id": 999},
        {"payload": "ph2:" + "0" * 32},
        {"payload": "pro"},
        {"recurring": False},
        {"charge_id": ""},
    ],
)
async def test_payment_rejects_tampered_contract_without_entitlement(billing, mutate):
    c, _ = billing
    user, _, payment = await checkout(billing)
    with pytest.raises(PaymentRejectedError):
        await c.billing.process_successful_payment(replace(payment, **mutate))
    assert (await c.entitlements.for_user(user.id)).plan == Plan.FREE
    async with c.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(PaymentEvent)) == 0


async def test_precheckout_rejects_foreign_wrong_currency_amount_and_reused_intent(billing):
    c, _ = billing
    user = await c.users.telegram(123)
    order = await c.billing.create_checkout(user.id, Plan.PRO, title="Pro", description="30 days")
    payload = checkout_payload(order.intent_id)
    for telegram_id, currency, amount in ((124, "XTR", 250), (123, "USD", 250), (123, "XTR", 249)):
        with pytest.raises(PaymentRejectedError):
            await c.billing.precheckout(telegram_id, payload, currency, amount, "q")
    await c.billing.precheckout(123, payload, "XTR", 250, "q")
    await c.billing.precheckout(123, payload, "XTR", 250, "q")
    with pytest.raises(PaymentRejectedError):
        await c.billing.precheckout(123, payload, "XTR", 250, "different-attempt")
    assert (await c.entitlements.for_user(user.id)).plan == Plan.FREE


async def test_catalog_is_versioned_and_old_subscription_renews_after_price_change(billing):
    c, _ = billing
    user, _, payment = await purchased(billing)
    c.settings.pro_price_stars = 300
    other = await c.users.telegram(124)
    with pytest.raises(BillingUnavailableError):
        await c.billing.create_checkout(other.id, Plan.PRO, title="Pro", description="30 days")
    c.settings.billing_price_version = "v2"
    order = await c.billing.create_checkout(other.id, Plan.PRO, title="Pro", description="30 days")
    assert order.product.stars == 300
    renewal = replace(
        payment,
        charge_id="renewal",
        first_recurring=False,
        paid_at=payment.paid_at + timedelta(days=29),
        expiration=payment.expiration + timedelta(days=30),
    )
    await c.billing.process_successful_payment(renewal)
    assert (await c.entitlements.for_user(user.id)).valid_until == renewal.expiration


async def test_renewal_uses_telegram_expiry_not_blind_period_increment(billing):
    c, _ = billing
    user, _, payment = await purchased(billing)
    t1 = payment.expiration
    t2 = t1 + timedelta(days=30)
    renewal = replace(
        payment,
        charge_id="renewal",
        first_recurring=False,
        paid_at=t1 - timedelta(hours=1),
        expiration=t2,
    )
    await c.billing.process_successful_payment(renewal)
    assert (await c.entitlements.for_user(user.id)).valid_until == t2
    assert not (await c.billing.process_successful_payment(renewal)).applied
    assert (await c.entitlements.for_user(user.id)).valid_until == t2
    with pytest.raises(PaymentRejectedError):
        await c.billing.process_successful_payment(replace(payment, charge_id="second-initial"))
    async with c.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Subscription)) == 1
        assert await s.scalar(select(func.count()).select_from(PaymentEvent)) == 2


async def test_exact_expiry_is_free_even_when_row_status_remains_active(container):
    user = await container.users.telegram(123)
    until = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    identity = await grant_plan(container, user.id, until=until)
    # Explicit required scenario: maintenance has not touched the active row.
    now = datetime(2026, 10, 19, 12, 0, 1, tzinfo=UTC)
    assert (await container.entitlements.for_user(user.id, now=now)).plan == Plan.FREE
    async with container.sessions() as s:
        assert (await s.get(Subscription, identity)).status == "active"


async def test_refund_appends_event_preserves_purchase_and_is_idempotent(billing):
    c, transport = billing
    user, _, payment = await purchased(billing)
    assert await c.billing.refund_stars(123, payment.charge_id)
    assert not await c.billing.refund_stars(123, payment.charge_id)
    assert (await c.entitlements.for_user(user.id)).plan == Plan.FREE
    assert (await c.entitlements.for_user(user.id)).status == "refunded"
    assert sum(isinstance(call, RefundStarPayment) for call in transport.calls) == 1
    async with c.sessions() as s:
        events = list(await s.scalars(select(PaymentEvent).order_by(PaymentEvent.created_at)))
        assert [event.event_type for event in events] == ["successful_payment", "refund"]
        assert events[0].amount == 250 and events[0].status == "paid"
        assert (await s.scalars(select(Subscription))).one().auto_renew is False


async def test_refunding_older_period_does_not_revoke_current_renewal(billing):
    c, _ = billing
    user, _, payment = await purchased(billing)
    renewal = replace(
        payment,
        charge_id="renewal",
        first_recurring=False,
        paid_at=payment.expiration - timedelta(hours=1),
        expiration=payment.expiration + timedelta(days=30),
    )
    await c.billing.process_successful_payment(renewal)
    assert await c.billing.record_refund(
        123, payment.charge_id, amount=250, payload=payment.payload
    )
    # There is a real gap before the later paid period; do not fill it using aggregate bounds.
    assert (
        await c.entitlements.for_user(user.id, now=payment.paid_at + timedelta(days=1))
    ).plan == Plan.FREE
    current = await c.entitlements.for_user(user.id, now=payment.expiration + timedelta(seconds=1))
    assert current.plan == Plan.PRO and current.valid_until == renewal.expiration


async def test_refunding_future_renewal_restores_original_deadline(billing):
    c, _ = billing
    user, _, payment = await purchased(billing)
    renewal = replace(
        payment,
        charge_id="renewal",
        first_recurring=False,
        paid_at=payment.expiration - timedelta(hours=1),
        expiration=payment.expiration + timedelta(days=30),
    )
    await c.billing.process_successful_payment(renewal)
    await c.billing.record_refund(123, "renewal", amount=250)
    assert (await c.entitlements.for_user(user.id)).valid_until == payment.expiration


async def test_upgrade_requires_cancel_and_cancel_preserves_paid_time(billing):
    c, transport = billing
    user, _, payment = await purchased(billing)
    with pytest.raises(SubscriptionConflictError):
        await c.billing.create_checkout(user.id, Plan.PRO, title="Pro", description="30 days")
    with pytest.raises(RenewalCancellationRequiredError):
        await c.billing.create_checkout(user.id, Plan.POWER, title="Power", description="30 days")
    effective = await c.entitlements.for_user(user.id)
    await c.billing.cancel_renewal(user.id, effective.subscription_id)
    await c.billing.cancel_renewal(user.id, effective.subscription_id)
    assert (await c.entitlements.for_user(user.id)).valid_until == payment.expiration
    assert sum(isinstance(call, EditUserStarSubscription) for call in transport.calls) == 1
    _, _, higher = await purchased(billing, plan=Plan.POWER)
    assert (await c.entitlements.for_user(user.id)).plan == Plan.POWER
    await c.billing.record_refund(123, higher.charge_id, amount=750)
    assert (await c.entitlements.for_user(user.id)).plan == Plan.PRO


async def test_ambiguous_refund_is_not_automatically_repeated(billing):
    c, transport = billing
    _, _, payment = await purchased(billing)
    transport.failures[RefundStarPayment] = TimeoutError("outcome unknown")
    with pytest.raises(BillingOperationPendingError):
        await c.billing.refund_stars(123, payment.charge_id)
    with pytest.raises(BillingOperationPendingError):
        await c.billing.refund_stars(123, payment.charge_id)
    assert sum(isinstance(call, RefundStarPayment) for call in transport.calls) == 1
    async with c.sessions() as s:
        operation = await s.scalar(
            select(BillingOperation).where(BillingOperation.kind == "refund")
        )
        assert operation.status == "uncertain"


async def test_financial_ledger_and_contract_terms_are_immutable(billing):
    c, _ = billing
    _, order, _ = await purchased(billing)
    for statement in (
        text("UPDATE payment_events SET amount=1"),
        text("DELETE FROM payment_events"),
        update(BillingProductRecord).values(stars=1),
        update(CheckoutIntent)
        .where(CheckoutIntent.id == order.intent_id)
        .values(expected_amount=1),
    ):
        with pytest.raises(DBAPIError):
            async with c.sessions.begin() as s:
                await s.execute(statement)


async def test_durable_intake_recovers_transient_failure_after_restart(billing, monkeypatch):
    c, _ = billing
    user, _, payment = await checkout(billing)
    identity = await c.billing_intake.receive(payment)
    original = c.billing.process_successful_payment

    async def unavailable(*args):
        raise OSError("database temporarily unavailable")

    monkeypatch.setattr(c.billing, "process_successful_payment", unavailable)
    assert await c.billing_intake.process(identity) is None
    assert (await c.entitlements.for_user(user.id)).plan == Plan.FREE
    monkeypatch.setattr(c.billing, "process_successful_payment", original)
    async with c.sessions.begin() as s:
        await s.execute(update(BillingUpdate).values(available_at=utcnow() - timedelta(seconds=1)))
    fresh = BillingIntake(c.sessions, c.billing)
    assert await fresh.retry_pending() == 1
    assert (await c.entitlements.for_user(user.id)).plan == Plan.PRO
    assert await fresh.receive(payment) == identity
    assert await fresh.process(identity) is None


def transaction(payment, *, refund=False, amount=None):
    return StarTransaction.model_validate(
        {
            "id": payment.charge_id,
            "amount": amount or payment.amount,
            "date": int(payment.paid_at.timestamp()),
            "receiver" if refund else "source": {
                "type": "user",
                "transaction_type": "invoice_payment",
                "user": {"id": payment.telegram_user_id, "is_bot": False, "first_name": "Test"},
                "invoice_payload": payment.payload,
                "subscription_period": 2592000,
            },
        }
    )


async def test_reconcile_dry_run_and_explicit_refund_apply(billing):
    c, transport = billing
    user, _, payment = await purchased(billing)
    transport.transactions = [transaction(payment), transaction(payment, refund=True)]
    report = await c.reconciliation.scan()
    assert report.applied_refunds == 0 and report.complete
    assert report.findings[0]["kind"] == "refund_missing_local"
    assert (await c.entitlements.for_user(user.id)).plan == Plan.PRO
    report = await c.reconciliation.scan(apply_refunds=True)
    assert report.applied_refunds == 1
    assert (await c.entitlements.for_user(user.id)).plan == Plan.FREE
    assert (await c.reconciliation.scan(apply_refunds=True)).applied_refunds == 0


async def test_reconcile_detects_missing_and_mismatch_without_granting_access(billing):
    c, transport = billing
    user, _, payment = await checkout(billing)
    transport.transactions = [transaction(payment)]
    report = await c.reconciliation.scan()
    assert report.findings[0]["kind"] == "payment_missing_local"
    assert (await c.entitlements.for_user(user.id)).plan == Plan.FREE
    transport.transactions = [transaction(payment, amount=1)]
    assert (await c.reconciliation.scan()).findings[0]["kind"] == "amount_or_owner_mismatch"
    await c.billing.process_successful_payment(payment)
    transport.transactions = []
    assert (await c.reconciliation.scan()).findings[0]["kind"] == "missing_remote"
    assert (await c.reconciliation.scan(offset=100)).findings == []


async def test_free_feature_gates_search_quota_and_bounded_history(container):
    c = container
    user = await c.users.telegram(123)
    offer = await c.products.resolve("https://mock.pricehunter.test/products/headphones", user.id)
    with pytest.raises(FeatureRequiresUpgradeError):
        await c.trackers.create(
            user.id, TrackerCreate(store_offer_id=offer.id, target_price=Decimal(90))
        )
    tracker = await c.trackers.create(user.id, TrackerCreate(store_offer_id=offer.id))
    for patch in (
        TrackerPatch(target_price=Decimal(90)),
        TrackerPatch(notify_on_target=True),
        TrackerPatch(notify_on_back_in_stock=True),
        TrackerPatch(notify_on_historical_low=True),
    ):
        with pytest.raises(FeatureRequiresUpgradeError):
            await c.trackers.update(user.id, tracker.id, patch)
    await c.trackers.update(
        user.id, tracker.id, TrackerPatch(target_price=None, notify_on_target=False)
    )
    for _ in range(3):
        result = await c.search.search("", user.id)
        assert len(result.products) <= 3
    with pytest.raises(RateLimitExceededError):
        await c.search.search("", user.id)
    async with c.sessions.begin() as s:
        s.add(
            PriceObservation(
                store_offer_id=offer.id,
                refresh_key="old",
                price=Decimal(1),
                currency="EUR",
                availability="in_stock",
                checked_at=utcnow() - timedelta(days=10),
            )
        )
    history = await c.products.history(offer.id, user.id)
    assert history.count == 1 and history.minimum == 100 and history.retention_days == 7
    await grant_plan(c, user.id)
    assert (await c.products.history(offer.id, user.id)).count == 2


async def test_expiry_preserves_trackers_but_limits_scheduling_and_alerts(billing, monkeypatch):
    c, _ = billing
    user, _, payment = await purchased(billing)
    slugs = ["headphones", "coffee-machine", "sneakers-42", "camera"]
    offers = [
        await c.products.resolve("https://mock.pricehunter.test/products/" + slug, user.id)
        for slug in slugs
    ]
    trackers = [
        await c.trackers.create(user.id, TrackerCreate(store_offer_id=o.id)) for o in offers
    ]
    after = payment.expiration + timedelta(seconds=1)
    for module in (
        "entitlement_service",
        "price_check_service",
        "tracking_service",
        "notification_service",
    ):
        monkeypatch.setattr("pricehunter.services." + module + ".utcnow", lambda: after)
    assert (await c.entitlements.for_user(user.id)).plan == Plan.FREE
    items = await c.trackers.list(user.id, size=10)
    assert len(items) == 4 and sum(t.scheduled for t in items) == 2
    resumed = await c.trackers.update(user.id, trackers[-1].id, TrackerPatch(enabled=True))
    assert not resumed.scheduled
    assert [t.id for t in items if t.scheduled] == [t.id for t in trackers[:2]]
    with pytest.raises(SubscriptionLimitReachedError):
        extra = await c.products.resolve("https://mock.pricehunter.test/products/keyboard", user.id)
        await c.trackers.create(user.id, TrackerCreate(store_offer_id=extra.id))
    claims = await c.price_checks.claim_due()
    assert {claim.offer_id for claim in claims} == {offer.id for offer in offers[:2]}
    for claim in claims:
        assert await c.price_checks.refresh(claim)
    async with c.sessions() as s:
        from pricehunter.db.models import NotificationEvent

        events = list(await s.scalars(select(NotificationEvent)))
        assert events and all(event.event_type == "price_drop" for event in events)
        assert await s.scalar(select(func.count()).select_from(Tracker)) == 4


async def test_concurrent_checkout_reuses_single_persistent_contract(billing):
    c, _ = billing
    user = await c.users.telegram(123)
    orders = await asyncio.gather(
        *(
            c.billing.create_checkout(user.id, Plan.PRO, title="Pro", description="30 days")
            for _ in range(4)
        )
    )
    assert len({order.intent_id for order in orders}) == 1
    async with c.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(CheckoutIntent)) == 1


async def test_expired_unapproved_and_retired_invoice_cannot_start_access(billing):
    c, _ = billing
    user = await c.users.telegram(123)
    order = await c.billing.create_checkout(user.id, Plan.PRO, title="Pro", description="30 days")
    payload = checkout_payload(order.intent_id)
    payment = StarsPayment(
        123, "unapproved", payload, "XTR", 250, utcnow(), utcnow() + timedelta(days=30), True, True
    )
    with pytest.raises(PaymentRejectedError):
        await c.billing.process_successful_payment(payment)
    async with c.sessions.begin() as s:
        await s.execute(update(CheckoutIntent).values(expires_at=utcnow() - timedelta(seconds=1)))
    with pytest.raises(PaymentRejectedError):
        await c.billing.precheckout(123, payload, "XTR", 250, "expired")
    order = await c.billing.create_checkout(user.id, Plan.PRO, title="Pro", description="30 days")
    async with c.sessions.begin() as s:
        await s.execute(update(BillingProductRecord).values(active=False))
    with pytest.raises(PaymentRejectedError):
        await c.billing.precheckout(123, checkout_payload(order.intent_id), "XTR", 250, "retired")


async def test_delayed_approved_payment_honored_after_checkout_ttl(billing, monkeypatch):
    c, _ = billing
    user, _, payment = await checkout(billing)
    monkeypatch.setattr(
        "pricehunter.services.billing_service.utcnow", lambda: payment.paid_at + timedelta(hours=1)
    )
    await c.billing.process_successful_payment(payment)
    assert (await c.entitlements.for_user(user.id)).valid_until == payment.expiration


async def test_renewal_before_initial_is_retained_and_retried(billing):
    c, _ = billing
    user, _, payment = await checkout(billing)
    renewal = replace(
        payment,
        charge_id="renewal-before-first",
        first_recurring=False,
        paid_at=payment.expiration,
        expiration=payment.expiration + timedelta(days=30),
    )
    identity = await c.billing_intake.receive(renewal)
    assert await c.billing_intake.process(identity) is None
    assert await c.billing_intake.status(identity) == "pending"
    await c.billing.process_successful_payment(payment)
    assert (await c.billing_intake.process(identity)).applied
    assert (await c.entitlements.for_user(user.id)).valid_until == renewal.expiration


async def test_refund_concurrency_and_owner_validation(billing):
    c, _ = billing
    user, _, payment = await purchased(billing)
    other = await c.users.telegram(124)
    with pytest.raises(PaymentRejectedError):
        await c.billing.refund_stars(124, payment.charge_id)
    current = await c.entitlements.for_user(user.id)
    with pytest.raises(PaymentRejectedError):
        await c.billing.cancel_renewal(other.id, current.subscription_id)
    results = await asyncio.gather(
        *(c.billing.record_refund(123, payment.charge_id, amount=250) for _ in range(5))
    )
    assert sum(results) == 1
    async with c.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(PaymentEvent)) == 2


async def test_ambiguous_refund_can_be_reconciled_without_second_money_movement(billing):
    c, transport = billing
    _, _, payment = await purchased(billing)
    transport.failures[RefundStarPayment] = TimeoutError("reply lost after refund")
    with pytest.raises(BillingOperationPendingError):
        await c.billing.refund_stars(123, payment.charge_id)
    transport.transactions = [transaction(payment), transaction(payment, refund=True)]
    assert (await c.reconciliation.scan(apply_refunds=True)).applied_refunds == 1
    assert not await c.billing.refund_stars(123, payment.charge_id)
    assert sum(isinstance(call, RefundStarPayment) for call in transport.calls) == 1
    async with c.sessions() as s:
        row = await s.scalar(select(BillingOperation).where(BillingOperation.kind == "refund"))
        assert row.status == "completed"


async def test_cancellation_timeout_retries_idempotent_remote_setting(billing):
    c, transport = billing
    user, _, payment = await purchased(billing)
    subscription_id = (await c.entitlements.for_user(user.id)).subscription_id
    transport.failures[EditUserStarSubscription] = TimeoutError("unknown cancellation result")
    with pytest.raises(BillingOperationPendingError):
        await c.billing.cancel_renewal(user.id, subscription_id)
    assert (await c.entitlements.for_user(user.id)).plan == Plan.PRO
    await c.billing.cancel_renewal(user.id, subscription_id)
    assert (await c.entitlements.for_user(user.id)).auto_renew is False
    assert (await c.entitlements.for_user(user.id)).valid_until == payment.expiration


async def test_reconciliation_pagination_is_resumable(billing):
    c, transport = billing
    _, _, payment = await purchased(billing)
    unrelated = StarTransaction.model_validate(
        {
            "id": "unrelated",
            "amount": 1,
            "date": int(utcnow().timestamp()),
            "source": {"type": "other"},
        }
    )
    transport.transactions = [unrelated] * 100 + [transaction(payment)]
    first = await c.reconciliation.scan(max_pages=1)
    assert first.scanned == 100 and not first.complete and first.next_offset == 100
    assert first.findings == []  # Partial scan does not claim remote payment is missing.
    last = await c.reconciliation.scan(offset=first.next_offset)
    assert last.scanned == 1 and last.complete and last.findings == []


async def test_cleanup_and_concurrent_renewal_cannot_remove_new_access(billing, monkeypatch):
    c, _ = billing
    user, _, payment = await purchased(billing)
    after = payment.expiration + timedelta(seconds=1)
    monkeypatch.setattr("pricehunter.services.billing_service.utcnow", lambda: after)
    renewal = replace(
        payment,
        charge_id="renewal",
        first_recurring=False,
        paid_at=payment.expiration,
        expiration=payment.expiration + timedelta(days=30),
    )
    await asyncio.gather(c.billing.expire(), c.billing.process_successful_payment(renewal))
    effective = await c.entitlements.for_user(user.id, now=after)
    assert effective.plan == Plan.PRO and effective.valid_until == renewal.expiration


async def test_paid_alert_queued_before_expiry_is_gated_at_delivery(billing, monkeypatch):
    from pricehunter.db.models import NotificationEvent
    from pricehunter.services.notification_service import NotificationService
    from tests.integration.test_tracking import FakeSender

    c, _ = billing
    user, _, payment = await purchased(billing)
    offer = await c.products.resolve("https://mock.pricehunter.test/products/headphones", user.id)
    await c.trackers.create(
        user.id, TrackerCreate(store_offer_id=offer.id, target_price=Decimal(96))
    )
    await c.price_checks.refresh((await c.price_checks.claim_due())[0])
    after = payment.expiration + timedelta(seconds=1)
    monkeypatch.setattr("pricehunter.services.notification_service.utcnow", lambda: after)
    monkeypatch.setattr("pricehunter.services.entitlement_service.utcnow", lambda: after)
    sender = FakeSender()
    assert await NotificationService(c.sessions, sender, c.entitlements).send_pending() == 0
    assert sender.deliveries == []
    async with c.sessions() as s:
        assert (await s.scalars(select(NotificationEvent))).one().status == "cancelled"


async def test_real_store_interval_recalculates_on_expiry_and_shared_paid_user(
    billing, monkeypatch
):
    from pricehunter.db.models import Store, StoreOffer

    c, _ = billing
    user, _, payment = await purchased(billing)
    offer = await c.products.resolve("https://mock.pricehunter.test/products/headphones", user.id)
    await c.trackers.create(user.id, TrackerCreate(store_offer_id=offer.id))
    async with c.sessions.begin() as s:
        await s.execute(update(Store).values(provider_type="test_real"))
        await s.execute(
            update(StoreOffer).values(
                refresh_sequence=1,
                last_checked_at=payment.expiration - timedelta(hours=3),
                next_check_at=payment.expiration - timedelta(hours=1),
            )
        )
    after = payment.expiration + timedelta(seconds=1)
    monkeypatch.setattr("pricehunter.services.price_check_service.utcnow", lambda: after)
    monkeypatch.setattr("pricehunter.services.entitlement_service.utcnow", lambda: after)
    assert (
        await c.price_checks.claim_due() == []
    )  # Free 12 h; stored Pro interval cannot authorize a check.
    second = await c.users.telegram(124)
    await grant_plan(c, second.id, until=after + timedelta(days=30))
    await c.trackers.create(second.id, TrackerCreate(store_offer_id=offer.id))
    assert len(await c.price_checks.claim_due()) == 1  # One offer, fastest eligible subscriber.


async def test_concurrent_precheckout_attempts_approve_only_one_query(billing):
    c, _ = billing
    user = await c.users.telegram(123)
    order = await c.billing.create_checkout(user.id, Plan.PRO, title="Pro", description="30 days")
    outcomes = await asyncio.gather(
        *(
            c.billing.precheckout(
                123, checkout_payload(order.intent_id), "XTR", 250, f"attempt-{i}"
            )
            for i in range(8)
        ),
        return_exceptions=True,
    )
    assert sum(result is None for result in outcomes) == 1
    assert sum(isinstance(result, PaymentRejectedError) for result in outcomes) == 7
    assert (await c.entitlements.for_user(user.id)).plan == Plan.FREE


async def test_operator_commands_use_services_without_public_mutation_endpoints(
    billing, monkeypatch, capsys
):
    from argparse import Namespace
    from unittest.mock import AsyncMock

    from pricehunter.apps import admin

    c, transport = billing
    user, _, payment = await purchased(billing)
    monkeypatch.setattr(admin, "Container", lambda settings: c)
    monkeypatch.setattr(admin, "get_settings", lambda: c.settings)
    monkeypatch.setattr(c, "close", AsyncMock())
    await admin.run(Namespace(command="subscription", user_id=user.id))
    assert '"plan": "pro"' in capsys.readouterr().out
    await admin.run(Namespace(command="stars-balance"))
    assert '"amount":123' in capsys.readouterr().out
    transport.transactions = [transaction(payment)]
    await admin.run(
        Namespace(command="reconcile-stars", offset=0, max_pages=1, apply_refunds=False)
    )
    assert '"findings": []' in capsys.readouterr().out
    await admin.run(
        Namespace(command="refund-stars", telegram_user_id=123, charge_id=payment.charge_id)
    )
    assert "Refund recorded" in capsys.readouterr().out
    assert (await c.entitlements.for_user(user.id)).plan == Plan.FREE
