"""Reviewed inventory; a new mapped or deployed table must be classified explicitly."""

from typing import Literal

from pricehunter.db import models as _models  # noqa: F401 -- registers every mapped table
from pricehunter.db.base import Base
from pricehunter.operations.errors import RecoveryError

Durability = Literal["authoritative", "reconstructable", "ephemeral"]

# A covers identities, rights, idempotency, contractual/audit evidence and historical state.
# B can be reacquired, but restore still verifies it exactly and documents lost freshness.
# No PostgreSQL business table is C: even job leases travel with their durable job state.
TABLES: dict[str, Durability] = {
    "users": "authoritative",
    "api_keys": "authoritative",
    "billing_products": "authoritative",
    "checkout_intents": "authoritative",
    "subscriptions": "authoritative",
    "payment_events": "authoritative",
    "subscription_periods": "authoritative",
    "billing_updates": "authoritative",
    "billing_operations": "authoritative",
    "merchants": "authoritative",
    "stores": "authoritative",
    "merchant_audits": "authoritative",
    "products": "authoritative",
    "product_identifiers": "authoritative",
    "store_offers": "authoritative",
    "price_observations": "authoritative",
    "trackers": "authoritative",
    "product_watches": "authoritative",
    "product_best_states": "authoritative",
    "best_price_events": "authoritative",
    "notification_events": "authoritative",
    "product_discoveries": "authoritative",
    "merchant_programs": "authoritative",
    "merchant_program_audits": "authoritative",
    "merchant_program_validations": "authoritative",
    "feed_publication_audits": "authoritative",
    "feed_sync_states": "authoritative",
    "outbound_clicks": "authoritative",
    "merchant_feed_items": "reconstructable",
    "feed_pending_items": "reconstructable",
    "fx_rates": "reconstructable",
    "delivery_quotes": "reconstructable",
    "alembic_version": "authoritative",
}


def validate_inventory(names: set[str] | None = None) -> None:
    mapped = set(Base.metadata.tables) | {"alembic_version"}
    if set(TABLES) != mapped or (names is not None and names != mapped):
        raise RecoveryError("table_inventory_mismatch")
