from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from pricehunter.domain.products import Availability, model_code, normalized


class ComparisonOffer(BaseModel):
    offer_id: UUID
    store: str
    store_slug: str
    store_country: str
    title: str
    price: Decimal
    currency: str
    availability: Availability
    url: str
    image_url: str | None
    last_checked_at: datetime
    shipping_price: Decimal | None = None
    delivery_country: str | None = None
    tax_included: bool | None = None
    total_price: Decimal | None = None


class CurrencyComparison(BaseModel):
    currency: str
    best_available_offer: ComparisonOffer | None
    cheapest_known_offer: ComparisonOffer
    price_spread: Decimal | None


class ComparisonProduct(BaseModel):
    id: UUID
    canonical_name: str
    brand: str | None
    gtin: str | None = None
    model: str | None
    variant: dict[str, str]
    image_url: str | None
    offers: list[ComparisonOffer]
    currencies: list[str]
    currency_groups: list[CurrencyComparison]
    best_available_offer: ComparisonOffer | None
    price_spread: Decimal | None
    match_confidence: Decimal
    store_count: int
    offer_count: int
    page: int = 0
    page_size: int = 10


def offer_order(offer: ComparisonOffer) -> tuple[str, int, Decimal, str, str]:
    return (
        offer.currency,
        {Availability.IN_STOCK: 0, Availability.UNKNOWN: 1, Availability.OUT_OF_STOCK: 2}[
            offer.availability
        ],
        offer.price,
        offer.store_slug,
        str(offer.offer_id),
    )


def currency_comparisons(offers: list[ComparisonOffer]) -> list[CurrencyComparison]:
    groups = []
    for currency in sorted({offer.currency for offer in offers}):
        same = sorted(
            (o for o in offers if o.currency == currency),
            key=lambda o: (o.price, o.store_slug, str(o.offer_id)),
        )
        available = [o for o in same if o.availability == Availability.IN_STOCK]
        groups.append(
            CurrencyComparison(
                currency=currency,
                best_available_offer=available[0] if available else None,
                cheapest_known_offer=same[0],
                price_spread=available[-1].price - available[0].price if available else None,
            )
        )
    return groups


def comparison_rank(
    product: ComparisonProduct, query: str
) -> tuple[int, int, Decimal, int, str, str]:
    query_code = model_code(query)
    model = model_code(product.model or "")
    exact = bool(model and query_code in (model, model_code(product.brand or "") + model))
    exact = exact or bool(product.gtin and query.strip().zfill(14) == product.gtin)
    title = normalized(product.canonical_name)
    relevance = 2 if exact else int(bool(query.strip()) and normalized(query) in title)
    # No numeric price comparison between currencies or unrelated canonical products.
    return (
        -relevance,
        -int(any(g.best_available_offer for g in product.currency_groups)),
        -product.match_confidence,
        -product.store_count,
        title,
        str(product.id),
    )
