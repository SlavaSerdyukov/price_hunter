import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.methods import CreateInvoiceLink, EditUserStarSubscription, RefundStarPayment
from aiogram.types import Update
from pydantic import ValidationError

from pricehunter.bot.polling import durable_polling
from pricehunter.core.config import Settings
from pricehunter.domain.billing import BillingProduct, checkout_payload, parse_checkout_payload
from pricehunter.domain.errors import (
    FeatureRequiresUpgradeError,
    FeatureUnavailableError,
    PaymentRejectedError,
)
from pricehunter.domain.subscriptions import Feature, Plan, PlanEntitlements
from pricehunter.payments.base import (
    CheckoutContext,
    StripePaymentProvider,
    WalletPayPaymentProvider,
    eligible_provider,
)
from pricehunter.payments.stars import TelegramStarsPaymentProvider
from pricehunter.services.billing_catalog import BillingCatalog
from tests.telegram import FakeTelegramSession


def test_catalog_and_versioned_prices():
    settings = Settings(
        _env_file=None, pro_price_stars=300, power_price_stars=800, billing_price_version="v2"
    )
    catalog = BillingCatalog(settings)
    assert catalog.for_plan(Plan.PRO).code == "pricehunter_pro_monthly_v2"
    assert catalog.for_plan(Plan.PRO).stars == 300
    assert catalog.for_plan(Plan.POWER).stars == 800
    assert catalog.for_plan(Plan.POWER).subscription_period == 2592000
    with pytest.raises(PaymentRejectedError):
        catalog.for_plan(Plan.FREE)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, power_price_stars=10001)
    with pytest.raises(ValueError):
        BillingProduct("bad", Plan.PRO, 50, subscription_period=60)


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "pro",
        "ph2:pro:250",
        "ph2:" + "a" * 31,
        "ph2:" + "A" * 32,
        "ph2:" + "a" * 32 + ":power",
        "ph2:" + "z" * 32,
    ],
)
def test_payload_rejects_mutable_or_malformed_state(payload):
    with pytest.raises(PaymentRejectedError):
        parse_checkout_payload(payload)


def test_payload_and_feature_policy():
    identity = uuid4()
    assert parse_checkout_payload(checkout_payload(identity)) == identity
    free = PlanEntitlements(2, 43200)
    with pytest.raises(FeatureRequiresUpgradeError):
        free.require(Feature.TARGET_ALERTS)
    pro = replace(free, target_price_alerts=True)
    pro.require(Feature.TARGET_ALERTS)
    assert free.history_days == 7


async def test_stars_provider_official_recurring_invoice_and_admin_methods():
    session = FakeTelegramSession()
    bot = Bot("123456789:TEST_TOKEN_FOR_LOCAL_TESTS_ONLY_12345", session=session)
    provider = TelegramStarsPaymentProvider(bot)
    context = CheckoutContext(
        uuid4(),
        "telegram",
        "BE",
        "pro",
        payload=checkout_payload(uuid4()),
        product=BillingProduct("test_pro", Plan.PRO, 250),
        title="PriceHunter Pro",
        description="30 days",
    )
    assert (await provider.create_checkout(context)).startswith("https://t.me/")
    invoice = session.calls[-1]
    assert isinstance(invoice, CreateInvoiceLink)
    assert invoice.currency == "XTR" and invoice.provider_token == ""
    assert invoice.subscription_period == 2592000
    assert len(invoice.prices) == 1 and invoice.prices[0].amount == 250
    await provider.cancel_renewal(123, "charge")
    assert isinstance(session.calls[-1], EditUserStarSubscription)
    assert session.calls[-1].is_canceled
    await provider.refund(123, "charge")
    assert isinstance(session.calls[-1], RefundStarPayment)
    assert await provider.transactions(0) == []
    assert (await provider.balance()).amount == 123
    for external in (StripePaymentProvider(), WalletPayPaymentProvider()):
        assert not eligible_provider(external.name, context, enabled=True)
        with pytest.raises(FeatureUnavailableError):
            await external.create_checkout(context)
    with pytest.raises(PaymentRejectedError):
        await provider.create_checkout(replace(context, plan="power"))
    await bot.session.close()


async def test_polling_does_not_acknowledge_failed_durable_intake(monkeypatch):
    update = Update(update_id=99)
    bot, dispatcher = AsyncMock(), AsyncMock()
    dispatcher.resolve_used_update_types = lambda: ["message", "pre_checkout_query"]
    bot.get_updates.side_effect = [[update], [update], asyncio.CancelledError()]
    persist = AsyncMock(side_effect=[OSError("database unavailable"), None])
    monkeypatch.setattr("pricehunter.bot.polling.asyncio.sleep", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await durable_polling(bot, dispatcher, persist)
    assert [call.kwargs["offset"] for call in bot.get_updates.call_args_list] == [None, None, 100]
