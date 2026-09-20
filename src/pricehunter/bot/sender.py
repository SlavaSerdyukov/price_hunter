from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from pricehunter.bot.keyboards import button
from pricehunter.localization.messages import money, tr
from pricehunter.services.notification_service import (
    Delivery,
    PermanentDeliveryFailure,
    RetryDelivery,
)


class TelegramNotificationSender:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, delivery: Delivery) -> int:
        language = delivery.language
        text = tr(
            language,
            "notification",
            event=tr(language, delivery.event_type),
            title=delivery.title[:180],
            price=money(delivery.price, delivery.currency, language),
        )
        rows = [[InlineKeyboardButton(text=tr(language, "open_store"), url=delivery.url)]]
        if delivery.product_id is not None:
            text = tr(
                language,
                "watch_notification",
                event=tr(language, delivery.event_type),
                title=delivery.title[:180],
                store=delivery.store[:60],
                price=money(delivery.price, delivery.currency, language),
                previous=money(delivery.previous_price, delivery.currency, language)
                if delivery.previous_price is not None
                else "—",
                previous_store=delivery.previous_store[:60],
            )
            rows.append([button(language, "compare_stores", "compare", delivery.product_id.hex)])
        try:
            message = await self.bot.send_message(
                delivery.telegram_id,
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                request_timeout=20,
            )
            return message.message_id
        except TelegramRetryAfter as exc:
            raise RetryDelivery(exc.retry_after) from exc
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            raise PermanentDeliveryFailure() from exc
