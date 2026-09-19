import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import structlog
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pricehunter.api.middleware import BodyLimitMiddleware
from pricehunter.api.routes import router
from pricehunter.bot.app import create_bot, create_dispatcher
from pricehunter.core.config import Settings, get_settings
from pricehunter.core.container import Container
from pricehunter.core.logging import configure_logging
from pricehunter.domain.errors import PriceHunterError


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    resources = container or Container(settings)
    bot: Bot | None = None
    dispatcher: Dispatcher | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal bot, dispatcher
        if settings.telegram_mode == "webhook" and settings.telegram_bot_token.get_secret_value():
            bot, dispatcher = create_bot(resources), create_dispatcher(resources)
        yield
        if bot:
            await bot.session.close()
        if dispatcher:
            await dispatcher.storage.close()
        await resources.close()

    app = FastAPI(
        title="PriceHunter",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.environment != "production" else None,
    )
    app.state.container = resources
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(BodyLimitMiddleware)
    app.include_router(router)

    @app.exception_handler(PriceHunterError)
    async def domain_error(request: Request, exc: PriceHunterError) -> JSONResponse:
        headers = {"Retry-After": "60"} if exc.status_code == 429 else None
        return JSONResponse(
            status_code=exc.status_code, content={"error": exc.code}, headers=headers
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        structlog.get_logger().error("api_request_failed", error_type=type(exc).__name__)
        return JSONResponse(status_code=500, content={"error": "unexpected_error"})

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        try:
            async with asyncio.timeout(5):
                async with resources.sessions() as session:
                    await session.execute(text("SELECT 1 FROM alembic_version LIMIT 1"))
                    await session.execute(text("SELECT 1 FROM users LIMIT 1"))
                await resources.redis.ping()
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return JSONResponse(content={"status": "ready"})

    @app.post("/telegram/webhook", include_in_schema=False)
    async def webhook(
        request: Request,
        secret: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
    ) -> dict[str, bool]:
        expected = settings.telegram_webhook_secret.get_secret_value()
        if not expected or not secret or not secrets.compare_digest(expected, secret):
            raise HTTPException(status_code=403)
        if bot is None or dispatcher is None:
            raise HTTPException(status_code=503)
        try:
            update = Update.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid Telegram update") from exc
        await dispatcher.feed_update(bot, update)
        return {"ok": True}

    return app
