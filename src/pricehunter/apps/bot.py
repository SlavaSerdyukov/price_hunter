import asyncio

from pricehunter.bot.app import create_bot, create_dispatcher
from pricehunter.core.config import get_settings
from pricehunter.core.container import Container
from pricehunter.core.logging import configure_logging


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.telegram_mode != "polling":
        raise RuntimeError("Webhook mode is served by the API; do not also start polling")
    container = Container(settings)
    try:
        bot = create_bot(container)
        dispatcher = create_dispatcher(container)
        try:
            # Explicit local polling mode; preserve updates Telegram already queued.
            await bot.delete_webhook(drop_pending_updates=False)
            await dispatcher.start_polling(
                bot, allowed_updates=dispatcher.resolve_used_update_types()
            )
        finally:
            await bot.session.close()
    finally:
        await container.close()


if __name__ == "__main__":
    asyncio.run(main())
