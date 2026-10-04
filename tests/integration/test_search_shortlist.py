from itertools import count
from uuid import UUID

import pytest
from sqlalchemy import func, select

from pricehunter.db.models import Product, StoreOffer
from pricehunter.providers.mock import MockStoreProvider
from tests.support import market_user
from tests.unit.test_search_relevance import QUERY, offer

pytestmark = pytest.mark.integration


async def test_real_canonical_search_keeps_exact_model_with_highest_controlled_uuid(
    container, monkeypatch
):
    from pricehunter.services import catalog_resolver

    sequence = count(1)
    monkeypatch.setattr(catalog_resolver, "uuid4", lambda: UUID(int=next(sequence)))
    weak = [
        offer(f"early-{i:03}", title=f"Unrelated retrieved product {i}", store_slug="aaa")
        for i in range(99)
    ]
    exact = offer(
        "zzz-exact",
        title=QUERY,
        brand="Sony",
        model="WH-1000XM6",
        gtin="4548736162563",
        store_slug="zzz",
    )

    class Retrieved(MockStoreProvider):
        async def search(self, *args, **kwargs):
            return [*weak, exact]

    container.registry.providers = {"mock": Retrieved()}
    container.settings.search_comparison_limit = 10
    user = await market_user(container, 781001, "BE")
    built = []
    original = container.products.product

    async def product(product_id, *args, **kwargs):
        built.append(product_id)
        return await original(product_id, *args, **kwargs)

    monkeypatch.setattr(container.products, "product", product)
    result = await container.search.search(QUERY, user.id)
    async with container.sessions() as session:
        target = (await session.scalars(select(Product).where(Product.model == "WH-1000XM6"))).one()
        ids = list(await session.scalars(select(Product.id)))
        assert len(ids) == 100 and max(ids) == target.id
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 100
    assert len(built) == 10 and target.id in built
    assert result.products[0].id == target.id
