"""Read-only restored-domain checks. No retailer or Redis request is required."""

from pydantic import SecretStr
from sqlalchemy import select

from pricehunter.core.config import Settings
from pricehunter.core.container import Container
from pricehunter.db.models import MerchantProgram, ProductWatch, StoreOffer, Subscription, User
from pricehunter.db.session import SessionFactory
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY
from pricehunter.operations.errors import RecoveryError


async def domain_smoke(sessions: SessionFactory) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        mock_provider_enabled=True,
        provider_data_policies={"mock": SYNTHETIC_POLICY},
        ebay_enabled=False,
        amazon_enabled=False,
        rakuten_enabled=False,
        awin_enabled=False,
        tradedoubler_enabled=False,
        cj_enabled=False,
        woocommerce_stores=[],
        telegram_bot_token=SecretStr(""),
        fx_enabled=False,
    )
    container = Container(settings, sessions=sessions)
    try:
        # This is the runtime's exact-head check, before any domain access or migration.
        await container.runtime.check_schema()
        async with sessions() as session:
            user = await session.scalar(select(User).order_by(User.id).limit(1))
            offer = await session.scalar(select(StoreOffer).order_by(StoreOffer.id).limit(1))
            subscriber = await session.scalar(select(Subscription.user_id).limit(1))
            watch = await session.scalar(select(ProductWatch).limit(1))
            program_id = await session.scalar(select(MerchantProgram.id).limit(1))
        if user:
            await container.subscriptions.status(user.id)
            await container.watches.list(user.id)
        if subscriber:
            await container.subscriptions.status(subscriber)
        if watch:
            await container.watches.list(watch.user_id)
        if user and offer:
            await container.products.product(
                offer.product_id, user.id, market_country=offer.market_country
            )
            await container.best_prices.history(
                offer.product_id, user.id, offer.currency, market_country=offer.market_country
            )
        await container.merchants.list_merchants(10)
        if program_id:
            await container.merchant_programs.get(program_id)
            await container.merchant_programs.history(program_id, limit=10)
    except Exception as exc:
        from pricehunter.services.runtime import RuntimePreflightError

        if isinstance(exc, RuntimePreflightError):
            raise RecoveryError(exc.reason) from None
        raise RecoveryError("domain_smoke_failed") from None
    finally:
        await container.close()
