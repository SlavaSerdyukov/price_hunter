from decimal import Decimal
from urllib.parse import urlsplit

from pricehunter.core.security import canonical_url
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


class MockStoreProvider(StoreProvider):
    name = "mock"
    domains = {"mock.pricehunter.test"}

    def supports_url(self, url: str) -> bool:
        return urlsplit(url).hostname in self.domains

    def _offer(self, slug: str, sequence: int = 0) -> ProductOfferData:
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
        return [
            self._offer(slug)
            for slug, data in CATALOG.items()
            if query.casefold() in data[0].casefold() and (not currency or data[1] == currency)
        ]

    async def refresh_offer(self, offer: OfferReference) -> ProductOfferData:
        return self._offer(offer.external_id, offer.refresh_sequence + 1)
