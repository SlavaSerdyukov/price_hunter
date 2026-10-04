from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.types import Update

from pricehunter.bot.app import create_dispatcher
from pricehunter.bot.keyboards import Action
from pricehunter.localization.languages import SUPPORTED_LANGUAGES
from pricehunter.localization.messages import CATALOGS, tr
from pricehunter.schemas.api import UserSettingsPatch
from tests.integration.test_delivery_operations import dynamic
from tests.telegram import FakeTelegramSession

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_delivery_settings_and_comparison_are_localized_and_separate(container, language):
    _, (a, _), user = await dynamic(container)
    await container.users.settings(user.id, UserSettingsPatch(language_code=language))
    session = FakeTelegramSession()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=session)
    dispatcher = create_dispatcher(container)
    sender = {"id": 56001, "is_bot": False, "first_name": "Tester", "language_code": language}
    counter = 0

    async def send(text=None, action=None, value=""):
        nonlocal counter
        counter += 1
        message = {
            "message_id": counter,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": 56001, "type": "private"},
            "from": sender,
            "text": text or "settings",
        }
        payload = {"update_id": counter}
        if action:
            payload["callback_query"] = {
                "id": str(counter),
                "from": sender,
                "chat_instance": "fixture",
                "message": message,
                "data": Action(action=action, value=value).pack(),
            }
        else:
            payload["message"] = message
        await dispatcher.feed_update(bot, Update.model_validate(payload))

    try:
        await send("/settings")
        assert tr(language, "country") in session.sent[-1].text
        await send(action="setting", value="delivery_country")
        await send(action="dcountry", value="BE")
        await send(action="setting", value="delivery_postal")
        await send("2000")
        saved = await container.users.telegram(56001)
        assert (
            saved.delivery_country == saved.country_code == "BE"
            and saved.delivery_postal_code == "2000"
        )
        await send(action="delquote", value=f"{a.product_id.hex}_BE")
        assert any(tr(language, "delivery_heading", country="BE") in m.text for m in session.sent)
        assert any(tr(language, "comparison_best_label") in m.text for m in session.sent)
        assert "2000" not in session.sent[-1].text
        await send(action="dclear")
        saved = await container.users.telegram(56001)
        assert (
            saved.delivery_country is None
            and saved.delivery_postal_code is None
            and saved.country_code == "BE"
        )
        assert all(
            key in CATALOGS[language]
            for key in ("delivery_incomplete", "invalid_delivery_context", "delivery_quote")
        )
    finally:
        await dispatcher.storage.close()
        await bot.session.close()
