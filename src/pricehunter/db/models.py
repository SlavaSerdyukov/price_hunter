from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pricehunter.db.base import Base, Timestamps, UUIDPrimaryKey, utcnow

MONEY = Numeric(18, 4)


class FxRate(UUIDPrimaryKey, Base):
    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint("base_currency", "quote_currency", "effective_date", "source"),
        CheckConstraint("rate > 0", name="positive_rate"),
        Index("ix_fx_effective_date", "effective_date"),
    )
    base_currency: Mapped[str] = mapped_column(String(3))
    quote_currency: Mapped[str] = mapped_column(String(3))
    rate: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    effective_date: Mapped[date] = mapped_column(Date)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(30), default="ECB")


class OutboundClick(UUIDPrimaryKey, Base):
    __tablename__ = "outbound_clicks"
    offer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("store_offers.id", ondelete="SET NULL")
    )
    store_id: Mapped[UUID | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"))
    affiliate_network: Mapped[str | None] = mapped_column(String(80))
    surface: Mapped[str] = mapped_column(String(20))
    market_country: Mapped[str | None] = mapped_column(String(2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    opaque_click_reference: Mapped[str] = mapped_column(String(32))


class User(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "users"
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(100))
    first_name: Mapped[str | None] = mapped_column(String(200))
    language_code: Mapped[str] = mapped_column(String(10), default="en")
    country_code: Mapped[str | None] = mapped_column(String(2))
    preferred_currency: Mapped[str] = mapped_column(String(3), default="EUR")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    referral_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    referred_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class APIKey(UUIDPrimaryKey, Base):
    __tablename__ = "api_keys"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Store(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "stores"
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    domain: Mapped[str] = mapped_column(String(200))
    provider_type: Mapped[str] = mapped_column(String(50))
    external_merchant_id: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str] = mapped_column(String(2))
    supported: Mapped[bool] = mapped_column(default=True)
    active: Mapped[bool] = mapped_column(default=True)


class Product(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "products"
    identity_key: Mapped[str] = mapped_column(String(64), unique=True)
    canonical_name: Mapped[str] = mapped_column(String(500))
    brand: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(200))
    gtin: Mapped[str | None] = mapped_column(String(14), index=True)
    ean: Mapped[str | None] = mapped_column(String(13))
    upc: Mapped[str | None] = mapped_column(String(12))
    asin: Mapped[str | None] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(200))
    mpn: Mapped[str | None] = mapped_column(String(200))
    variant: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


class ProductIdentifier(UUIDPrimaryKey, Base):
    __tablename__ = "product_identifiers"
    __table_args__ = (
        UniqueConstraint("product_id", "kind", "value"),
        Index("ix_product_identifier_lookup", "kind", "value"),
    )
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))
    value: Mapped[str] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(80))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))


class StoreOffer(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "store_offers"
    __table_args__ = (
        UniqueConstraint("store_id", "external_id"),
        CheckConstraint("price > 0", name="positive_price"),
        Index("ix_offers_due", "next_check_at"),
        Index("ix_offers_product_currency", "product_id", "currency"),
    )
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), index=True)
    store_id: Mapped[UUID] = mapped_column(ForeignKey("stores.id"))
    external_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(2048))
    direct_url: Mapped[str | None] = mapped_column(String(2048))
    affiliate_url: Mapped[str | None] = mapped_column(String(2048))
    affiliate_network: Mapped[str | None] = mapped_column(String(80))
    affiliate_click_id: Mapped[str | None] = mapped_column(String(100))
    affiliate_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    delivery_country: Mapped[str | None] = mapped_column(String(2))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    shipping_price: Mapped[Decimal | None] = mapped_column(MONEY)
    tax: Mapped[Decimal | None] = mapped_column(MONEY)
    title: Mapped[str] = mapped_column(String(500))
    image_url: Mapped[str | None] = mapped_column(String(2048))
    price: Mapped[Decimal] = mapped_column(MONEY)
    original_price: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    availability: Mapped[str] = mapped_column(String(20))
    seller: Mapped[str | None] = mapped_column(String(200))
    sku: Mapped[str | None] = mapped_column(String(200))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    identity_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    match_confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=1, server_default="1")
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    refresh_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(default=0)
    refresh_sequence: Mapped[int] = mapped_column(default=0)
    suspicious_price: Mapped[Decimal | None] = mapped_column(MONEY)
    suspicious_currency: Mapped[str | None] = mapped_column(String(3))
    minimum_price: Mapped[Decimal] = mapped_column(MONEY)
    maximum_price: Mapped[Decimal] = mapped_column(MONEY)
    total_price: Mapped[Decimal] = mapped_column(Numeric(30, 4))
    observation_count: Mapped[int] = mapped_column(BigInteger, default=1)


class PriceObservation(UUIDPrimaryKey, Base):
    __tablename__ = "price_observations"
    __table_args__ = (
        Index("ix_observation_offer_time", "store_offer_id", "checked_at"),
        CheckConstraint("price > 0", name="positive_price"),
    )
    store_offer_id: Mapped[UUID] = mapped_column(ForeignKey("store_offers.id", ondelete="CASCADE"))
    refresh_key: Mapped[str] = mapped_column(String(100), unique=True)
    price: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    availability: Mapped[str] = mapped_column(String(20))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Tracker(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "trackers"
    __table_args__ = (
        UniqueConstraint("user_id", "store_offer_id"),
        CheckConstraint("target_price IS NULL OR target_price > 0", name="positive_target"),
        CheckConstraint("check_interval_seconds >= 30", name="minimum_interval"),
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    store_offer_id: Mapped[UUID] = mapped_column(ForeignKey("store_offers.id"), index=True)
    target_price: Mapped[Decimal | None] = mapped_column(MONEY)
    baseline_price: Mapped[Decimal] = mapped_column(MONEY)
    notify_on_any_drop: Mapped[bool] = mapped_column(default=True)
    notify_on_target: Mapped[bool] = mapped_column(default=True)
    notify_on_back_in_stock: Mapped[bool] = mapped_column(default=True)
    notify_on_historical_low: Mapped[bool] = mapped_column(default=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    check_interval_seconds: Mapped[int]
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProductWatch(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "product_watches"
    __table_args__ = (
        UniqueConstraint("user_id", "product_id", "market_country", "currency"),
        CheckConstraint("market_country ~ '^[A-Z]{2}$'", name="market_country_code"),
        CheckConstraint("target_price IS NULL OR target_price > 0", name="positive_target"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_code"),
        Index("ix_watches_product_currency", "product_id", "currency"),
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), index=True)
    market_country: Mapped[str] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(String(3))
    target_price: Mapped[Decimal | None] = mapped_column(MONEY)
    notify_on_new_best: Mapped[bool] = mapped_column(default=True)
    notify_on_price_drop: Mapped[bool] = mapped_column(default=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    best_offer_id: Mapped[UUID | None] = mapped_column(ForeignKey("store_offers.id"))
    best_price: Mapped[Decimal | None] = mapped_column(MONEY)
    best_absence_reason: Mapped[str | None] = mapped_column(String(30))
    evaluation_sequence: Mapped[int] = mapped_column(default=0)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationEvent(UUIDPrimaryKey, Base):
    __tablename__ = "notification_events"
    __table_args__ = (
        CheckConstraint(
            "(tracker_id IS NOT NULL)::int + (product_watch_id IS NOT NULL)::int = 1",
            name="one_subject",
        ),
        Index("ix_notifications_watch_created", "product_watch_id", "created_at"),
        Index("ix_notifications_tracker_created", "tracker_id", "created_at"),
        Index(
            "ix_notifications_sending",
            "attempt_started_at",
            postgresql_where=text("status = 'sending'"),
        ),
        Index(
            "ix_notifications_pending", "available_at", postgresql_where=text("status = 'pending'")
        ),
    )
    tracker_id: Mapped[UUID | None] = mapped_column(ForeignKey("trackers.id", ondelete="CASCADE"))
    product_watch_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("product_watches.id", ondelete="CASCADE")
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    event_type: Mapped[str] = mapped_column(String(40))
    price: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    deduplication_key: Mapped[str] = mapped_column(String(150), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)


class BillingProductRecord(Base):
    __tablename__ = "billing_products"
    __table_args__ = (
        CheckConstraint("plan IN ('pro', 'power')", name="paid_plan"),
        CheckConstraint("stars BETWEEN 1 AND 10000", name="stars_amount"),
        CheckConstraint("subscription_period = 2592000", name="stars_period"),
    )
    code: Mapped[str] = mapped_column(String(100), primary_key=True)
    plan: Mapped[str] = mapped_column(String(20))
    stars: Mapped[int]
    subscription_period: Mapped[int]
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CheckoutIntent(UUIDPrimaryKey, Base):
    __tablename__ = "checkout_intents"
    __table_args__ = (
        CheckConstraint("status IN ('pending','paid','expired','cancelled')", name="status"),
        CheckConstraint("expected_amount > 0 AND currency = 'XTR'", name="stars_payment"),
        Index(
            "ix_checkout_pending_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    billing_product_code: Mapped[str] = mapped_column(ForeignKey("billing_products.code"))
    expected_amount: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3), default="XTR")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_query_id: Mapped[str | None] = mapped_column(String(200))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invoice_url: Mapped[str | None] = mapped_column(String(2048))


class Subscription(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("provider", "provider_subscription_id"),
        CheckConstraint("plan IN ('pro','power')", name="paid_plan"),
        CheckConstraint("status IN ('active','cancelled','expired','refunded')", name="status"),
        Index("ix_subscription_entitlement", "user_id", "valid_until"),
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    plan: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(30))
    # Local recurring identity: the checkout UUID, not an invented Telegram API field.
    provider_subscription_id: Mapped[str | None] = mapped_column(String(200))
    checkout_intent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("checkout_intents.id"), unique=True
    )
    billing_product_code: Mapped[str | None] = mapped_column(ForeignKey("billing_products.code"))
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    auto_renew: Mapped[bool] = mapped_column(default=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentEvent(UUIDPrimaryKey, Base):
    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint("provider", "external_event_id"),
        UniqueConstraint(
            "provider", "telegram_payment_charge_id", "event_type", name="uq_payment_charge_type"
        ),
        Index("ix_payment_subscription", "subscription_id"),
    )
    provider: Mapped[str] = mapped_column(String(30))
    external_event_id: Mapped[str] = mapped_column(String(200))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(String(200))
    billing_product_code: Mapped[str | None] = mapped_column(ForeignKey("billing_products.code"))
    subscription_id: Mapped[UUID | None] = mapped_column(ForeignKey("subscriptions.id"))
    plan: Mapped[str | None] = mapped_column(String(20))
    event_type: Mapped[str] = mapped_column(String(40))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SubscriptionPeriod(UUIDPrimaryKey, Base):
    __tablename__ = "subscription_periods"
    __table_args__ = (
        CheckConstraint("valid_until > valid_from", name="valid_period"),
        Index("ix_period_entitlement", "subscription_id", "valid_until"),
    )
    subscription_id: Mapped[UUID] = mapped_column(ForeignKey("subscriptions.id"))
    payment_event_id: Mapped[UUID] = mapped_column(ForeignKey("payment_events.id"), unique=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BillingUpdate(UUIDPrimaryKey, Base):
    __tablename__ = "billing_updates"
    __table_args__ = (
        UniqueConstraint("event_type", "charge_id"),
        CheckConstraint("status IN ('pending','processed','rejected')", name="status"),
        Index("ix_billing_updates_due", "status", "available_at"),
    )
    event_type: Mapped[str] = mapped_column(String(40))
    charge_id: Mapped[str] = mapped_column(String(200))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error_code: Mapped[str | None] = mapped_column(String(80))


class BillingOperation(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "billing_operations"
    __table_args__ = (
        UniqueConstraint("kind", "identity"),
        CheckConstraint("status IN ('processing','completed','uncertain')", name="status"),
    )
    kind: Mapped[str] = mapped_column(String(20))
    identity: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20))
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    token: Mapped[UUID]


class ProductDiscovery(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "product_discoveries"
    __table_args__ = (
        UniqueConstraint("product_id", "provider", "country", "currency"),
        Index("ix_discovery_due", "next_discovery_at"),
    )
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    country: Mapped[str] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(String(3))
    next_discovery_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(60))
    result_count: Mapped[int] = mapped_column(default=0)
    sequence: Mapped[int] = mapped_column(default=0)


class ProductBestState(UUIDPrimaryKey, Base):
    __tablename__ = "product_best_states"
    __table_args__ = (
        UniqueConstraint("product_id", "currency"),
        Index("ix_best_state_expiry", "next_evaluation_at"),
    )
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    store_offer_id: Mapped[UUID | None] = mapped_column(ForeignKey("store_offers.id"))
    price: Mapped[Decimal | None] = mapped_column(MONEY)
    sequence: Mapped[int] = mapped_column(default=0)
    next_evaluation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BestPriceEvent(UUIDPrimaryKey, Base):
    __tablename__ = "best_price_events"
    __table_args__ = (
        UniqueConstraint("product_id", "currency", "sequence"),
        Index("ix_best_history", "product_id", "currency", "observed_at"),
    )
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"))
    currency: Mapped[str] = mapped_column(String(3))
    sequence: Mapped[int]
    store_offer_id: Mapped[UUID | None] = mapped_column(ForeignKey("store_offers.id"))
    price: Mapped[Decimal | None] = mapped_column(MONEY)
    store: Mapped[str | None] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(30))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source_observation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("price_observations.id", ondelete="SET NULL")
    )
