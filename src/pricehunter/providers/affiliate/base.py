from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from pricehunter.domain.products import ProductOfferData


@dataclass(frozen=True)
class AffiliateLink:
    direct_url: str
    affiliate_url: str
    network: str
    click_id: str | None = None


class AffiliateProvider(ABC):
    """Authorized Rakuten/CJ/Awin adapters implement this bounded streaming contract."""

    name: str

    @abstractmethod
    def offers(
        self, *, country: str, cursor: str | None = None
    ) -> AsyncIterator[ProductOfferData]: ...

    @abstractmethod
    async def affiliate_link(self, offer: ProductOfferData) -> AffiliateLink | None: ...
