from uuid import UUID

import httpx
from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.core.network import PublicHTTPTransport
from pricehunter.db.models import MerchantProgram
from pricehunter.db.session import SessionFactory, create_sessions
from pricehunter.domain.feeds import FEED_NETWORKS
from pricehunter.domain.subscriptions import Plan, PlanEntitlements, SubscriptionPolicy
from pricehunter.payments.stars import TelegramStarsPaymentProvider
from pricehunter.providers.affiliate.rakuten import RakutenProvider, RakutenTokenManager
from pricehunter.providers.amazon import AmazonCreatorsProvider
from pricehunter.providers.base import StoreProvider
from pricehunter.providers.ebay import EbayBrowseProvider
from pricehunter.providers.feeds.awin import AwinFeedSource
from pricehunter.providers.feeds.base import FeedSource
from pricehunter.providers.feeds.cj import CJFeedSource
from pricehunter.providers.feeds.local import FeedStoreProvider
from pricehunter.providers.feeds.streaming import FeedBounds, FeedHTTP
from pricehunter.providers.feeds.tradedoubler import TradeDoublerFeedSource
from pricehunter.providers.http import ProviderHTTP
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.providers.woocommerce import WooCommerceProvider
from pricehunter.services.billing_catalog import BillingCatalog
from pricehunter.services.billing_intake import BillingIntake
from pricehunter.services.billing_reconciliation import BillingReconciliation
from pricehunter.services.billing_service import BillingService
from pricehunter.services.comparison_operations import ComparisonOperations
from pricehunter.services.coverage import CoverageDiagnostics
from pricehunter.services.discovery_service import ProductDiscoveryService
from pricehunter.services.entitlement_service import EntitlementService
from pricehunter.services.feed_sync import FeedSyncService
from pricehunter.services.fx_service import FxService
from pricehunter.services.merchant_programs import MerchantProgramService
from pricehunter.services.outbound_service import OutboundLinkService, validate_redirect_settings
from pricehunter.services.policy_resolver import PolicyResolver
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
        validate_redirect_settings(settings)
        self.outbound = OutboundLinkService(settings)
        self.sessions = sessions or create_sessions(settings)
        self.fx = FxService(self.sessions, settings)
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
        self.feed_sources: dict[str, FeedSource] = {}
        enabled_feeds = [name for name in FEED_NETWORKS if getattr(settings, f"{name}_enabled")]
        if enabled_feeds:
            if not settings.feed_program_ids:
                raise ValueError(
                    "Enabled feeds require reviewed FEED_PROGRAM_IDS; see docs/merchant-programs.md"
                )
            try:
                [UUID(value) for value in settings.feed_program_ids]
            except ValueError:
                raise ValueError("FEED_PROGRAM_IDS must contain merchant program UUIDs") from None
            feed_http = FeedHTTP(
                self.http,
                self.limiter,
                FeedBounds(
                    compressed_bytes=settings.feed_max_compressed_bytes,
                    decompressed_bytes=settings.feed_max_decompressed_bytes,
                    record_bytes=settings.feed_max_record_bytes,
                    rows=settings.feed_max_rows,
                ),
            )
            if settings.awin_enabled:
                self.feed_sources["awin"] = AwinFeedSource(feed_http, settings.awin_feed_api_key)
            if settings.tradedoubler_enabled:
                self.feed_sources["tradedoubler"] = TradeDoublerFeedSource(
                    feed_http,
                    settings.tradedoubler_token,
                    page_size=settings.feed_page_size,
                    max_pages=settings.feed_max_pages,
                )
            if settings.cj_enabled:
                self.feed_sources["cj"] = CJFeedSource(
                    feed_http,
                    settings.cj_api_token,
                    settings.cj_company_id,
                    settings.cj_website_id,
                    page_size=settings.feed_page_size,
                    max_pages=settings.feed_max_pages,
                )
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
                    epn_campaign_id=settings.ebay_epn_campaign_id,
                    delivery_country=settings.ebay_delivery_country,
                    delivery_postal_code=settings.ebay_delivery_postal_code,
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
        if settings.rakuten_enabled:
            settings.data_policy("rakuten").require("affiliate_allowed")
            credentials = [
                settings.rakuten_client_id,
                settings.rakuten_client_secret,
                settings.rakuten_account_id,
            ]
            if (
                not all(c.get_secret_value().strip() for c in credentials)
                or not settings.rakuten_advertisers
            ):
                raise ValueError("Rakuten requires credentials and reviewed market advertisers")
            http = ProviderHTTP(
                self.http,
                timeout=settings.provider_timeout_seconds,
                max_bytes=settings.max_response_bytes,
            )
            providers.append(
                RakutenProvider(
                    http,
                    RakutenTokenManager(http, *(c.get_secret_value() for c in credentials)),
                    self.limiter,
                    settings.rakuten_advertisers,
                    page_size=settings.rakuten_page_size,
                    max_pages=settings.rakuten_max_pages,
                    max_results=settings.rakuten_max_results,
                )
            )
        for provider in providers:
            settings.data_policy(provider.name).require("catalog_persistence_allowed")
        providers.extend(FeedStoreProvider(name, self.sessions, settings) for name in enabled_feeds)
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
        self.trackers = TrackingService(self.sessions, self.entitlements, settings, self.registry)
        self.search = SearchService(self.registry, self.limiter, self.entitlements, self.products)
        self.price_checks = PriceCheckService(
            self.sessions, self.registry, self.limiter, settings, self.entitlements
        )
        self.merchant_programs = MerchantProgramService(self.sessions, settings)
        self.feed_sync = FeedSyncService(self.sessions, settings, self.products)
        self.coverage = CoverageDiagnostics(self.sessions, settings, self.registry, self.limiter)

    def create_feed_source(self, network: str) -> FeedSource:
        """Operator discovery can use credentials while the network remains disabled."""
        settings = self.settings
        http = FeedHTTP(
            self.http,
            self.limiter,
            FeedBounds(
                compressed_bytes=settings.feed_max_compressed_bytes,
                decompressed_bytes=settings.feed_max_decompressed_bytes,
                record_bytes=settings.feed_max_record_bytes,
                rows=settings.feed_max_rows,
            ),
        )
        if network == "awin":
            return AwinFeedSource(http, settings.awin_feed_api_key)
        if network == "tradedoubler":
            return TradeDoublerFeedSource(
                http,
                settings.tradedoubler_token,
                page_size=settings.feed_page_size,
                max_pages=settings.feed_max_pages,
            )
        if network == "cj":
            return CJFeedSource(
                http,
                settings.cj_api_token,
                settings.cj_company_id,
                settings.cj_website_id,
                page_size=settings.feed_page_size,
                max_pages=settings.feed_max_pages,
            )
        raise ValueError("Unknown feed network")

    async def validate_feeds(self) -> None:
        if not self.feed_sources:
            return
        async with self.sessions() as session:
            rows = list(
                await session.scalars(
                    select(MerchantProgram).where(
                        MerchantProgram.id.in_([UUID(v) for v in self.settings.feed_program_ids])
                    )
                )
            )
        if (
            len(rows) != len(set(self.settings.feed_program_ids))
            or any(
                p.network not in self.feed_sources
                or not PolicyResolver(self.settings).program(p).catalog_persistence_allowed
                for p in rows
            )
            or set(p.network for p in rows) != set(self.feed_sources)
        ):
            raise ValueError(
                "FEED_PROGRAM_IDS requires active approved programs for each enabled network"
            )

    async def close(self) -> None:
        if self.payment_provider.bot is not None:
            await self.payment_provider.bot.session.close()
        await self.http.aclose()
        await self.redis.aclose()
        engine = self.sessions.kw["bind"]
        if isinstance(engine, AsyncEngine):
            await engine.dispose()
