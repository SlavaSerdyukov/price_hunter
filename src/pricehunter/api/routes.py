from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from pricehunter.api.dependencies import ContainerDependency, UserDependency
from pricehunter.db.models import User
from pricehunter.domain.comparison import ComparisonProduct
from pricehunter.domain.delivery import DeliveryContext
from pricehunter.domain.errors import InvalidDeliveryContextError
from pricehunter.domain.markets import CountryCode
from pricehunter.schemas.api import (
    HistoryView,
    OfferView,
    ResolveRequest,
    TrackerCreate,
    TrackerPatch,
    TrackerView,
    UserSettingsPatch,
)
from pricehunter.schemas.watches import WatchCreate, WatchPatch, WatchView
from pricehunter.services.best_price_service import BestHistory
from pricehunter.services.comparison_operations import RefreshAccepted
from pricehunter.services.search_service import SearchResult
from pricehunter.services.subscription_service import SubscriptionView

router = APIRouter(prefix="/api/v1")


def destination(user: User, data: DeliveryContext | None) -> DeliveryContext:
    if data is not None:
        return data
    if user.delivery_country is None:
        raise InvalidDeliveryContextError()
    return DeliveryContext(country=user.delivery_country, postal_code=user.delivery_postal_code)


@router.post("/products/{product_id}/delivery-comparison", response_model=ComparisonProduct)
async def delivery_comparison(
    product_id: UUID,
    container: ContainerDependency,
    user: UserDependency,
    data: DeliveryContext | None = None,
    country: CountryCode | None = None,
    page: Annotated[int, Query(ge=0, le=10000)] = 0,
    size: Annotated[int, Query(ge=1, le=50)] = 10,
) -> ComparisonProduct:
    # Destination belongs in the request body, never a postal-bearing access-log URL.
    return await container.products.comparisons.get(
        product_id,
        user.id,
        market_country=country,
        delivery_context=destination(user, data),
        page=page,
        size=size,
    )


@router.post("/products/{product_id}/delivery-quote", response_model=ComparisonProduct)
async def delivery_quote(
    product_id: UUID,
    container: ContainerDependency,
    user: UserDependency,
    data: DeliveryContext | None = None,
    country: CountryCode | None = None,
) -> ComparisonProduct:
    return await container.delivery.request(
        product_id, user.id, destination(user, data), market_country=country
    )


@router.post("/products/resolve", response_model=OfferView)
async def resolve(
    data: ResolveRequest, container: ContainerDependency, user: UserDependency
) -> OfferView:
    return await container.products.resolve(data.url, user.id)


@router.get("/products/{product_id}", response_model=ComparisonProduct)
async def product(
    product_id: UUID,
    container: ContainerDependency,
    user: UserDependency,
    country: CountryCode | None = None,
) -> ComparisonProduct:
    return await container.products.product(product_id, user.id, market_country=country)


@router.get("/products/{product_id}/offers", response_model=ComparisonProduct)
async def product_offers(
    product_id: UUID,
    container: ContainerDependency,
    user: UserDependency,
    page: Annotated[int, Query(ge=0, le=10000)] = 0,
    size: Annotated[int, Query(ge=1, le=50)] = 10,
    country: CountryCode | None = None,
) -> ComparisonProduct:
    return await container.products.comparisons.get(
        product_id, user.id, page=page, size=size, market_country=country
    )


@router.get("/products/{product_id}/best-price-history", response_model=BestHistory)
async def best_price_history(
    product_id: UUID,
    container: ContainerDependency,
    user: UserDependency,
    currency: Annotated[str, Query(pattern=r"^[A-Z]{3}$")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    country: CountryCode | None = None,
) -> BestHistory:
    return await container.best_prices.history(
        product_id, user.id, currency, limit, market_country=country
    )


@router.post("/products/{product_id}/refresh", response_model=RefreshAccepted, status_code=202)
async def request_refresh(
    product_id: UUID,
    container: ContainerDependency,
    user: UserDependency,
    country: CountryCode | None = None,
) -> RefreshAccepted:
    return await container.comparison_operations.request_refresh(
        product_id, user.id, market_country=country
    )


@router.get("/product-watches", response_model=list[WatchView])
async def watches(
    container: ContainerDependency,
    user: UserDependency,
    page: Annotated[int, Query(ge=0, le=10000)] = 0,
    size: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[WatchView]:
    return await container.watches.list(user.id, page, size)


@router.post("/product-watches", response_model=WatchView, status_code=201)
async def create_watch(
    data: WatchCreate, container: ContainerDependency, user: UserDependency
) -> WatchView:
    return await container.watches.create(user.id, data)


@router.patch("/product-watches/{watch_id}", response_model=WatchView)
async def update_watch(
    watch_id: UUID, data: WatchPatch, container: ContainerDependency, user: UserDependency
) -> WatchView:
    return await container.watches.update(user.id, watch_id, data)


@router.delete("/product-watches/{watch_id}", status_code=204)
async def delete_watch(
    watch_id: UUID, container: ContainerDependency, user: UserDependency
) -> Response:
    await container.watches.delete(user.id, watch_id)
    return Response(status_code=204)


@router.get("/products/{product_id}/history", response_model=HistoryView)
async def history(
    product_id: UUID,
    offer_id: UUID,
    container: ContainerDependency,
    user: UserDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> HistoryView:
    from pricehunter.domain.errors import ProductNotFoundError

    offer = await container.products.offer(offer_id)
    if offer.product_id != product_id:
        raise ProductNotFoundError()
    return await container.products.history(offer_id, user.id, limit)


@router.get("/search", response_model=SearchResult)
async def search(
    container: ContainerDependency,
    user: UserDependency,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    country: CountryCode | None = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
) -> SearchResult:
    return await container.search.search(
        q, user.id, country=country or user.country_code, currency=currency
    )


@router.get("/trackers", response_model=list[TrackerView])
async def trackers(
    container: ContainerDependency,
    user: UserDependency,
    page: Annotated[int, Query(ge=0, le=10000)] = 0,
    size: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[TrackerView]:
    return await container.trackers.list(user.id, page=page, size=size)


@router.post("/trackers", response_model=TrackerView, status_code=201)
async def create_tracker(
    data: TrackerCreate,
    container: ContainerDependency,
    user: UserDependency,
) -> TrackerView:
    return await container.trackers.create(user.id, data)


@router.patch("/trackers/{tracker_id}", response_model=TrackerView)
async def update_tracker(
    tracker_id: UUID,
    data: TrackerPatch,
    container: ContainerDependency,
    user: UserDependency,
) -> TrackerView:
    return await container.trackers.update(user.id, tracker_id, data)


@router.delete("/trackers/{tracker_id}", status_code=204)
async def delete_tracker(
    tracker_id: UUID, container: ContainerDependency, user: UserDependency
) -> Response:
    await container.trackers.delete(user.id, tracker_id)
    return Response(status_code=204)


@router.get("/subscriptions/me", response_model=SubscriptionView)
async def subscription(container: ContainerDependency, user: UserDependency) -> SubscriptionView:
    return await container.subscriptions.status(user.id)


class SettingsView(BaseModel):
    model_config = {"from_attributes": True}
    language_code: str
    country_code: str | None
    preferred_currency: str
    timezone: str
    delivery_country: str | None
    delivery_postal_code: str | None


@router.get("/users/me/settings", response_model=SettingsView)
async def read_settings(user: UserDependency) -> SettingsView:
    return SettingsView.model_validate(user)


@router.patch("/users/me/settings", response_model=SettingsView)
async def settings(
    data: UserSettingsPatch,
    container: ContainerDependency,
    user: UserDependency,
) -> SettingsView:
    updated = await container.users.settings(user.id, data)
    return SettingsView.model_validate(updated)
