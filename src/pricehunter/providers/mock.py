from decimal import Decimal
from urllib.parse import urlsplit

from pricehunter.core.security import canonical_url
from pricehunter.domain.discovery import Capability, DiscoveryQuery
from pricehunter.domain.errors import ProductNotFoundError
from pricehunter.domain.products import Availability, ProductOfferData
from pricehunter.providers.base import OfferReference, StoreProvider

CATALOG = {
    "headphones": ("PriceHunter Studio Headphones", "EUR", "100.00"),
    "coffee-machine": ("PriceHunter Coffee Machine", "EUR", "250.00"),
    "sneakers-42": ("PriceHunter Sneakers · 42", "EUR", "120.00"),
    "sneakers-44": ("PriceHunter Sneakers · 44", "EUR", "120.00"),
    "camera": ("PriceHunter Travel Camera", "USD", "500.00"),
    "keyboard": ("PriceHunter Mechanical Keyboard", "GBP", "80.00"),
}

# Synthetic multi-merchant prices, never presented as live retailer data.
COMPARISON_CATALOG = {
    "sony-a": ("Demo Alpha", "EUR", ("329", "349", "349"), "in_stock", "4006381333931", "black"),
    "sony-b": ("Demo Beta", "EUR", ("345", "319", "319"), "in_stock", "4006381333931", "black"),
    "sony-out": (
        "Demo Outlet",
        "EUR",
        ("100", "100", "100"),
        "out_of_stock",
        "4006381333931",
        "black",
    ),
    "sony-unknown": (
        "Demo Unconfirmed",
        "EUR",
        ("90", "90", "90"),
        "unknown",
        "4006381333931",
        "black",
    ),
    "sony-us": ("Demo US", "USD", ("80", "79", "79"), "in_stock", "4006381333931", "black"),
    "sony-white": (
        "Demo Alpha",
        "EUR",
        ("330", "325", "325"),
        "in_stock",
        "4006381333931",
        "white",
    ),
    "sony-conflict": (
        "Demo Conflict",
        "EUR",
        ("200", "200", "200"),
        "in_stock",
        "5901234123457",
        "black",
    ),
}


class MockStoreProvider(StoreProvider):
    capabilities = StoreProvider.capabilities | frozenset(
        {Capability.SEARCH_KEYWORD, Capability.SEARCH_GTIN, Capability.SEARCH_MODEL}
    )
    name = "mock"
    domains = {"mock.pricehunter.test"}

    def __init__(self, discovery_interval_seconds: int = 600) -> None:
        self.discovery_interval_seconds = discovery_interval_seconds

    async def discover(self, query: DiscoveryQuery) -> list[ProductOfferData]:
        if query.text in ("04006381333931", "4006381333931", "Sony WH-1000XM6"):
            return [self._offer("sony-new")] if query.currency == "EUR" else []
        return await super().discover(query)

    def supports_url(self, url: str) -> bool:
        return urlsplit(url).hostname in self.domains

    def _offer(self, slug: str, sequence: int = 0) -> ProductOfferData:
        if slug in ("sony-new", "sony-old"):
            original = self._offer("sony-a")
            return original.model_copy(
                update={
                    "store_slug": "demo_new" if slug == "sony-new" else "demo_old",
                    "store_name": "Demo New Store" if slug == "sony-new" else "Demo Old Store",
                    "external_id": slug,
                    "url": f"https://mock.pricehunter.test/products/{slug}",
                    "direct_url": f"https://mock.pricehunter.test/products/{slug}",
                    "price": Decimal("315" if sequence == 0 else "310")
                    if slug == "sony-new"
                    else Decimal("299"),
                }
            )
        if slug in COMPARISON_CATALOG:
            store, currency, prices, availability, gtin, color = COMPARISON_CATALOG[slug]
            return ProductOfferData(
                provider=self.name,
                store_slug=store.lower().replace(" ", "_"),
                store_name=store,
                store_domain="mock.pricehunter.test",
                country="US" if currency == "USD" else "DE",
                external_id=slug,
                url=f"https://mock.pricehunter.test/products/{slug}",
                title=f"Sony WH-1000XM6 {color.title()}",
                price=Decimal(prices[min(sequence, 2)]),
                currency=currency,
                availability=Availability(availability),
                brand="Sony",
                model="WH 1000 XM6" if slug == "sony-b" else "WH-1000XM6",
                gtin=gtin,
                variant={"color": color},
            )
        if slug in ("model-a", "model-b", "model-256"):
            capacity = "256 GB" if slug == "model-256" else "128 GB"
            return ProductOfferData(
                provider=self.name,
                store_slug=f"demo_{slug}",
                store_name=f"Demo {slug}",
                store_domain="mock.pricehunter.test",
                country="BE",
                external_id=slug,
                url=f"https://mock.pricehunter.test/products/{slug}",
                title=f"Demo Phone ZX-200 {capacity}",
                price=Decimal("200"),
                currency="EUR",
                availability=Availability.IN_STOCK,
                brand="Demo",
                model="ZX 200" if slug == "model-b" else "ZX-200",
                variant={"capacity": capacity},
            )
        if slug not in CATALOG:
            raise ProductNotFoundError()
        title, currency, base = CATALOG[slug]
        # Persisted refresh_sequence makes this deterministic across process restarts.
        multiplier = (Decimal("1"), Decimal("0.95"), Decimal("0.90"), Decimal("0.85"))[
            min(sequence, 3)
        ]
        return ProductOfferData(
            provider=self.name,
            store_slug="mock_eu",
            store_name="Demo Store",
            store_domain="mock.pricehunter.test",
            country="BE",
            external_id=slug,
            url=f"https://mock.pricehunter.test/products/{slug}",
            title=title,
            price=Decimal(base) * multiplier,
            currency=currency,
            availability=Availability.IN_STOCK,
            brand="PriceHunter",
            model=slug,
            variant={"size": slug.split("-")[-1]} if slug.startswith("sneakers") else {},
        )

    async def resolve_url(self, url: str) -> ProductOfferData:
        clean = canonical_url(url, self.domains)
        parts = urlsplit(clean).path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "products":
            raise ProductNotFoundError()
        return self._offer(parts[1])

    async def search(
        self,
        query: str,
        *,
        country: str | None = None,
        currency: str | None = None,
    ) -> list[ProductOfferData]:
        regular = [
            self._offer(slug)
            for slug, data in CATALOG.items()
            if query.casefold() in data[0].casefold() and (not currency or data[1] == currency)
        ]
        extra = [
            self._offer(slug) for slug in (*COMPARISON_CATALOG, "model-a", "model-b", "model-256")
        ]
        from pricehunter.domain.products import model_code

        return regular + [
            offer
            for offer in extra
            if model_code(query) in model_code(offer.title)
            and (not currency or offer.currency == currency)
        ]

    async def refresh_offer(self, offer: OfferReference) -> ProductOfferData:
        return self._offer(offer.external_id, offer.refresh_sequence + 1)
