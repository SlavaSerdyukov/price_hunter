import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.core.network import PublicHTTPTransport
from pricehunter.db.session import SessionFactory, create_sessions
from pricehunter.domain.subscriptions import Plan, PlanLimits, SubscriptionPolicy
from pricehunter.providers.amazon import AmazonCreatorsProvider
from pricehunter.providers.base import StoreProvider
from pricehunter.providers.ebay import EbayBrowseProvider
from pricehunter.providers.http import ProviderHTTP
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.providers.woocommerce import WooCommerceProvider
from pricehunter.services.price_check_service import PriceCheckService
from pricehunter.services.product_service import ProductService
from pricehunter.services.search_service import SearchService
from pricehunter.services.tracking_service import TrackingService
from pricehunter.services.user_service import UserService


class Container:
    """Composition root. No service locator inside domain or business services."""

    def __init__(
        self,
        settings: Settings,
        *,
        sessions: SessionFactory | None = None,
        redis: Redis | None = None,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions or create_sessions(settings)
        self.redis = (
            redis
            if redis is not None
            else Redis.from_url(
                settings.redis_url.get_secret_value(),
                socket_timeout=5,
                socket_connect_timeout=5,
            )
        )
        self.http = httpx.AsyncClient(
            transport=PublicHTTPTransport(),
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20),
        )
        self.limiter = RateLimiter(self.redis, settings)
        providers: list[StoreProvider] = []
        if settings.mock_provider_enabled:
            providers.append(MockStoreProvider())
        if (
            settings.ebay_enabled
            and settings.ebay_client_id.get_secret_value()
            and settings.ebay_client_secret.get_secret_value()
        ):
            providers.append(
                EbayBrowseProvider(
                    ProviderHTTP(
                        self.http,
                        timeout=settings.provider_timeout_seconds,
                        max_bytes=settings.max_response_bytes,
                    ),
                    settings.ebay_client_id.get_secret_value(),
                    settings.ebay_client_secret.get_secret_value(),
                    settings.ebay_marketplaces,
                    belgium_locale=settings.ebay_belgium_locale,
                )
            )
        for shop in dict.fromkeys(settings.woocommerce_stores):
            providers.append(
                WooCommerceProvider(
                    ProviderHTTP(
                        self.http,
                        timeout=settings.provider_timeout_seconds,
                        max_bytes=settings.max_response_bytes,
                    ),
                    shop,
                )
            )
        if settings.amazon_enabled:
            providers.append(
                AmazonCreatorsProvider(
                    ProviderHTTP(
                        self.http,
                        timeout=settings.provider_timeout_seconds,
                        max_bytes=settings.max_response_bytes,
                    ),
                    settings.amazon_creator_client_id.get_secret_value(),
                    settings.amazon_creator_client_secret.get_secret_value(),
                    settings.amazon_marketplaces,
                    credential_version=settings.amazon_credential_version,
                    partner_tag=settings.amazon_partner_tag,
                )
            )
        self.registry = registry or ProviderRegistry(providers)
        self.policy = SubscriptionPolicy(
            {
                Plan.FREE: PlanLimits(settings.free_tracker_limit, settings.free_check_seconds),
                Plan.PRO: PlanLimits(settings.pro_tracker_limit, settings.pro_check_seconds),
                Plan.POWER: PlanLimits(settings.power_tracker_limit, settings.power_check_seconds),
            }
        )
        self.users = UserService(self.sessions)
        self.products = ProductService(self.sessions, self.registry, self.limiter, settings)
        self.trackers = TrackingService(self.sessions, self.policy, settings)
        self.search = SearchService(self.registry, self.limiter)
        self.price_checks = PriceCheckService(self.sessions, self.registry, self.limiter, settings)

    async def close(self) -> None:
        await self.http.aclose()
        await self.redis.aclose()
        engine = self.sessions.kw["bind"]
        if isinstance(engine, AsyncEngine):
            await engine.dispose()
