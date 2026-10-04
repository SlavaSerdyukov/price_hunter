import asyncio

from pricehunter.bot.app import create_bot, create_dispatcher
from pricehunter.bot.payment_updates import persist_financial_update
from pricehunter.bot.polling import durable_polling
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
        await container.runtime.run()
        bot = create_bot(container)
        dispatcher = None
        try:
            dispatcher = create_dispatcher(container)
            # Explicit local polling mode; preserve updates Telegram already queued.
            await bot.delete_webhook(drop_pending_updates=False)
            await durable_polling(
                bot, dispatcher, lambda update: persist_financial_update(container, update)
            )
        finally:
            try:
                if dispatcher is not None:
                    await dispatcher.storage.close()
            finally:
                await bot.session.close()
    finally:
        await container.close()


if __name__ == "__main__":
    asyncio.run(main())
