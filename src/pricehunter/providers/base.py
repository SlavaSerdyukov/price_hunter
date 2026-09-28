from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pricehunter.domain.discovery import Capability, DiscoveryQuery
from pricehunter.domain.products import ProductOfferData


@dataclass(frozen=True)
class OfferReference:
    url: str
    external_id: str
    store_slug: str
    refresh_sequence: int
    metadata: dict[str, Any]


class StoreProvider(ABC):
    name: str
    domains: set[str]
    capabilities: frozenset[Capability] = frozenset({Capability.URL_RESOLVE, Capability.REFRESH})
    discovery_interval_seconds: int | None = None
    discovery_countries: frozenset[str] = frozenset()
    # Multi-page APIs account for every HTTP request internally using the shared limiter.
    manages_request_limits: bool = False

    async def discover(self, query: DiscoveryQuery) -> list[ProductOfferData]:
        return await self.search(query.text, country=query.country, currency=query.currency)

    async def discovery_details(self, offer: ProductOfferData) -> ProductOfferData:
        """Opt-in enrichment for summary-only search APIs, charged as a separate operation."""
        return await self.refresh_offer(
            OfferReference(offer.url, offer.external_id, offer.store_slug, 0, offer.metadata)
        )

    @abstractmethod
    def supports_url(self, url: str) -> bool: ...

    @abstractmethod
    async def resolve_url(self, url: str) -> ProductOfferData: ...

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        country: str | None = None,
        currency: str | None = None,
    ) -> list[ProductOfferData]: ...

    async def refresh_offer(self, offer: OfferReference) -> ProductOfferData:
        return await self.resolve_url(offer.url)
