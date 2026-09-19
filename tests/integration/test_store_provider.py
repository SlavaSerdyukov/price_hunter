import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from pricehunter.db.models import NotificationEvent, StoreOffer
from pricehunter.providers.http import ProviderHTTP
from pricehunter.providers.woocommerce import WooCommerceProvider
from pricehunter.schemas.api import TrackerCreate

pytestmark = pytest.mark.integration


async def test_woo_variant_survives_persistence_and_price_refresh(container, respx_mock):
    fixtures = Path(__file__).parents[1] / "fixtures"
    parent = json.loads((fixtures / "woo_variable.json").read_text())
    child = json.loads((fixtures / "woo_variation.json").read_text())
    async with httpx.AsyncClient() as client:
        adapter = WooCommerceProvider(
            ProviderHTTP(client, timeout=5, max_bytes=10000), "raspberrypi_dk"
        )
        container.registry.providers[adapter.name] = adapter
        respx_mock.get(adapter.api).respond(200, json=[parent])
        refresh = respx_mock.get(f"{adapter.api}/{child['id']}").respond(200, json=child)
        user = await container.users.telegram(456)
        offer = await container.products.resolve(
            parent["permalink"] + "?attribute_capacity=256GB", user.id
        )
        tracker = await container.trackers.create(user.id, TrackerCreate(store_offer_id=offer.id))
        assert tracker.check_interval_seconds == container.settings.free_check_seconds
        assert offer.price == Decimal("499") and offer.currency == "DKK"
        child["prices"]["price"] = "45900"
        refresh.respond(200, json=child)
        claims = await container.price_checks.claim_due()
        assert len(claims) == 1 and await container.price_checks.refresh(claims[0])
        async with container.sessions() as session:
            stored = await session.get(StoreOffer, offer.id)
            assert stored.external_id == "348692" and stored.price == Decimal("459")
            assert stored.metadata_json == {"parent_id": 348678}
            events = list(await session.scalars(select(NotificationEvent)))
            assert len(events) == 1 and events[0].currency == "DKK"
            assert events[0].price == Decimal("459")
