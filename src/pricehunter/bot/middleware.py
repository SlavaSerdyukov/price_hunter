from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.fsm.middleware import FSMContextMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from pydantic import ValidationError
from redis.exceptions import RedisError

from pricehunter.core.container import Container
from pricehunter.core.logging import correlation_context
from pricehunter.domain.errors import PriceHunterError
from pricehunter.localization.languages import DEFAULT_LANGUAGE, normalize_language
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
        language = normalize_language(source.language_code or DEFAULT_LANGUAGE)
        if chat and chat.type != "private":
            if isinstance(event, Message):
                await event.answer(tr(language, "private_only"))
            elif isinstance(event, CallbackQuery):
                await event.answer(tr(language, "private_only"), show_alert=True)
            return None
        # Financial intake must commit before the transport acknowledges. Do not swallow
        # persistence failures: webhook returns 5xx and polling retains its offset.
        if isinstance(event, Message) and (event.successful_payment or event.refunded_payment):
            data["container"] = self.container
            return await handler(event, data)
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
            await self.container.limiter.user(user.id)
            data.update(container=self.container, user=user, language=language)
            return await handler(event, data)
        except PriceHunterError as exc:
            key = exc.code
        except (RedisError, OSError, TimeoutError):
            key = "service_unavailable"
        except (ValueError, ValidationError):
            key = "invalid_input"
        except Exception as exc:
            structlog.get_logger().error("bot_handler_failed", error_type=type(exc).__name__)
            key = "unexpected_error"
        if key in (
            "feature_requires_upgrade",
            "subscription_limit",
            "cancel_renewal_first",
            "subscription_conflict",
        ):
            from pricehunter.bot.billing import subscription_keyboard

            if isinstance(event, Message):
                await event.answer(tr(language, key), reply_markup=subscription_keyboard(language))
            elif isinstance(event, CallbackQuery) and isinstance(event.message, Message):
                await event.message.answer(
                    tr(language, key), reply_markup=subscription_keyboard(language)
                )
                await event.answer()
            return None
        if isinstance(event, Message):
            await event.answer(tr(language, key))
        elif isinstance(event, CallbackQuery):
            await event.answer(tr(language, key), show_alert=True)
        return None


class ConversationMiddleware(BaseMiddleware):
    """FSM/Redis isolation applies to conversations, never to financial updates."""

    def __init__(self, fsm: FSMContextMiddleware) -> None:
        self.fsm = fsm

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update) and (
            event.pre_checkout_query
            or (
                event.message
                and (event.message.successful_payment or event.message.refunded_payment)
            )
        ):
            return await handler(event, data)
        try:
            return await self.fsm(handler, event, data)
        except (RedisError, OSError, TimeoutError):
            # Ordinary conversation isolation fails closed; finance bypasses this entire block.
            source = data.get("event_from_user")
            language = normalize_language(
                (source.language_code or DEFAULT_LANGUAGE) if source else DEFAULT_LANGUAGE
            )
            if isinstance(event, Update):
                if event.callback_query:
                    await event.callback_query.answer(
                        tr(language, "service_unavailable"), show_alert=True
                    )
                elif event.message:
                    await event.message.answer(tr(language, "service_unavailable"))
            return None


class UpdateCorrelationMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            with correlation_context(telegram_update_id=event.update_id):
                return await handler(event, data)
        return await handler(event, data)
