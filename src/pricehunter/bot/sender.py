from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
        try:
            message = await self.bot.send_message(
                delivery.telegram_id,
                tr(
                    language,
                    "notification",
                    event=tr(language, delivery.event_type),
                    title=delivery.title,
                    price=money(delivery.price, delivery.currency, language),
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text=tr(language, "open_store"), url=delivery.url),
                        ]
                    ]
                ),
                request_timeout=20,
            )
            return message.message_id
        except TelegramRetryAfter as exc:
            raise RetryDelivery(exc.retry_after) from exc
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            raise PermanentDeliveryFailure() from exc
