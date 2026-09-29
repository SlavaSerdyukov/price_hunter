from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pricehunter.domain.markets import CountryCode
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY, ProviderDataPolicy


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://pricehunter:pricehunter@localhost:5432/pricehunter"
    )
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_webhook_secret: SecretStr = SecretStr("")
    telegram_mode: Literal["polling", "webhook"] = "polling"
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    mock_provider_enabled: bool = True
    mock_check_interval_seconds: int = Field(60, ge=30)
    ebay_enabled: bool = False
    ebay_client_id: SecretStr = SecretStr("")
    ebay_client_secret: SecretStr = SecretStr("")
    ebay_marketplaces: list[str] = ["BE", "DE", "FR", "NL", "IT", "ES", "AT", "IE", "PL"]
    ebay_belgium_locale: Literal["nl-BE", "fr-BE"] = "nl-BE"
    ebay_epn_campaign_id: str = Field(default="", pattern=r"^(?:[0-9]{10})?$")
    ebay_delivery_country: CountryCode | None = None
    ebay_delivery_postal_code: str = Field(default="", max_length=20, pattern=r"^[A-Za-z0-9 -]*$")
    provider_data_policies: dict[str, ProviderDataPolicy] = {}
    rakuten_enabled: bool = False
    rakuten_client_id: SecretStr = SecretStr("")
    rakuten_client_secret: SecretStr = SecretStr("")
    rakuten_account_id: SecretStr = SecretStr("")
    # Reviewed advertiser MIDs by catalog market; Product Search has no documented NID filter.
    rakuten_advertisers: dict[
        CountryCode, list[Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]]
    ] = {}
    rakuten_page_size: int = Field(20, ge=1, le=100)
    rakuten_max_pages: int = Field(2, ge=1, le=5)
    rakuten_max_results: int = Field(50, ge=1, le=100)
    public_base_url: str = ""
    redirect_signing_secret: SecretStr = SecretStr("")
    redirect_ttl_seconds: int = Field(86400, ge=60, le=2592000)
    outbound_click_retention_days: int = Field(30, ge=1, le=365)
    fx_enabled: bool = False
    fx_refresh_seconds: int = Field(21600, ge=3600, le=86400)
    fx_max_age_days: int = Field(7, ge=1, le=30)
    woocommerce_stores: list[
        Literal["pine64_eu", "raspberrypi_dk", "hemptees_be", "westernshop_be"]
    ] = []
    amazon_enabled: bool = False
    amazon_price_tracking_approved: bool = False
    amazon_credential_version: Literal["3.1", "3.2", "3.3"] = "3.2"
    amazon_creator_client_id: SecretStr = SecretStr("")
    amazon_creator_client_secret: SecretStr = SecretStr("")
    amazon_partner_tag: str = ""
    amazon_marketplaces: dict[str, dict[str, str]] = {}
    bestbuy_api_key: SecretStr = SecretStr("")
    stripe_secret_key: SecretStr = SecretStr("")
    wallet_pay_api_key: SecretStr = SecretStr("")
    free_tracker_limit: int = Field(2, ge=1)
    pro_tracker_limit: int = Field(50, ge=1)
    power_tracker_limit: int = Field(250, ge=1)
    free_check_seconds: int = Field(43200, ge=60)
    pro_check_seconds: int = Field(7200, ge=60)
    power_check_seconds: int = Field(3600, ge=60)
    pro_price_stars: int = Field(250, ge=1, le=10000)
    power_price_stars: int = Field(750, ge=1, le=10000)
    stars_billing_enabled: bool = False
    billing_price_version: str = Field("v1", pattern=r"^[a-zA-Z0-9_-]{1,20}$")
    checkout_ttl_seconds: int = Field(900, ge=60, le=3600)
    free_search_limit: int = Field(3, ge=1)
    pro_search_limit: int = Field(30, ge=1)
    power_search_limit: int = Field(100, ge=1)
    free_search_results: int = Field(3, ge=1, le=200)
    pro_search_results: int = Field(15, ge=1, le=200)
    power_search_results: int = Field(50, ge=1, le=200)
    free_history_days: int = Field(7, ge=1)
    pro_history_days: int = Field(90, ge=1)
    power_history_days: int = Field(365, ge=1)
    plan_features: dict[
        str,
        list[
            Literal[
                "target_price_alerts",
                "historical_low_alerts",
                "back_in_stock_alerts",
                "comparison_search",
                "history_access",
            ]
        ],
    ] = {
        "free": ["comparison_search", "history_access"],
        "pro": [
            "target_price_alerts",
            "historical_low_alerts",
            "back_in_stock_alerts",
            "comparison_search",
            "history_access",
        ],
        "power": [
            "target_price_alerts",
            "historical_low_alerts",
            "back_in_stock_alerts",
            "comparison_search",
            "history_access",
        ],
    }
    user_requests_per_minute: int = Field(10, ge=1)
    provider_requests_per_minute: int = Field(60, ge=1)
    provider_rate_limits: dict[str, Annotated[int, Field(ge=1)]] = {
        "rakuten": 20,
        "woocommerce_pine64_eu": 10,
        "woocommerce_raspberrypi_dk": 10,
        "woocommerce_hemptees_be": 5,
        "woocommerce_westernshop_be": 5,
        "amazon": 5,
    }
    provider_concurrency: int = Field(2, ge=1, le=20)
    provider_timeout_seconds: int = Field(20, ge=1, le=40)
    max_response_bytes: int = Field(2_000_000, ge=1024)
    refresh_lease_seconds: int = Field(120, ge=90)
    batch_size: int = Field(100, ge=1, le=1000)
    notification_cooldown_seconds: int = Field(3600, ge=0)
    history_retention_days: int = Field(0, ge=0)
    offer_freshness_seconds: dict[str, Annotated[int, Field(ge=0)]] = {
        "default": 86400,
        "ebay": 64800,
        "woocommerce": 86400,
        "amazon": 0,
        "mock": 300,
    }
    discovery_enabled: bool = True
    discovery_plan_seconds: dict[str, Annotated[int, Field(ge=60)]] = {
        "free": 604800,
        "pro": 86400,
        "power": 21600,
    }
    mock_discovery_seconds: int = Field(600, ge=60)
    discovery_batch_size: int = Field(20, ge=1, le=100)
    discovery_result_limit: int = Field(50, ge=1, le=100)
    discovery_lease_seconds: int = Field(180, ge=90)
    discovery_failure_threshold: int = Field(5, ge=1)
    discovery_suppression_seconds: int = Field(3600, ge=60)
    comparison_refresh_limit: int = Field(20, ge=1, le=100)
    support_contact: str = "Contact the bot administrator for support."

    def data_policy(self, provider: str) -> ProviderDataPolicy:
        if provider == "mock":
            return SYNTHETIC_POLICY
        return self.provider_data_policies.get(provider, ProviderDataPolicy())

    @model_validator(mode="after")
    def production_safety(self) -> "Settings":
        if "default" not in self.offer_freshness_seconds:
            raise ValueError("OFFER_FRESHNESS_SECONDS requires a default policy")
        if set(self.discovery_plan_seconds) != {"free", "pro", "power"}:
            raise ValueError("DISCOVERY_PLAN_SECONDS must configure free, pro and power")
        if set(self.plan_features) != {"free", "pro", "power"}:
            raise ValueError("PLAN_FEATURES must configure free, pro and power")
        if self.stars_billing_enabled and not self.telegram_bot_token.get_secret_value():
            raise ValueError("Stars checkout requires TELEGRAM_BOT_TOKEN")
        if self.amazon_enabled:
            if not self.amazon_price_tracking_approved:
                raise ValueError(
                    "Amazon tracking needs separate Amazon approval; see docs/amazon-setup.md"
                )
            if not (
                self.amazon_creator_client_id.get_secret_value().strip()
                and self.amazon_creator_client_secret.get_secret_value().strip()
                and self.amazon_marketplaces
            ):
                raise ValueError("Amazon requires Creators API credentials and marketplaces")
        if self.environment == "production":
            if self.mock_provider_enabled:
                raise ValueError("Disable MOCK_PROVIDER_ENABLED in production")
            if "*" in self.allowed_hosts:
                raise ValueError("Production requires explicit ALLOWED_HOSTS")
            if self.telegram_mode == "webhook":
                if len(self.telegram_webhook_secret.get_secret_value()) < 32:
                    raise ValueError("Webhook secret must have at least 32 characters")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
