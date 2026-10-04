"""Exercise real search orchestration with controlled canonical IDs and counted DB work.

Persistence/comparison substitutes make the UUID-cutoff failure deterministic. Existing
integration tests cover the actual resolver, staging, snapshots and policy transactions.
"""

from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.domain.comparison import ComparisonProduct
from pricehunter.domain.products import ProductOfferData
from pricehunter.domain.subscriptions import EffectiveEntitlement, Plan, PlanEntitlements
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.services.search_service import SearchService

QUERY = "Sony WH-1000XM6"


def offer(external_id, *, provider="mock", **changes):
    return ProductOfferData(
        **{
            "provider": provider,
            "store_slug": provider + "-store",
            "store_name": "Actual merchant",
            "store_domain": "example.com",
            "external_id": external_id,
            "country": "BE",
            "direct_url": "https://example.com/" + external_id,
            "title": "Unrelated retrieved product",
            "price": "100",
            "currency": "EUR",
            **changes,
        }
    )


class Results(MockStoreProvider):
    manages_request_limits = True

    def __init__(self, name, rows):
        self.name, self.rows = name, rows

    async def search(self, *args, **kwargs):
        return self.rows


class ControlledProducts:
    def __init__(self, id_for):
        self.id_for = id_for
        self.persisted = []
        self.built = []
        self.data = {}

    @asynccontextmanager
    async def sessions(self):
        async def get(*args):
            return SimpleNamespace(country_code="BE")

        yield SimpleNamespace(get=get)

    async def persist(self, data, **kwargs):
        self.persisted.append(data)
        product_id = self.id_for(data)
        self.data[product_id] = data
        return SimpleNamespace(product_id=product_id)

    async def product(self, product_id, user_id, *, market_country):
        self.built.append(product_id)
        data = self.data[product_id]
        assert data.country == market_country
        return ComparisonProduct(
            id=product_id,
            canonical_name=data.title,
            market_country=market_country,
            brand=data.brand,
            model=data.model,
            gtin=data.gtin.zfill(14) if data.gtin else None,
            variant=data.variant,
            image_url=None,
            offers=[],
            currencies=[],
            currency_groups=[],
            best_available_offer=None,
            price_spread=None,
            match_confidence=Decimal(1),
            store_count=1,
            offer_count=1,
        )


class Entitlements:
    def __init__(self, result_limit=10):
        self.limits = PlanEntitlements(
            max_trackers=2,
            check_interval_seconds=43200,
            search_limit=100,
            search_result_limit=result_limit,
        )

    async def for_user(self, user_id):
        return EffectiveEntitlement(plan=Plan.FREE, entitlements=self.limits)


def search_service(redis, providers, products, *, result_limit=10, **bounds):
    settings = Settings(_env_file=None, user_requests_per_minute=100, **bounds)
    return SearchService(
        ProviderRegistry(providers),
        RateLimiter(redis, settings),
        Entitlements(result_limit),
        products,
    )


async def test_exact_model_survives_100_candidates_with_deliberately_late_uuid(redis):
    weak = [
        offer(f"early-{i:03}", title=f"Unrelated product {i}", store_slug="aaa") for i in range(99)
    ]
    exact = offer("zzz-exact", title=QUERY, brand="Sony", model="WH-1000XM6", store_slug="zzz")
    ids = {data.external_id: UUID(int=i + 1) for i, data in enumerate(weak)}
    ids[exact.external_id] = UUID(int=10000)
    products = ControlledProducts(lambda data: ids[data.external_id])
    service = search_service(
        redis, [Results("mock", weak + [exact])], products, search_comparison_limit=10
    )
    result = await service.search(QUERY, UUID(int=1))
    assert len(products.persisted) == 100 and len(products.built) == 10
    assert ids[exact.external_id] in products.built
    assert result.products[0].id == ids[exact.external_id]


async def test_provider_relevance_order_survives_persistence_cap(redis):
    exact = offer("zzz", title=QUERY, brand="Sony", model="WH-1000XM6", store_slug="zzz")
    weak = [offer(str(i), store_slug="aaa") for i in range(20)]
    products = ControlledProducts(
        lambda data: UUID(int=100 if data.external_id == "zzz" else int(data.external_id) + 1)
    )
    service = search_service(
        redis,
        [Results("mock", [exact, *weak])],
        products,
        search_persistence_limit=3,
        search_comparison_limit=1,
    )
    result = await service.search(QUERY, UUID(int=1))
    assert [d.external_id for d in products.persisted] == ["zzz", "0", "1"]
    assert result.products[0].id == UUID(int=100)


@pytest.mark.parametrize("query", [QUERY, "4548736162563"])
async def test_many_weak_results_cannot_crowd_out_other_provider_exact_evidence(redis, query):
    weak = [offer(str(i), provider="a", title=f"Unrelated {i}") for i in range(98)]
    model = offer("model", provider="b", title=QUERY, brand="Sony", model="WH-1000XM6")
    gtin = offer(
        "gtin", provider="c", title=QUERY, brand="Sony", model="WH-1000XM6", gtin="4548736162563"
    )
    if query != QUERY:
        model = offer(
            "model", provider="b", title="Example numeric model", brand="Example", model=query
        )

    def id_for(data):
        if data.provider == "a":
            return UUID(int=int(data.external_id) + 1)
        return UUID(int={"b": 1000, "c": 2000}[data.provider])

    products = ControlledProducts(id_for)
    service = search_service(
        redis,
        [Results("a", weak), Results("b", [model]), Results("c", [gtin])],
        products,
        search_comparison_limit=2,
    )
    result = await service.search(query, UUID(int=1))
    assert len(products.built) == 2 and set(products.built) == {UUID(int=1000), UUID(int=2000)}
    assert products.built[0] == UUID(int=1000 if query == QUERY else 2000)
    assert {p.id for p in result.products} == {UUID(int=1000), UUID(int=2000)}


async def test_duplicate_newest_snapshot_retains_first_provider_relevance_slot(redis):
    first = offer(
        "z",
        title=QUERY,
        brand="Sony",
        model="WH-1000XM6",
        price="329",
        source_updated_at=datetime.fromisoformat("2026-10-01T10:00:00+00:00"),
        affiliate_metadata={"commission": 99999},
    )
    newer = first.model_copy(
        update={
            "price": Decimal("319"),
            "source_updated_at": datetime.fromisoformat("2026-10-01T11:00:00+00:00"),
            "affiliate_metadata": {"commission": 0},
        }
    )
    weak = offer("a")
    products = ControlledProducts(lambda data: UUID(int=1 if data.external_id == "a" else 2))
    provider = Results("mock", [first, weak, newer])
    service = search_service(
        redis, [provider], products, search_persistence_limit=1, search_comparison_limit=1
    )
    result = await service.search(QUERY, UUID(int=1))
    assert [(d.external_id, d.price) for d in products.persisted] == [("z", Decimal("319"))]
    assert result.provider_outcomes[0].duplicate_count == 1
    provider.rows = [newer, weak, first]
    first.affiliate_metadata["commission"], newer.affiliate_metadata["commission"] = 0, 999999
    products.persisted.clear()
    repeated = await service.search(QUERY, UUID(int=1))
    assert [(d.external_id, d.price) for d in products.persisted] == [("z", Decimal("319"))]
    assert repeated.provider_outcomes[0].duplicate_count == 1


async def test_best_evidence_from_multiple_merchants_uses_one_canonical_slot(redis):
    weak = [offer(str(i), provider="a", title=f"Other retrieved {i}") for i in range(20)]
    before = offer(
        "shared", provider="a", store_slug="merchant-first", title="Headphones assortment"
    )
    exact = offer(
        "shared",
        provider="b",
        store_slug="merchant-second",
        title=QUERY,
        brand="Sony",
        model="WH-1000XM6",
    )
    products = ControlledProducts(
        lambda data: UUID(int=1000 if data.external_id == "shared" else int(data.external_id) + 1)
    )
    service = search_service(
        redis,
        [Results("a", [before, *weak]), Results("b", [exact])],
        products,
        search_comparison_limit=1,
    )
    result = await service.search(QUERY, UUID(int=1))
    assert len(products.persisted) == 22 and products.built == [UUID(int=1000)]
    assert len(result.products) == 1 and result.products[0].id == UUID(int=1000)


async def test_local_provider_rank_and_round_robin_survive_small_global_bounds(redis):
    rows = []
    for name in ("a", "b", "c"):
        values = [offer(str(i), provider=name, title=f"Other {name} {i}") for i in range(10)]
        if name != "a":
            values[0] = offer("0", provider=name, title=QUERY, brand="Sony", model="WH-1000XM6")
        rows.append(Results(name, values))
    products = ControlledProducts(
        lambda data: UUID(int={"a": 1, "b": 1000, "c": 2000}[data.provider] + int(data.external_id))
    )
    service = search_service(
        redis, rows, products, search_persistence_limit=4, search_comparison_limit=2
    )
    result = await service.search(QUERY, UUID(int=1))
    assert [d.provider for d in products.persisted] == ["a", "b", "c", "a"]
    assert len(products.built) == 2 and set(products.built) == {UUID(int=1000), UUID(int=2000)}
    assert all("persistence_limit" in o.errors for o in result.provider_outcomes)


async def test_variants_with_shared_family_id_are_not_deduplicated_or_collapsed(redis):
    variants = [(size, color) for size in ("42", "43") for color in ("black", "white")]
    rows = [
        offer(
            "family",
            title="Example Runner",
            brand="Example",
            model="Runner",
            variant={"size": size, "color": color},
        )
        for size, color in variants
    ]
    products = ControlledProducts(
        lambda data: UUID(int=variants.index((data.variant["size"], data.variant["color"])) + 1)
    )
    service = search_service(redis, [Results("mock", rows)], products, search_comparison_limit=4)
    result = await service.search("Example Runner", UUID(int=1))
    assert len(products.persisted) == len(products.built) == len(result.products) == 4
    assert result.provider_outcomes[0].duplicate_count == 0
    assert {(p.variant["size"], p.variant["color"]) for p in result.products} == set(variants)


async def test_wrong_market_or_currency_exact_matches_never_join_shortlist(redis):
    de = offer("shared", country="DE", title=QUERY, brand="Sony", model="WH-1000XM6")
    be = offer("shared", country="BE", title="Other headphones")
    dollar = offer("usd", currency="USD", title=QUERY, brand="Sony", model="WH-1000XM6")
    exact = offer("exact", title=QUERY, brand="Sony", model="WH-1000XM6")
    products = ControlledProducts(lambda data: UUID(int=1000 if data.external_id == "exact" else 1))
    service = search_service(
        redis, [Results("mock", [de, be, dollar, exact])], products, search_comparison_limit=1
    )
    result = await service.search(QUERY, UUID(int=1), country="BE", currency="EUR")
    assert [d.external_id for d in products.persisted] == ["shared", "exact"]
    assert products.built == [UUID(int=1000)]
    assert result.provider_outcomes[0].rejected_count == 2
    assert result.provider_outcomes[0].duplicate_count == 0


async def test_caps_and_entitlement_output_limit_bound_all_stages(redis):
    rows = [offer(str(i), title=f"Other {i}") for i in range(20)]
    products = ControlledProducts(lambda data: UUID(int=int(data.external_id) + 1))
    service = search_service(
        redis,
        [Results("mock", rows)],
        products,
        search_provider_candidate_limit=5,
        search_persistence_limit=4,
        search_comparison_limit=3,
        result_limit=1,
    )
    result = await service.search(QUERY, UUID(int=1))
    assert len(products.persisted) == 4 and len(products.built) == 3 and len(result.products) == 1
    assert set(result.provider_outcomes[0].errors) == {"candidate_limit", "persistence_limit"}


async def test_provider_rank_beats_uuid_when_evidence_is_equal(redis):
    rows_a = [offer("a-first", provider="a"), offer("a-second", provider="a")]
    rows_b = [offer("b-first", provider="b")]
    ids = {"a-first": UUID(int=1000), "a-second": UUID(int=1), "b-first": UUID(int=2000)}
    products = ControlledProducts(lambda data: ids[data.external_id])
    service = search_service(
        redis, [Results("a", rows_a), Results("b", rows_b)], products, search_comparison_limit=2
    )
    await service.search(QUERY, UUID(int=1))
    assert products.built == [ids["a-first"], ids["b-first"]]
