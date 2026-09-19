from typing import Protocol

from pricehunter.domain.products import ProductOfferData


class BestBuyProductsClient(Protocol):
    async def get_product(self, sku: str) -> ProductOfferData: ...
    async def search_products(self, query: str) -> list[ProductOfferData]: ...
