import httpx
from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.core.network import PublicHTTPTransport
from pricehunter.db.session import SessionFactory, create_sessions
from pricehunter.domain.subscriptions import Plan, PlanEntitlements, SubscriptionPolicy
from pricehunter.payments.stars import TelegramStarsPaymentProvider
from pricehunter.providers.amazon import AmazonCreatorsProvider
from pricehunter.providers.base import StoreProvider
from pricehunter.providers.ebay import EbayBrowseProvider
from pricehunter.providers.http import ProviderHTTP
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.providers.woocommerce import WooCommerceProvider
from pricehunter.services.billing_catalog import BillingCatalog
from pricehunter.services.billing_intake import BillingIntake
from pricehunter.services.billing_reconciliation import BillingReconciliation
from pricehunter.services.billing_service import BillingService
from pricehunter.services.comparison_operations import ComparisonOperations
from pricehunter.services.discovery_service import ProductDiscoveryService
from pricehunter.services.entitlement_service import EntitlementService
from pricehunter.services.price_check_service import PriceCheckService
from pricehunter.services.product_service import ProductService
from pricehunter.services.product_watch_service import ProductWatchService
from pricehunter.services.search_service import SearchService
from pricehunter.services.subscription_service import SubscriptionService
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
            providers.append(MockStoreProvider(settings.mock_discovery_seconds))
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
                plan: PlanEntitlements(
                    max_trackers=getattr(settings, f"{plan}_tracker_limit"),
                    check_interval_seconds=getattr(settings, f"{plan}_check_seconds"),
                    target_price_alerts="target_price_alerts" in settings.plan_features[plan],
                    historical_low_alerts="historical_low_alerts" in settings.plan_features[plan],
                    back_in_stock_alerts="back_in_stock_alerts" in settings.plan_features[plan],
                    comparison_search="comparison_search" in settings.plan_features[plan],
                    history_access="history_access" in settings.plan_features[plan],
                    search_limit=getattr(settings, f"{plan}_search_limit"),
                    search_result_limit=getattr(settings, f"{plan}_search_results"),
                    history_days=getattr(settings, f"{plan}_history_days"),
                )
                for plan in Plan
            }
        )
        self.entitlements = EntitlementService(self.sessions, self.policy)
        self.subscriptions = SubscriptionService(self.sessions, self.entitlements, settings)
        self.catalog = BillingCatalog(settings)
        self.payment_provider = TelegramStarsPaymentProvider(
            Bot(settings.telegram_bot_token.get_secret_value())
            if settings.telegram_bot_token.get_secret_value()
            else None
        )
        self.billing = BillingService(
            self.sessions, settings, self.catalog, self.entitlements, self.payment_provider
        )
        self.billing_intake = BillingIntake(self.sessions, self.billing)
        self.reconciliation = BillingReconciliation(
            self.sessions, self.payment_provider, self.billing
        )
        self.users = UserService(self.sessions)
        self.products = ProductService(
            self.sessions, self.registry, self.limiter, settings, self.entitlements
        )
        self.watches = ProductWatchService(self.sessions, self.entitlements, settings)
        self.comparison_operations = ComparisonOperations(
            self.sessions, self.registry, self.limiter, settings, self.entitlements
        )
        self.best_prices = self.watches.best_prices
        self.discovery = ProductDiscoveryService(
            self.sessions, self.registry, self.limiter, settings, self.entitlements
        )
        self.trackers = TrackingService(self.sessions, self.entitlements, settings)
        self.search = SearchService(self.registry, self.limiter, self.entitlements, self.products)
        self.price_checks = PriceCheckService(
            self.sessions, self.registry, self.limiter, settings, self.entitlements
        )

    async def close(self) -> None:
        if self.payment_provider.bot is not None:
            await self.payment_provider.bot.session.close()
        await self.http.aclose()
        await self.redis.aclose()
        engine = self.sessions.kw["bind"]
        if isinstance(engine, AsyncEngine):
            await engine.dispose()
