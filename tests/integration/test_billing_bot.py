from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.methods import AnswerPreCheckoutQuery, CreateInvoiceLink, EditUserStarSubscription
from aiogram.types import Update
from sqlalchemy import func, select

from pricehunter.api.app import create_app
from pricehunter.bot.app import create_dispatcher
from pricehunter.bot.keyboards import Action
from pricehunter.db.models import BillingUpdate, PaymentEvent, Subscription, User
from pricehunter.localization.languages import SUPPORTED_LANGUAGES
from pricehunter.localization.messages import tr
from pricehunter.schemas.api import UserSettingsPatch
from tests.telegram import FakeTelegramSession

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_complete_telegram_stars_acceptance(container, language):
    container.settings.stars_billing_enabled = True
    container.settings.support_contact = "@slavasham"
    session = FakeTelegramSession()
    bot = Bot(
        container.settings.telegram_bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    container.payment_provider.bot = bot
    dispatcher = create_dispatcher(container)
    sender = {"id": 123, "is_bot": False, "first_name": "Buyer", "language_code": "en"}
    user = await container.users.telegram(123)
    await container.users.settings(user.id, UserSettingsPatch(language_code=language))
    update_id = 0
    paid_until = int((datetime.now(UTC) + timedelta(days=30)).timestamp())

    async def feed(*, command=None, callback=None, precheckout=None, payment=None):
        nonlocal update_id
        update_id += 1
        update = {"update_id": update_id}
        if precheckout:
            update["pre_checkout_query"] = {"id": "precheckout", "from": sender, **precheckout}
        elif callback:
            update["callback_query"] = {
                "id": str(update_id),
                "from": sender,
                "chat_instance": "test",
                "data": callback,
                "message": session.sent[-1].model_dump(mode="json"),
            }
        else:
            update["message"] = {
                "message_id": update_id,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": 123, "type": "private"},
                "from": sender,
            }
            if command:
                update["message"]["text"] = command
            if payment:
                update["message"]["successful_payment"] = payment
        await dispatcher.feed_update(bot, Update.model_validate(update))

    await feed(command="/plans")
    text = session.sent[-1].text
    assert "Free" in text and "250" in text and "750" in text
    buy = session.sent[-1].reply_markup.inline_keyboard[0][0].callback_data
    assert Action.unpack(buy).value == "pro"
    await feed(callback=buy)
    invoice = next(call for call in session.calls if isinstance(call, CreateInvoiceLink))
    assert invoice.currency == "XTR" and invoice.subscription_period == 2592000
    assert invoice.description == tr(language, "invoice_description", plan="Pro")
    assert session.sent[-1].reply_markup.inline_keyboard[0][0].url.startswith("https://t.me/")
    contract = {"currency": "XTR", "total_amount": 250, "invoice_payload": invoice.payload}
    await feed(precheckout={**contract, "currency": "USD"})
    rejected = next(call for call in session.calls if isinstance(call, AnswerPreCheckoutQuery))
    assert not rejected.ok and rejected.error_message == tr(language, "payment_rejected")
    await feed(precheckout=contract)
    answer = next(
        call for call in reversed(session.calls) if isinstance(call, AnswerPreCheckoutQuery)
    )
    assert answer.ok
    user = await container.users.telegram(123)
    assert (await container.entitlements.for_user(user.id)).plan == "free"
    successful = {
        **contract,
        "telegram_payment_charge_id": "test-payment",
        "provider_payment_charge_id": "",
        "subscription_expiration_date": paid_until,
        "is_recurring": True,
        "is_first_recurring": True,
    }
    await feed(payment=successful)
    assert session.sent[-1].text == tr(
        language,
        "payment_successful",
        plan="Pro",
        until=datetime.fromtimestamp(paid_until, UTC).strftime("%Y-%m-%d %H:%M UTC"),
    )
    assert (await container.entitlements.for_user(user.id)).plan == "pro"
    # Duplicate deliveries cannot create or extend financial state again.
    await feed(payment=successful)
    async with container.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(PaymentEvent)) == 1
        assert (await s.scalars(select(BillingUpdate))).one().status == "processed"
        assert (await s.scalars(select(Subscription))).one().valid_until.timestamp() == paid_until
    await feed(command="/subscription")
    assert "Pro" in session.sent[-1].text and "50" in session.sent[-1].text
    cancel = session.sent[-1].reply_markup.inline_keyboard[-1][0].callback_data
    assert Action.unpack(cancel).action == "cancelrenew"
    await feed(callback=cancel)
    confirm = session.sent[-1].reply_markup.inline_keyboard[0][0].callback_data
    assert not any(isinstance(call, EditUserStarSubscription) for call in session.calls)
    await feed(callback=confirm)
    assert (await container.entitlements.for_user(user.id)).plan == "pro"
    assert (await container.entitlements.for_user(user.id)).auto_renew is False
    await feed(command="/paysupport")
    assert "@slavasham" in session.sent[-1].text
    await dispatcher.storage.close()
    await bot.session.close()


async def test_precheckout_rejected_without_fsm_lock_or_user_creation(container):
    container.settings.stars_billing_enabled = True
    session = FakeTelegramSession()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=session)
    dispatcher = create_dispatcher(container)
    await dispatcher.feed_update(
        bot,
        Update.model_validate(
            {
                "update_id": 1,
                "pre_checkout_query": {
                    "id": "bad",
                    "from": {
                        "id": 999,
                        "is_bot": False,
                        "first_name": "Unknown",
                        "language_code": "ru",
                    },
                    "currency": "USD",
                    "total_amount": 1,
                    "invoice_payload": "forged:power",
                },
            }
        ),
    )
    answer = next(call for call in session.calls if isinstance(call, AnswerPreCheckoutQuery))
    assert not answer.ok and answer.error_message == tr("ru", "payment_rejected")
    async with container.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(PaymentEvent)) == 0
        assert await s.scalar(select(func.count()).select_from(User)) == 0
    await dispatcher.storage.close()
    await bot.session.close()


async def test_webhook_financial_persistence_failure_returns_error_for_retry(
    container, monkeypatch
):
    import httpx
    from pydantic import SecretStr

    container.settings.telegram_mode = "webhook"
    container.settings.telegram_webhook_secret = SecretStr("a" * 32)
    bot = Bot(
        container.settings.telegram_bot_token.get_secret_value(), session=FakeTelegramSession()
    )
    monkeypatch.setattr("pricehunter.api.app.create_bot", lambda resources: bot)

    async def fail(*args, **kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr(container.billing_intake, "receive", fail)
    app = create_app(container.settings, container)
    payment = {
        "currency": "XTR",
        "total_amount": 250,
        "invoice_payload": "ph2:" + "a" * 32,
        "telegram_payment_charge_id": "charge",
        "provider_payment_charge_id": "",
        "is_recurring": True,
        "is_first_recurring": True,
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "a" * 32},
                json={
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "date": int(datetime.now(UTC).timestamp()),
                        "chat": {"id": 123, "type": "private"},
                        "from": {"id": 123, "is_bot": False, "first_name": "Buyer"},
                        "successful_payment": payment,
                    },
                },
            )
            assert response.status_code == 500


async def test_precheckout_bypasses_busy_conversation_lock(container):
    import asyncio

    from aiogram.fsm.storage.base import StorageKey

    session = FakeTelegramSession()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=session)
    dispatcher = create_dispatcher(container)
    key = StorageKey(bot_id=bot.id, chat_id=999, user_id=999)
    async with dispatcher.fsm.events_isolation.lock(key):
        async with asyncio.timeout(1):
            await dispatcher.feed_update(
                bot,
                Update.model_validate(
                    {
                        "update_id": 1,
                        "pre_checkout_query": {
                            "id": "query",
                            "from": {"id": 999, "is_bot": False, "first_name": "Buyer"},
                            "currency": "XTR",
                            "total_amount": 250,
                            "invoice_payload": "ph2:" + "0" * 32,
                        },
                    }
                ),
            )
    assert isinstance(session.calls[-1], AnswerPreCheckoutQuery)
    await dispatcher.storage.close()
    await bot.session.close()


async def test_refunded_payment_update_uses_payer_chat_not_bot_sender(container):
    from tests.integration.test_billing import purchased

    container.settings.stars_billing_enabled = True
    transport = FakeTelegramSession()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=transport)
    container.payment_provider.bot = bot
    user, _, payment = await purchased((container, transport))
    dispatcher = create_dispatcher(container)
    update = Update.model_validate(
        {
            "update_id": 123,
            "message": {
                "message_id": 123,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": 123, "type": "private"},
                "from": {"id": bot.id, "is_bot": True, "first_name": "PriceHunter"},
                "refunded_payment": {
                    "currency": "XTR",
                    "total_amount": 250,
                    "invoice_payload": payment.payload,
                    "telegram_payment_charge_id": payment.charge_id,
                },
            },
        }
    )
    await dispatcher.feed_update(bot, update)
    await dispatcher.feed_update(bot, update)
    assert (await container.entitlements.for_user(user.id)).plan == "free"
    async with container.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(PaymentEvent)) == 2
    await dispatcher.storage.close()
    await bot.session.close()
