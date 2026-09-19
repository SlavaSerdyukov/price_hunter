from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from pydantic import ValidationError

from pricehunter.core.container import Container
from pricehunter.domain.errors import PriceHunterError
from pricehunter.localization.messages import tr


class UserContextMiddleware(BaseMiddleware):
    def __init__(self, container: Container) -> None:
        self.container = container

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        source = data.get("event_from_user")
        chat = data.get("event_chat")
        if not source:
            return None
        language = source.language_code or "en"
        if chat and chat.type != "private":
            if isinstance(event, Message):
                await event.answer(tr(language, "private_only"))
            elif isinstance(event, CallbackQuery):
                await event.answer(tr(language, "private_only"), show_alert=True)
            return None
        try:
            referral = None
            if isinstance(event, Message) and event.text and event.text.startswith("/start ref_"):
                referral = event.text.split("ref_", 1)[1][:32]
            user = await self.container.users.telegram(
                source.id,
                username=source.username,
                first_name=source.first_name,
                language=source.language_code,
                referral=referral,
            )
            language = user.language_code
            data.update(container=self.container, user=user, language=language)
            return await handler(event, data)
        except PriceHunterError as exc:
            key = exc.code
        except (ValueError, ValidationError):
            key = "invalid_input"
        except Exception as exc:
            structlog.get_logger().error("bot_handler_failed", error_type=type(exc).__name__)
            key = "unexpected_error"
        if isinstance(event, Message):
            await event.answer(tr(language, key))
        elif isinstance(event, CallbackQuery):
            await event.answer(tr(language, key), show_alert=True)
        return None
