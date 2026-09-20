from dataclasses import dataclass, field

import structlog
from aiogram.types import StarTransaction, TransactionPartnerUser
from sqlalchemy import select

from pricehunter.db.models import CheckoutIntent, PaymentEvent, Subscription, User
from pricehunter.db.session import SessionFactory
from pricehunter.domain.billing import STARS_PROVIDER, BillingEventType, parse_checkout_payload
from pricehunter.domain.errors import PaymentRejectedError
from pricehunter.payments.stars import TelegramStarsPaymentProvider
from pricehunter.services.billing_service import BillingService, charge_reference


@dataclass
class ReconciliationReport:
    scanned: int = 0
    next_offset: int = 0
    complete: bool = False
    applied_refunds: int = 0
    findings: list[dict[str, str]] = field(default_factory=list)


class BillingReconciliation:
    def __init__(
        self,
        sessions: SessionFactory,
        provider: TelegramStarsPaymentProvider,
        billing: BillingService,
    ) -> None:
        self.sessions, self.provider, self.billing = sessions, provider, billing

    async def scan(
        self, *, offset: int = 0, max_pages: int = 10, apply_refunds: bool = False
    ) -> ReconciliationReport:
        if offset < 0 or not 1 <= max_pages <= 1000:
            raise ValueError("Invalid pagination")
        report = ReconciliationReport(next_offset=offset)
        seen: set[tuple[str, str]] = set()
        for _ in range(max_pages):
            transactions = await self.provider.transactions(report.next_offset)
            for transaction in transactions:
                await self._compare(transaction, report, seen, apply_refunds)
            report.scanned += len(transactions)
            report.next_offset += len(transactions)
            if len(transactions) < 100:
                report.complete = True
                break
        # A partial page range cannot prove absence of a local payment remotely.
        if offset == 0 and report.complete:
            async with self.sessions() as session:
                events = await session.scalars(
                    select(PaymentEvent).where(
                        PaymentEvent.provider == STARS_PROVIDER,
                        PaymentEvent.telegram_payment_charge_id.is_not(None),
                    )
                )
                for event in events:
                    assert event.telegram_payment_charge_id is not None
                    if (event.telegram_payment_charge_id, event.event_type) not in seen:
                        self._finding(report, "missing_remote", event.telegram_payment_charge_id)
        return report

    @staticmethod
    def _finding(report: ReconciliationReport, kind: str, charge: str) -> None:
        reference = charge_reference(charge)[:12]
        report.findings.append({"kind": kind, "charge_ref": reference})
        structlog.get_logger().warning(
            "reconciliation_mismatch", kind=kind, provider=STARS_PROVIDER, charge_ref=reference
        )

    async def _compare(
        self,
        transaction: StarTransaction,
        report: ReconciliationReport,
        seen: set[tuple[str, str]],
        apply_refunds: bool,
    ) -> None:
        refund = transaction.receiver is not None
        partner = transaction.receiver if refund else transaction.source
        if (
            not isinstance(partner, TransactionPartnerUser)
            or partner.transaction_type != "invoice_payment"
        ):
            return
        event_type = BillingEventType.REFUND if refund else BillingEventType.PURCHASE
        seen.add((transaction.id, event_type))
        async with self.sessions() as session:
            event = await session.scalar(
                select(PaymentEvent).where(
                    PaymentEvent.provider == STARS_PROVIDER,
                    PaymentEvent.telegram_payment_charge_id == transaction.id,
                    PaymentEvent.event_type == event_type,
                )
            )
            purchase = (
                await session.scalar(
                    select(PaymentEvent).where(
                        PaymentEvent.provider == STARS_PROVIDER,
                        PaymentEvent.telegram_payment_charge_id == transaction.id,
                        PaymentEvent.event_type == BillingEventType.PURCHASE,
                    )
                )
                if refund
                else event
            )
            try:
                intent_id = parse_checkout_payload(partner.invoice_payload or "")
            except PaymentRejectedError:
                self._finding(report, "unmapped_transaction", transaction.id)
                return
            row = (
                await session.execute(
                    select(CheckoutIntent, User)
                    .join(User)
                    .where(
                        CheckoutIntent.id == intent_id,
                        User.telegram_user_id == partner.user.id,
                    )
                )
            ).one_or_none()
            if (
                row is None
                or row[0].expected_amount != abs(transaction.amount)
                or transaction.nanostar_amount
            ):
                self._finding(report, "amount_or_owner_mismatch", transaction.id)
                return
            intent, user = row
            if partner.subscription_period not in (None, 2592000):
                self._finding(report, "product_mismatch", transaction.id)
                return
            if purchase is not None:
                subscription = await session.get(Subscription, purchase.subscription_id)
                if (
                    purchase.user_id != user.id
                    or purchase.amount != abs(transaction.amount)
                    or purchase.billing_product_code != intent.billing_product_code
                    or subscription is None
                    or subscription.checkout_intent_id != intent.id
                ):
                    self._finding(report, "product_mismatch", transaction.id)
                    return
            if event is not None:
                return
            self._finding(
                report,
                "refund_missing_local" if refund else "payment_missing_local",
                transaction.id,
            )
        if refund and purchase is not None and apply_refunds:
            applied = await self.billing.record_refund(
                partner.user.id,
                transaction.id,
                amount=abs(transaction.amount),
                payload=partner.invoice_payload,
            )
            report.applied_refunds += int(applied)
        # History lacks recurring expiration/first-payment information: purchases are report-only.
