from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage

from pricehunter.bot.billing import build_billing_router
from pricehunter.bot.handlers import build_router
from pricehunter.bot.middleware import ConversationMiddleware, UserContextMiddleware
from pricehunter.core.container import Container


def create_bot(container: Container) -> Bot:
    token = container.settings.telegram_bot_token.get_secret_value()
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN to run the Telegram client")
    return Bot(token, default=DefaultBotProperties(parse_mode="HTML"))


def create_dispatcher(container: Container) -> Dispatcher:
    storage = RedisStorage(container.redis, state_ttl=3600, data_ttl=3600)
    dispatcher = Dispatcher(
        storage=storage,
        disable_fsm=True,
        events_isolation=RedisEventIsolation(container.redis),
        container=container,
    )
    dispatcher.update.outer_middleware(ConversationMiddleware(dispatcher.fsm))
    router = build_router()
    middleware = UserContextMiddleware(container)
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)
    billing = build_billing_router()
    billing.message.outer_middleware(middleware)
    billing.callback_query.outer_middleware(middleware)
    dispatcher.include_router(billing)
    dispatcher.include_router(router)
    return dispatcher
