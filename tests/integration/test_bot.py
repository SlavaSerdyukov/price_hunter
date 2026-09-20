from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
)
from aiogram.types import Update

from pricehunter.bot.app import create_dispatcher
from pricehunter.bot.keyboards import Action
from pricehunter.bot.sender import TelegramNotificationSender
from pricehunter.localization.languages import SUPPORTED_LANGUAGES
from pricehunter.localization.messages import CATALOGS, tr
from pricehunter.services.notification_service import NotificationService
from tests.support import grant_plan
from tests.telegram import FakeTelegramSession

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "telegram_language,expected",
    [
        *((code, code) for code in SUPPORTED_LANGUAGES),
        ("fr-BE", "fr"),
        ("pt-BR", "en"),
        ("de-DE", "de"),
        (None, "en"),
        ("", "en"),
    ],
)
async def test_first_start_uses_telegram_language(container, telegram_language, expected):
    session = FakeTelegramSession()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=session)
    dispatcher = create_dispatcher(container)
    sender = {"id": 123, "is_bot": False, "first_name": "New user"}
    if telegram_language is not None:
        sender["language_code"] = telegram_language
    try:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "date": int(datetime.now(UTC).timestamp()),
                        "chat": {"id": 123, "type": "private"},
                        "from": sender,
                        "text": "/start",
                    },
                }
            ),
        )
        assert tr(expected, "start") in session.sent[-1].text
        assert (await container.users.telegram(123)).language_code == expected
        assert session.sent[-1].reply_markup.inline_keyboard[0][0].text == tr(expected, "search")
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_bot_full_acceptance_scenario(container, language):
    session = FakeTelegramSession()
    bot = Bot(
        container.settings.telegram_bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dispatcher = create_dispatcher(container)
    source = {"id": 123, "is_bot": False, "first_name": "Tester", "language_code": "ru"}
    update_id = 0

    async def message(text):
        nonlocal update_id
        update_id += 1
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                {
                    "update_id": update_id,
                    "message": {
                        "message_id": update_id,
                        "date": int(datetime.now(UTC).timestamp()),
                        "chat": {"id": 123, "type": "private"},
                        "from": source,
                        "text": text,
                    },
                }
            ),
        )

    async def callback(action, value=""):
        nonlocal update_id
        update_id += 1
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                {
                    "update_id": update_id,
                    "callback_query": {
                        "id": str(update_id),
                        "from": source,
                        "chat_instance": "test-chat",
                        "message": session.sent[-1].model_dump(mode="json"),
                        "data": Action(action=action, value=value).pack(),
                    },
                }
            ),
        )

    await message("/start")
    assert tr("ru", "start") in session.sent[-1].text
    assert (await container.users.telegram(123)).language_code == "ru"
    await callback("settings")
    await callback("lang", language)
    assert tr(language, "settings").removeprefix("⚙️ ") in session.sent[-1].text
    assert (await container.users.telegram(123)).language_code == language
    await callback("menu")
    assert tr(language, "start") == session.sent[-1].text
    # Subsequent /start and Telegram profile updates preserve the explicit choice.
    source["language_code"] = "fr" if language == "de" else "de"
    await message("/start")
    assert tr(language, "start") in session.sent[-1].text
    await message("https://mock.pricehunter.test/products/headphones")
    card = session.sent[-1]
    assert "100" in card.text
    track = Action.unpack(card.reply_markup.inline_keyboard[0][0].callback_data)
    await callback(track.action, track.value)
    assert tr(language, "tracked") in session.sent[-1].text
    user = await container.users.telegram(123)
    tracker = (await container.trackers.list(user.id))[0]
    await grant_plan(container, user.id)
    await callback("target", tracker.id.hex)
    await message("10%")
    assert "90" in session.sent[-1].text
    assert (await container.trackers.get(user.id, tracker.id)).target_price == 90
    await container.price_checks.refresh((await container.price_checks.claim_due())[0])
    notifications = NotificationService(
        container.sessions, TelegramNotificationSender(bot), container.entitlements
    )
    assert await notifications.send_pending() == 1
    assert "95" in session.sent[-1].text
    assert tr(language, "historical_low") in session.sent[-1].text
    assert await notifications.send_pending() == 0
    await message("/my")
    assert "95" in session.sent[-1].text
    await callback("history", tracker.offer.id.hex)
    assert CATALOGS[language]["history_card"].split("\n", 1)[0] in session.sent[-1].text
    await callback("pause", tracker.id.hex)
    assert not (await container.trackers.get(user.id, tracker.id)).enabled
    await callback("resume", tracker.id.hex)
    assert (await container.trackers.get(user.id, tracker.id)).enabled
    await message("/settings")
    assert tr(language, "settings").removeprefix("⚙️ ") in session.sent[-1].text
    await callback("lang", "en")
    assert "Settings" in session.sent[-1].text
    await callback("delete", tracker.id.hex)
    assert await container.trackers.list(user.id) == []
    await dispatcher.storage.close()
    await bot.session.close()


async def test_clothing_variant_selection_pagination_tracking_and_stale_buttons(
    container, respx_mock
):
    import json
    from pathlib import Path

    from pricehunter.providers.http import ProviderHTTP
    from pricehunter.providers.woocommerce import WooCommerceProvider

    fixtures = Path(__file__).parents[1] / "fixtures"
    parent = json.loads((fixtures / "woo_hemptees_parent.json").read_text())
    child = json.loads((fixtures / "woo_hemptees_variant.json").read_text())
    provider = WooCommerceProvider(
        ProviderHTTP(container.http, timeout=5, max_bytes=50000), "hemptees_be"
    )
    container.registry.providers[provider.name] = provider
    respx_mock.get(provider.api).respond(200, json=[parent])
    refresh = respx_mock.get(f"{provider.api}/{child['id']}").respond(200, json=child)
    session = FakeTelegramSession()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=session)
    dispatcher = create_dispatcher(container)
    source = {"id": 456, "is_bot": False, "first_name": "Clothing Tester", "language_code": "ru"}

    async def feed(text=None, data=None, source_id=456):
        base = {"update_id": len(session.calls) + 1}
        sender = {**source, "id": source_id}
        if text:
            base["message"] = {
                "message_id": 1,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": source_id, "type": "private"},
                "from": sender,
                "text": text,
            }
        else:
            base["callback_query"] = {
                "id": str(len(session.calls)),
                "from": sender,
                "chat_instance": "clothing",
                "message": session.sent[-1].model_dump(mode="json"),
                "data": data,
            }
        await dispatcher.feed_update(bot, Update.model_validate(base))

    await feed(text="/stores")
    assert "Hemptees" in session.sent[-1].text
    await feed(text=parent["permalink"])
    choices = session.sent[-1]
    assert "Выберите размер/цвет" in choices.text
    assert len(choices.reply_markup.inline_keyboard) == 9
    first = next(
        row[0].callback_data
        for row in choices.reply_markup.inline_keyboard
        if row[0].text == "Size: XS, Color: Pomelo Red"
    )
    # Buttons cannot be used by a different Telegram user, nor fetch any prices.
    await feed(data=first, source_id=999)
    assert isinstance(session.calls[-1], AnswerCallbackQuery) and session.calls[-1].show_alert
    assert refresh.call_count == 0
    await feed(data=choices.reply_markup.inline_keyboard[-1][0].callback_data)
    assert isinstance(session.calls[-1], EditMessageReplyMarkup)
    assert refresh.call_count == 0
    await feed(data=first)
    assert "XS" in session.sent[-1].text and "Pomelo Red" in session.sent[-1].text
    assert "40" in session.sent[-1].text
    await feed(data=session.sent[-1].reply_markup.inline_keyboard[0][0].callback_data)
    user = await container.users.telegram(456)
    trackers = await container.trackers.list(user.id)
    assert len(trackers) == 1 and trackers[0].offer.price == 40
    await feed(data=first)
    assert isinstance(session.calls[-1], AnswerCallbackQuery) and session.calls[-1].show_alert
    assert refresh.call_count == 1
    await dispatcher.storage.close()
    await bot.session.close()


async def test_ebay_group_link_select_track_and_refresh_exact_variant(container, respx_mock):
    import json
    from pathlib import Path

    from sqlalchemy import func, select

    from pricehunter.db.models import Product, StoreOffer
    from pricehunter.providers.ebay import EbayBrowseProvider
    from pricehunter.providers.http import ProviderHTTP

    rows = json.loads(
        (Path(__file__).parents[1] / "fixtures/ebay_variation_group.json").read_text()
    )
    selected = rows["items"][0]
    provider = EbayBrowseProvider(
        ProviderHTTP(container.http, timeout=5, max_bytes=50000), "id", "secret", ["DE"]
    )
    container.registry.providers["ebay"] = provider
    respx_mock.post(provider.api + "/identity/v1/oauth2/token").respond(
        200, json={"access_token": "fake", "expires_in": 3600}
    )
    lookup = respx_mock.get(provider.api + "/buy/browse/v1/item/get_item_by_legacy_id")
    lookup.respond(400, json={"errors": [{"errorId": 11006}]})
    respx_mock.get(provider.api + "/buy/browse/v1/item/get_items_by_item_group").respond(
        200, json=rows
    )
    refresh = respx_mock.get(
        provider.api + "/buy/browse/v1/item/v1%7C167526377039%7C467150657985"
    ).respond(200, json=selected)
    session = FakeTelegramSession()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=session)
    dispatcher = create_dispatcher(container)
    source = {"id": 567, "is_bot": False, "first_name": "eBay Tester", "language_code": "ru"}
    await dispatcher.feed_update(
        bot,
        Update.model_validate(
            {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": 567, "type": "private"},
                    "from": source,
                    "text": "https://www.ebay.de/itm/167526377039?itmprp=advertising&itmmeta=ignored",
                },
            }
        ),
    )
    assert "Выберите размер/цвет" in session.sent[-1].text
    assert session.sent[-1].reply_markup.inline_keyboard[0][0].text == "Uk Schuhgröße: 5,5"
    async with container.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(StoreOffer)) == 0
    lookup.respond(200, json=selected)
    for update_id in (2, 3):  # Select variant, then press Track on its card.
        card = session.sent[-1]
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                {
                    "update_id": update_id,
                    "callback_query": {
                        "id": str(update_id),
                        "from": source,
                        "chat_instance": "ebay",
                        "message": card.model_dump(mode="json"),
                        "data": card.reply_markup.inline_keyboard[0][0].callback_data,
                    },
                }
            ),
        )
        if update_id == 2:
            assert "5,5" in session.sent[-1].text and "239,95" in session.sent[-1].text
    user = await container.users.telegram(567)
    tracker = (await container.trackers.list(user.id))[0]
    assert tracker.offer.url.endswith("?var=467150657985")
    claims = await container.price_checks.claim_due()
    assert len(claims) == 1 and await container.price_checks.refresh(claims[0])
    async with container.sessions() as db:
        offer = await db.get(StoreOffer, tracker.offer.id)
        product = await db.get(Product, offer.product_id)
        assert offer.external_id == selected["itemId"] and offer.observation_count == 2
        assert product.variant["size_uk"] == "5,5" and "5-12" not in offer.title
    assert refresh.call_count == 1
    await dispatcher.storage.close()
    await bot.session.close()
