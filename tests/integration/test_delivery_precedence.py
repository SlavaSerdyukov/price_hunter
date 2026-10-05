"""Conflicting whole evidence tuples must follow freshness, scope and time."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from pricehunter.db.base import utcnow
from pricehunter.db.models import DeliveryQuote, PriceObservation, StoreOffer
from pricehunter.domain.delivery import (
    DeliveryContext,
    DeliveryQuoteData,
    delivered_total,
    snapshot_key,
)
from tests.integration.test_delivery_acceptance import delivered, listing

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("cheap_source", ["static", "dynamic"])
@pytest.mark.parametrize(
    "static_scope,dynamic_scope,static_age,dynamic_age,status,expired_source,selected_source",
    [
        pytest.param(
            "exact", "exact", 10, 30, "complete", None, "static", id="A-newer-static-exact"
        ),
        pytest.param(
            "exact", "exact", 30, 10, "complete", None, "dynamic", id="B-newer-dynamic-exact"
        ),
        pytest.param(
            "country", "exact", 10, 30, "complete", None, "dynamic", id="C-dynamic-more-specific"
        ),
        pytest.param(
            "exact", "country", 30, 10, "complete", None, "static", id="D-static-more-specific"
        ),
        pytest.param(
            "country", "country", 10, 30, "complete", None, "static", id="E-newer-static-country"
        ),
        pytest.param(
            "country", "country", 30, 10, "complete", None, "dynamic", id="F-newer-dynamic-country"
        ),
        pytest.param("country", "exact", 30, 10, "failed", None, "static", id="G-failed-exact"),
        pytest.param(
            "country", "exact", 30, 10, "unsupported", None, "static", id="G-unsupported-exact"
        ),
        pytest.param(
            "country", "exact", 30, 10, "complete", "dynamic", "static", id="H-stale-dynamic-exact"
        ),
        pytest.param(
            "exact",
            "country",
            10,
            30,
            "complete",
            "static",
            "dynamic",
            id="current-dynamic-beats-stale-static",
        ),
        pytest.param(
            "exact",
            "exact",
            10,
            10,
            "complete",
            None,
            "static",
            id="equal-exact-time-static-tie-break",
        ),
        pytest.param(
            "country",
            "country",
            10,
            10,
            "complete",
            None,
            "static",
            id="equal-country-time-static-tie-break",
        ),
    ],
)
async def test_delivery_evidence_precedence_controls_display_and_winner(
    container,
    cheap_source,
    static_scope,
    dynamic_scope,
    static_age,
    dynamic_age,
    status,
    expired_source,
    selected_source,
):
    now = utcnow()
    context = DeliveryContext(country="BE", postal_code="2000")
    costs = {
        cheap_source: {
            "shipping_price": Decimal("2"),
            "tax_status": "additional",
            "tax_amount": Decimal("3"),
        },
        ("dynamic" if cheap_source == "static" else "static"): {
            "shipping_price": Decimal("20"),
            "tax_status": "included",
            "tax_amount": None,
        },
    }
    quote_at = {
        "static": now - timedelta(seconds=static_age),
        "dynamic": now - timedelta(seconds=dynamic_age),
    }
    expiry = {
        source: now + timedelta(seconds=-1 if source == expired_source else 300)
        for source in ("static", "dynamic")
    }
    a = await listing(
        container,
        "sony-a",
        "299",
        delivery_scope=static_scope,
        delivery_destination_key=(
            context.fingerprint if static_scope == "exact" else context.country_key
        ),
        delivery_quoted_at=quote_at["static"],
        delivery_expires_at=expiry["static"],
        **costs["static"],
    )
    b = await listing(container, "sony-b", "309", "0")
    async with container.sessions() as session:
        external_id = await session.scalar(
            select(StoreOffer.external_id).where(StoreOffer.id == a.id)
        )
    quote = DeliveryQuoteData(
        offer_id=a.id,
        snapshot_key=snapshot_key(a.price, a.currency, external_id, a.url),
        country="BE",
        scope=dynamic_scope,
        destination_key=(context.fingerprint if dynamic_scope == "exact" else context.country_key),
        currency="EUR",
        availability="in_stock",
        quoted_at=quote_at["dynamic"],
        expires_at=expiry["dynamic"],
        source="mock",
        status=status,
        **costs["dynamic"],
    )
    async with container.sessions.begin() as session:
        session.add(
            DeliveryQuote(
                **quote.model_dump(),
                id=uuid4(),
                item_price=a.price,
                offer_url=a.url,
                delivered_total=delivered_total(a.price, a.currency, quote),
            )
        )

    expected = costs[selected_source]
    expected_total = Decimal("304") if selected_source == cheap_source else Decimal("319")
    # Repeat reads also pin the documented deterministic equal-time tie-break.
    for _ in range(2):
        view = await delivered(container, a.product_id, postal_code="2000")
        assert view.best_available_offer.offer_id == a.id
        assert view.best_available_offer.price == Decimal("299")
        for row in (
            next(o for o in view.offers if o.offer_id == a.id),
            next(o for o in view.delivered_offers if o.offer_id == a.id),
        ):
            assert row.shipping_price == expected["shipping_price"]
            assert row.tax_status == expected["tax_status"]
            assert row.additional_tax == expected["tax_amount"]
            assert row.delivered_total == expected_total
            assert row.delivery_quote_at == quote_at[selected_source]
            assert row.delivery_quote_expires_at == expiry[selected_source]
            assert row.delivery_quote_status == "complete"
            assert row.delivery_availability == "in_stock"
            assert row.price == Decimal("299") and row.currency == "EUR" and row.url == a.url
        winner = view.currency_groups[0].best_delivered_offer
        if selected_source == cheap_source:
            assert winner.offer_id == a.id and winner.delivered_total == Decimal("304")
            assert winner.shipping_price == Decimal("2")
            assert winner.tax_status == "additional" and winner.additional_tax == Decimal("3")
            assert winner.url == a.url
        else:
            assert winner.offer_id == b.id and winner.delivered_total == Decimal("309")
            assert winner.shipping_price == Decimal("0") and winner.tax_status == "included"
            assert winner.additional_tax is None and winner.url == b.url
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 2
