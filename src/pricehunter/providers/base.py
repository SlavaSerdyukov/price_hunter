from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

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
