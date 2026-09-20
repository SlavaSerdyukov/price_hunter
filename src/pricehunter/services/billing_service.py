import hashlib
from datetime import timedelta
from uuid import UUID, uuid4

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BillingOperation,
    BillingProductRecord,
    CheckoutIntent,
    PaymentEvent,
    StoreOffer,
    Subscription,
    SubscriptionPeriod,
    Tracker,
    User,
)
from pricehunter.db.session import SessionFactory
from pricehunter.domain.billing import (
    STARS_PROVIDER,
    BillingEventType,
    Checkout,
    CheckoutStatus,
    PaymentResult,
    StarsPayment,
    checkout_payload,
    parse_checkout_payload,
)
from pricehunter.domain.errors import (
    BillingOperationPendingError,
    BillingUnavailableError,
    PaymentRejectedError,
    RenewalCancellationRequiredError,
    SubscriptionConflictError,
)
from pricehunter.domain.subscriptions import Plan, SubscriptionStatus
from pricehunter.payments.base import CheckoutContext
from pricehunter.payments.stars import TelegramStarsPaymentProvider
from pricehunter.services.billing_catalog import BillingCatalog
from pricehunter.services.entitlement_service import EntitlementService

log = structlog.get_logger()


def charge_reference(charge_id: str) -> str:
    return hashlib.sha256(charge_id.encode()).hexdigest()


def lifecycle(event: str, subscription: Subscription, charge_id: str = "") -> None:
    log.info(
        event,
        user_id=str(subscription.user_id),
        billing_product=subscription.billing_product_code,
        plan=subscription.plan,
        provider=subscription.provider,
        charge_ref=charge_reference(charge_id)[:12] if charge_id else None,
    )


class BillingService:
    def __init__(
        self,
        sessions: SessionFactory,
        settings: Settings,
        catalog: BillingCatalog,
        entitlements: EntitlementService,
        provider: TelegramStarsPaymentProvider,
    ) -> None:
        self.sessions, self.settings = sessions, settings
        self.catalog, self.entitlements, self.provider = catalog, entitlements, provider

    async def _purchase_allowed(self, session: AsyncSession, user_id: UUID, plan: Plan) -> None:
        effective = await self.entitlements.for_user(user_id, session=session)
        if self.entitlements.policy.rank(effective.plan) >= self.entitlements.policy.rank(plan):
            raise SubscriptionConflictError()
        # Even an expired subscription can still have renewal enabled remotely.
        renewable = await session.scalar(
            select(Subscription.id)
            .where(
                Subscription.user_id == user_id,
                Subscription.auto_renew.is_(True),
                Subscription.provider == STARS_PROVIDER,
            )
            .limit(1)
        )
        if renewable is not None:
            raise RenewalCancellationRequiredError()

    async def create_checkout(
        self, user_id: UUID, plan: Plan, *, title: str, description: str
    ) -> Checkout:
        if not self.settings.stars_billing_enabled:
            raise BillingUnavailableError()
        product = self.catalog.for_plan(plan)
        now = utcnow()
        async with self.sessions.begin() as session:
            user = await session.get(User, user_id, with_for_update=True)
            if user is None or user.telegram_user_id is None:
                raise PaymentRejectedError()
            await self._purchase_allowed(session, user_id, plan)
            await self.catalog.persist(session, product)
            intent = await session.scalar(
                select(CheckoutIntent).where(
                    CheckoutIntent.user_id == user_id,
                    CheckoutIntent.status == CheckoutStatus.PENDING,
                )
            )
            if intent is not None and (
                intent.expires_at <= now or intent.billing_product_code != product.code
            ):
                if intent.approved_at and intent.expires_at + timedelta(minutes=10) > now:
                    raise SubscriptionConflictError()
                intent.status = (
                    CheckoutStatus.EXPIRED if intent.expires_at <= now else CheckoutStatus.CANCELLED
                )
                await session.flush()
                intent = None
            if intent is None:
                intent = CheckoutIntent(
                    user_id=user_id,
                    billing_product_code=product.code,
                    expected_amount=product.stars,
                    expires_at=now + timedelta(seconds=self.settings.checkout_ttl_seconds),
                )
                session.add(intent)
                await session.flush()
                log.info(
                    "checkout_created",
                    user_id=str(user_id),
                    billing_product=product.code,
                    plan=plan,
                    provider=STARS_PROVIDER,
                )
            intent_id, invoice_url = intent.id, intent.invoice_url
            country = user.country_code
        if invoice_url is None:
            invoice_url = await self.provider.create_checkout(
                CheckoutContext(
                    user_id,
                    "telegram",
                    country,
                    plan,
                    payload=checkout_payload(intent_id),
                    product=product,
                    title=title,
                    description=description,
                )
            )
            async with self.sessions.begin() as session:
                await session.execute(
                    update(CheckoutIntent)
                    .where(
                        CheckoutIntent.id == intent_id,
                        CheckoutIntent.status == CheckoutStatus.PENDING,
                    )
                    .values(invoice_url=invoice_url)
                )
        return Checkout(intent_id, invoice_url, product)

    async def _contract(
        self, session: AsyncSession, telegram_id: int, payload: str, currency: str, amount: int
    ) -> tuple[User, CheckoutIntent, BillingProductRecord]:
        intent_id = parse_checkout_payload(payload)
        # Acquire the owner lock before reading mutable checkout state. A joined
        # SELECT FOR UPDATE OF users could retain an old snapshot of the intent
        # while waiting for another checkout approval to release the user lock.
        owner_id = await session.scalar(
            select(CheckoutIntent.user_id).where(CheckoutIntent.id == intent_id)
        )
        user = await session.scalar(
            select(User)
            .where(
                User.id == owner_id,
                User.telegram_user_id == telegram_id,
            )
            .with_for_update()
        )
        if user is None:
            raise PaymentRejectedError()
        intent = await session.get(CheckoutIntent, intent_id, populate_existing=True)
        if intent is None:
            raise PaymentRejectedError()
        product = await session.get(BillingProductRecord, intent.billing_product_code)
        if product is None:
            raise PaymentRejectedError()
        if (
            currency != "XTR"
            or intent.currency != currency
            or type(amount) is not int
            or amount != intent.expected_amount
            or amount != product.stars
            or product.plan not in (Plan.PRO, Plan.POWER)
        ):
            raise PaymentRejectedError()
        return user, intent, product

    async def precheckout(
        self, telegram_id: int, payload: str, currency: str, amount: int, query_id: str
    ) -> None:
        if not self.settings.stars_billing_enabled:
            raise BillingUnavailableError()
        async with self.sessions.begin() as session:
            user, intent, product = await self._contract(
                session, telegram_id, payload, currency, amount
            )
            configured = self.catalog.for_plan(Plan(product.plan))
            if (
                intent.status != CheckoutStatus.PENDING
                or intent.expires_at <= utcnow()
                or not product.active
                or configured.code != product.code
                or configured.stars != amount
                or (intent.approved_query_id is not None and intent.approved_query_id != query_id)
            ):
                raise PaymentRejectedError()
            await self._purchase_allowed(session, user.id, Plan(product.plan))
            intent.approved_at = intent.approved_at or utcnow()
            intent.approved_query_id = query_id
            log.info(
                "precheckout_approved",
                user_id=str(user.id),
                billing_product=product.code,
                provider=STARS_PROVIDER,
                plan=product.plan,
            )

    async def process_successful_payment(self, payment: StarsPayment) -> PaymentResult:
        if (
            not 1 <= len(payment.charge_id) <= 200
            or not payment.recurring
            or payment.paid_at.tzinfo is None
        ):
            raise PaymentRejectedError()
        async with self.sessions.begin() as session:
            user, intent, product = await self._contract(
                session,
                payment.telegram_user_id,
                payment.payload,
                payment.currency,
                payment.amount,
            )
            existing = await session.scalar(
                select(PaymentEvent).where(
                    PaymentEvent.provider == STARS_PROVIDER,
                    PaymentEvent.telegram_payment_charge_id == payment.charge_id,
                    PaymentEvent.event_type == BillingEventType.PURCHASE,
                )
            )
            if existing is not None:
                subscription = await session.get(Subscription, existing.subscription_id)
                if (
                    subscription is None
                    or subscription.checkout_intent_id != intent.id
                    or existing.amount != payment.amount
                    or existing.user_id != user.id
                ):
                    raise PaymentRejectedError()
                return PaymentResult(False, Plan(subscription.plan), subscription.valid_until)
            subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.checkout_intent_id == intent.id,
                )
            )
            first = subscription is None
            if first:
                if not payment.first_recurring:
                    # A renewal may arrive before its first payment; durable intake retries it.
                    raise BillingUnavailableError()
                if (
                    intent.approved_at is None
                    or intent.status not in (CheckoutStatus.PENDING, CheckoutStatus.EXPIRED)
                    or payment.paid_at > intent.expires_at + timedelta(minutes=10)
                    or payment.paid_at < intent.created_at - timedelta(minutes=1)
                ):
                    raise PaymentRejectedError()
            elif payment.first_recurring:
                raise PaymentRejectedError()  # A second initial charge cannot consume one intent.
            expiration = payment.expiration or (
                payment.paid_at + timedelta(seconds=product.subscription_period)
            )
            if expiration.tzinfo is None or expiration <= payment.paid_at:
                raise PaymentRejectedError()
            # Never invent a rolling extension from processing time or a stale database deadline.
            valid_from = (
                payment.paid_at
                if first
                else expiration - timedelta(seconds=product.subscription_period)
            )
            if subscription is None:
                subscription = Subscription(
                    user_id=user.id,
                    plan=product.plan,
                    provider=STARS_PROVIDER,
                    provider_subscription_id=str(intent.id),
                    checkout_intent_id=intent.id,
                    billing_product_code=product.code,
                    telegram_payment_charge_id=payment.charge_id,
                    status=SubscriptionStatus.ACTIVE,
                    valid_from=valid_from,
                    valid_until=expiration,
                    auto_renew=True,
                )
                session.add(subscription)
                await session.flush()
            event = PaymentEvent(
                provider=STARS_PROVIDER,
                external_event_id="pay:" + charge_reference(payment.charge_id),
                telegram_payment_charge_id=payment.charge_id,
                user_id=user.id,
                subscription_id=subscription.id,
                billing_product_code=product.code,
                plan=product.plan,
                event_type=BillingEventType.PURCHASE,
                amount=payment.amount,
                currency="XTR",
                status="paid",
                metadata_json={
                    "paid_at": payment.paid_at.isoformat(),
                    "recurring": True,
                    "first_recurring": payment.first_recurring,
                    "expiration_source": "telegram" if payment.expiration else "payment_timestamp",
                    "valid_until": expiration.isoformat(),
                },
            )
            session.add(event)
            await session.flush()
            session.add(
                SubscriptionPeriod(
                    subscription_id=subscription.id,
                    payment_event_id=event.id,
                    valid_from=valid_from,
                    valid_until=expiration,
                )
            )
            subscription.valid_from = min(subscription.valid_from, valid_from)
            subscription.valid_until = max(subscription.valid_until, expiration)
            subscription.status = (
                SubscriptionStatus.CANCELLED
                if subscription.cancelled_at
                else SubscriptionStatus.ACTIVE
            )
            subscription.auto_renew = subscription.cancelled_at is None
            intent.status, intent.consumed_at = CheckoutStatus.PAID, intent.consumed_at or utcnow()
            # Re-evaluate faster scheduling without disturbing any refresh already in flight.
            await session.execute(
                update(StoreOffer)
                .where(
                    StoreOffer.id.in_(
                        select(Tracker.store_offer_id).where(Tracker.user_id == user.id)
                    )
                )
                .values(next_check_at=func.least(StoreOffer.next_check_at, utcnow()))
            )
            result = PaymentResult(True, Plan(product.plan), subscription.valid_until)
            lifecycle("payment_recorded", subscription, payment.charge_id)
            lifecycle(
                "subscription_activated" if first else "subscription_renewed",
                subscription,
                payment.charge_id,
            )
        return result

    async def record_refund(
        self,
        telegram_id: int,
        charge_id: str,
        *,
        amount: int,
        currency: str = "XTR",
        payload: str | None = None,
    ) -> bool:
        async with self.sessions.begin() as session:
            user = await session.scalar(
                select(User).where(User.telegram_user_id == telegram_id).with_for_update()
            )
            if user is None:
                raise PaymentRejectedError()
            purchase = await session.scalar(
                select(PaymentEvent).where(
                    PaymentEvent.provider == STARS_PROVIDER,
                    PaymentEvent.telegram_payment_charge_id == charge_id,
                    PaymentEvent.event_type == BillingEventType.PURCHASE,
                )
            )
            if purchase is None:
                raise BillingUnavailableError()  # Refund update can precede purchase intake.
            if purchase.user_id != user.id or currency != "XTR" or purchase.amount != amount:
                raise PaymentRejectedError()
            subscription = await session.get(Subscription, purchase.subscription_id)
            if subscription is None:
                raise PaymentRejectedError()
            if (
                payload is not None
                and parse_checkout_payload(payload) != subscription.checkout_intent_id
            ):
                raise PaymentRejectedError()
            duplicate = await session.scalar(
                select(PaymentEvent.id).where(
                    PaymentEvent.provider == STARS_PROVIDER,
                    PaymentEvent.telegram_payment_charge_id == charge_id,
                    PaymentEvent.event_type == BillingEventType.REFUND,
                )
            )
            if duplicate:
                return False
            session.add(
                PaymentEvent(
                    provider=STARS_PROVIDER,
                    external_event_id="refund:" + charge_reference(charge_id),
                    telegram_payment_charge_id=charge_id,
                    user_id=user.id,
                    subscription_id=subscription.id,
                    billing_product_code=purchase.billing_product_code,
                    plan=purchase.plan,
                    event_type=BillingEventType.REFUND,
                    amount=amount,
                    currency="XTR",
                    status="refunded",
                    metadata_json={"purchase_event_id": str(purchase.id)},
                )
            )
            period = await session.scalar(
                select(SubscriptionPeriod).where(SubscriptionPeriod.payment_event_id == purchase.id)
            )
            if period is None:
                raise PaymentRejectedError()
            period.refunded_at = utcnow()
            await session.flush()
            remaining = list(
                await session.scalars(
                    select(SubscriptionPeriod).where(
                        SubscriptionPeriod.subscription_id == subscription.id,
                        SubscriptionPeriod.refunded_at.is_(None),
                    )
                )
            )
            subscription.refunded_at = utcnow()
            if remaining:
                subscription.valid_from = min(p.valid_from for p in remaining)
                subscription.valid_until = max(p.valid_until for p in remaining)
                subscription.status = (
                    SubscriptionStatus.CANCELLED
                    if subscription.cancelled_at
                    else SubscriptionStatus.ACTIVE
                )
            else:
                subscription.status = SubscriptionStatus.REFUNDED
            # A refund does not prove Telegram cancelled future renewals.
            await session.execute(
                update(BillingOperation)
                .where(
                    BillingOperation.kind == "refund",
                    BillingOperation.identity == charge_id,
                )
                .values(status="completed")
            )
            lifecycle("refund_recorded", subscription, charge_id)
            return True

    async def _claim_operation(self, kind: str, identity: str) -> UUID | None:
        now, token = utcnow(), uuid4()
        async with self.sessions.begin() as session:
            created = await session.scalar(
                insert(BillingOperation)
                .values(
                    kind=kind,
                    identity=identity,
                    status="processing",
                    token=token,
                    lease_until=now + timedelta(minutes=2),
                )
                .on_conflict_do_nothing(
                    index_elements=[BillingOperation.kind, BillingOperation.identity]
                )
                .returning(BillingOperation.id)
            )
            if created:
                return token
            operation = await session.scalar(
                select(BillingOperation)
                .where(
                    BillingOperation.kind == kind,
                    BillingOperation.identity == identity,
                )
                .with_for_update()
            )
            assert operation is not None
            if operation.status == "completed":
                return None
            if kind == "refund" or (
                operation.status == "processing" and operation.lease_until > now
            ):
                raise BillingOperationPendingError()
            operation.status, operation.token = "processing", token
            operation.lease_until = now + timedelta(minutes=2)
            return token

    async def _operation_state(self, kind: str, identity: str, token: UUID, status: str) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(BillingOperation)
                .where(
                    BillingOperation.kind == kind,
                    BillingOperation.identity == identity,
                    BillingOperation.token == token,
                    BillingOperation.status != "completed",
                )
                .values(status=status)
            )

    async def cancel_renewal(self, user_id: UUID, subscription_id: UUID) -> None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(Subscription, User)
                    .join(User)
                    .where(
                        Subscription.id == subscription_id,
                        Subscription.user_id == user_id,
                        Subscription.provider == STARS_PROVIDER,
                    )
                )
            ).one_or_none()
            if (
                row is None
                or row[1].telegram_user_id is None
                or not row[0].telegram_payment_charge_id
            ):
                raise PaymentRejectedError()
            subscription, user = row
            if subscription.cancelled_at:
                return
            charge, telegram_id = subscription.telegram_payment_charge_id, user.telegram_user_id
        assert charge is not None and telegram_id is not None
        token = await self._claim_operation("cancel", str(subscription_id))
        if token is None:
            return
        try:
            await self.provider.cancel_renewal(telegram_id, charge)
            async with self.sessions.begin() as session:
                await session.get(User, user_id, with_for_update=True)
                subscription = await session.get(Subscription, subscription_id)
                assert subscription is not None
                subscription.auto_renew, subscription.cancelled_at = False, utcnow()
                if subscription.status == SubscriptionStatus.ACTIVE:
                    subscription.status = SubscriptionStatus.CANCELLED
                await session.execute(
                    update(BillingOperation)
                    .where(
                        BillingOperation.kind == "cancel",
                        BillingOperation.identity == str(subscription_id),
                        BillingOperation.token == token,
                    )
                    .values(status="completed")
                )
                lifecycle("subscription_cancelled", subscription, charge)
        except Exception:
            await self._operation_state("cancel", str(subscription_id), token, "uncertain")
            raise BillingOperationPendingError() from None

    async def refund_stars(self, telegram_id: int, charge_id: str) -> bool:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(PaymentEvent, User)
                    .join(User)
                    .where(
                        PaymentEvent.provider == STARS_PROVIDER,
                        PaymentEvent.telegram_payment_charge_id == charge_id,
                        PaymentEvent.event_type == BillingEventType.PURCHASE,
                        User.telegram_user_id == telegram_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise PaymentRejectedError()
            purchase, user = row
            amount, subscription_id, user_id = (
                int(purchase.amount),
                purchase.subscription_id,
                user.id,
            )
            refunded = await session.scalar(
                select(PaymentEvent.id).where(
                    PaymentEvent.provider == STARS_PROVIDER,
                    PaymentEvent.telegram_payment_charge_id == charge_id,
                    PaymentEvent.event_type == BillingEventType.REFUND,
                )
            )
        if refunded:
            return False
        if subscription_id is None:
            raise PaymentRejectedError()
        # Explicit refund primitive also stops future charges before moving money.
        await self.cancel_renewal(user_id, subscription_id)
        token = await self._claim_operation("refund", charge_id)
        if token is None:
            return False
        try:
            await self.provider.refund(telegram_id, charge_id)
            return await self.record_refund(telegram_id, charge_id, amount=amount)
        except Exception:
            await self._operation_state("refund", charge_id, token, "uncertain")
            raise BillingOperationPendingError() from None

    async def expire(self, limit: int = 100) -> int:
        now = utcnow()
        async with self.sessions() as session:
            user_ids = list(
                await session.scalars(
                    select(Subscription.user_id)
                    .where(
                        Subscription.valid_until <= now,
                        Subscription.status.in_(
                            [SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELLED]
                        ),
                    )
                    .distinct()
                    .limit(limit)
                )
            )
        changed = 0
        for user_id in user_ids:
            async with self.sessions.begin() as session:
                # The same lock order as payment/refund/cancellation prevents cleanup
                # from overwriting a concurrently renewed subscription's active state.
                user = await session.scalar(
                    select(User).where(User.id == user_id).with_for_update(skip_locked=True)
                )
                if user is None:
                    continue
                rows = list(
                    await session.scalars(
                        select(Subscription).where(
                            Subscription.user_id == user_id,
                            Subscription.valid_until <= now,
                            Subscription.status.in_(
                                [SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELLED]
                            ),
                        )
                    )
                )
                for subscription in rows:
                    subscription.status = SubscriptionStatus.EXPIRED
                changed += len(rows)
            for subscription in rows:
                lifecycle("subscription_expired", subscription)
        return changed
