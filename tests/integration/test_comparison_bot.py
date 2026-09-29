from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update
from sqlalchemy import update

from pricehunter.bot.app import create_dispatcher
from pricehunter.bot.keyboards import Action
from pricehunter.bot.sender import TelegramNotificationSender
from pricehunter.db.base import utcnow
from pricehunter.db.models import StoreOffer
from pricehunter.localization.languages import SUPPORTED_LANGUAGES
from pricehunter.localization.messages import tr
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.schemas.api import UserSettingsPatch
from pricehunter.services.notification_service import NotificationService

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_comparison_watch_and_exact_offer_flow_in_every_language(container, language):
    from tests.telegram import FakeTelegramSession

    transport = FakeTelegramSession()
    bot = Bot(
        container.settings.telegram_bot_token.get_secret_value(),
        session=transport,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dispatcher = create_dispatcher(container)
    source = {"id": 777, "is_bot": False, "first_name": "Tester", "language_code": language}
    counter = 0

    async def feed(text=None, action=None, value=""):
        nonlocal counter
        counter += 1
        update = {"update_id": counter}
        if text:
            update["message"] = {
                "message_id": counter,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": 777, "type": "private"},
                "from": source,
                "text": text,
            }
        else:
            update["callback_query"] = {
                "id": str(counter),
                "from": source,
                "chat_instance": "comparison",
                "message": transport.sent[-1].model_dump(mode="json"),
                "data": Action(action=action, value=value).pack(),
            }
        await dispatcher.feed_update(bot, Update.model_validate(update))

    try:
        # More than one offer page; all records still resolve to the same black product.
        base = MockStoreProvider()._offer("sony-a")
        for index in range(2):
            await container.products.persist(
                base.model_copy(
                    update={
                        "store_slug": f"extra_{index}",
                        "external_id": f"extra_{index}",
                        "store_name": f"Extra {index}",
                    }
                )
            )
        await feed(text="/start")
        await feed(action="country", value="DE")
        before = len(transport.sent)
        await feed(text="/search Sony WH-1000XM6")
        cards = [m for m in transport.sent[before:] if m.reply_markup]
        assert len(cards) == 3  # Three canonical variants/conflicting IDs, not nine offer cards.
        alpha = await container.products.persist(base)
        user = await container.users.telegram(777)
        await feed(action="compare", value=f"{alpha.product_id.hex}_DE")
        card = transport.sent[-1]
        assert tr(language, "comparison_best_label") in card.text
        assert "Demo Alpha" in card.text and "USD" not in card.text
        await container.users.settings(user.id, UserSettingsPatch(country_code=None))
        await feed(action="watch", value=f"{alpha.product_id.hex}_EUR")
        assert tr(language, "country_required") in transport.sent[-1].text
        await feed(action="wmarket", value=f"{alpha.product_id.hex}_EUR_DE")
        assert tr(language, "watch_created", currency="EUR") in transport.sent[-1].text
        watch = (await container.watches.list(user.id))[0]
        await container.users.settings(user.id, UserSettingsPatch(country_code="BE"))
        await feed(action="wcompare", value=watch.id.hex)
        assert tr(language, "market_context", country="DE") in transport.sent[-1].text
        assert (await container.watches.get(user.id, watch.id)).market_country == "DE"
        actions = [
            Action.unpack(b.callback_data)
            for row in transport.sent[-1].reply_markup.inline_keyboard
            for b in row
            if b.callback_data
        ]
        history_action = next(a for a in actions if a.action == "besthist")
        refresh_action = next(a for a in actions if a.action == "refreshcmp")
        assert history_action.value.endswith("_DE") and refresh_action.value.endswith("_DE")
        await feed(action=history_action.action, value=history_action.value)
        history = transport.sent[-1]
        assert "329" in history.text and "Demo Alpha" in history.text
        assert any(button.url for row in history.reply_markup.inline_keyboard for button in row)
        await feed(text="/watches")
        assert tr(language, "active") in transport.sent[-1].text
        await feed(action="wpause", value=watch.id.hex)
        assert not (await container.watches.list(user.id))[0].scheduled
        await feed(action="wresume", value=watch.id.hex)
        assert (await container.watches.list(user.id))[0].scheduled
        await feed(action="offers", value=f"{alpha.product_id.hex}_1_DE")
        assert (
            len([row for row in transport.sent[-1].reply_markup.inline_keyboard if row[0].url]) == 1
        )
        await feed(action="offers", value=f"{alpha.product_id.hex}_0_DE")
        card = transport.sent[-1]
        action = Action.unpack(card.reply_markup.inline_keyboard[0][1].callback_data)
        await feed(action=action.action, value=action.value)
        assert len(await container.trackers.list(user.id)) == 1
        await feed(action="details", value=f"{alpha.product_id.hex}_DE")
        assert "Sony" in transport.sent[-1].text and "WH" in transport.sent[-1].text
        beta = await container.products.persist(MockStoreProvider()._offer("sony-b"))
        claim = next(c for c in await container.price_checks.claim_due() if c.offer_id == beta.id)
        assert await container.price_checks.refresh(claim)
        notifier = NotificationService(
            container.sessions,
            TelegramNotificationSender(bot),
            container.entitlements,
        )
        assert await notifier.send_pending() == 1
        alert = transport.sent[-1]
        assert tr(language, "merchant_became_cheapest") in alert.text
        assert "319" in alert.text and "329" in alert.text and "Demo Beta" in alert.text
        assert tr(language, "market_context", country="DE") in alert.text
        comparison_action = Action.unpack(alert.reply_markup.inline_keyboard[1][0].callback_data)
        assert comparison_action.value == f"{alpha.product_id.hex}_DE"
        await feed(action=comparison_action.action, value=comparison_action.value)
        assert tr(language, "market_context", country="DE") in transport.sent[-1].text
        assert await notifier.send_pending() == 0
        async with container.sessions.begin() as session:
            await session.execute(
                update(StoreOffer).values(last_checked_at=utcnow() - timedelta(days=4))
            )
        await feed(action="compare", value=f"{alpha.product_id.hex}_DE")
        assert tr(language, "no_fresh_prices", currency="EUR") in transport.sent[-1].text
        await feed(action="besthist", value=f"{alpha.product_id.hex}_EUR_DE")
        assert tr(language, "best_unknown") in transport.sent[-1].text
        await feed(action=refresh_action.action, value=refresh_action.value)
        # Only DE Beta needs a new request; the other five DE leases remain active.
        assert tr(language, "refresh_queued", count=1) == transport.sent[-1].text
        await feed(action="watches", value="0")
        await feed(action="wdelete", value=watch.id.hex)
        await feed(text="/watches")
        assert transport.sent[-1].text == tr(language, "watches_empty")
        for message in transport.sent:
            assert len(message.text.encode("utf-16-le")) // 2 <= 4096
            if message.reply_markup:
                assert all(
                    len(button.callback_data.encode()) <= 64
                    for row in message.reply_markup.inline_keyboard
                    for button in row
                    if button.callback_data
                )
    finally:
        await dispatcher.storage.close()
        await bot.session.close()
