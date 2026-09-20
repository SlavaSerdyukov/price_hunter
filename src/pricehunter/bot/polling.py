import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from pricehunter.domain.errors import PaymentRejectedError


async def durable_polling(
    bot: Bot, dispatcher: Dispatcher, persist: Callable[[Update], Awaitable[None]]
) -> None:
    """Commit financial intake before offsets advance; slow retailer calls never block checkout."""
    offset: int | None = None
    allowed = dispatcher.resolve_used_update_types()
    tasks: set[asyncio.Task[Any]] = set()

    async def dispatch(update: Update) -> None:
        try:
            await dispatcher.feed_update(bot, update)
        except Exception as exc:
            # Financial data is already durable and the worker retries it.
            structlog.get_logger().warning(
                "update_handler_retry_needed", error_type=type(exc).__name__
            )

    try:
        while True:
            try:
                updates = await bot.get_updates(
                    offset=offset, timeout=30, allowed_updates=allowed, request_timeout=40
                )
                for update in updates:
                    try:
                        await persist(update)
                    except PaymentRejectedError:
                        structlog.get_logger().warning("financial_update_rejected")
                        offset = update.update_id + 1
                        continue
                    task = asyncio.create_task(dispatch(update))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                    offset = update.update_id + 1
                    if len(tasks) >= 200:  # Bounded memory/backpressure under overload.
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                structlog.get_logger().warning("polling_retry", error_type=type(exc).__name__)
                await asyncio.sleep(1)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
