"""Amazon Creators API (LwA 3.x / OffersV2), enabled only after tracking approval."""

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlsplit

from pricehunter.core.security import validate_url
from pricehunter.domain.discovery import Capability, DiscoveryQuery
from pricehunter.domain.errors import (
    InvalidProductUrlError,
    ProductNotFoundError,
    ProviderHTTPError,
    ProviderUnavailableError,
    UnsupportedProductError,
)
from pricehunter.domain.products import Availability, ProductOfferData
from pricehunter.providers.base import OfferReference, StoreProvider
from pricehunter.providers.http import ProviderHTTP

MARKETPLACES = {
    "BE": "www.amazon.com.be",
    "DE": "www.amazon.de",
    "FR": "www.amazon.fr",
    "NL": "www.amazon.nl",
    "IT": "www.amazon.it",
    "ES": "www.amazon.es",
    "IE": "www.amazon.ie",
    "PL": "www.amazon.pl",
    "SE": "www.amazon.se",
    "GB": "www.amazon.co.uk",
}
TOKEN_HOSTS = {"3.1": "api.amazon.com", "3.2": "api.amazon.co.uk", "3.3": "api.amazon.co.jp"}
RESOURCES = [
    "itemInfo.title",
    "itemInfo.byLineInfo",
    "itemInfo.productInfo",
    "images.primary.medium",
    "offersV2.listings.price",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
    "offersV2.listings.isBuyBoxWinner",
    "offersV2.listings.merchantInfo",
    "offersV2.listings.dealDetails",
    "offersV2.listings.type",
]


class AmazonCreatorsProvider(StoreProvider):
    capabilities = StoreProvider.capabilities | frozenset(
        {Capability.SEARCH_KEYWORD, Capability.SEARCH_MODEL, Capability.SEARCH_ASIN}
    )
    name = "amazon"
    api = "https://creatorsapi.amazon/catalog/v1/"

    def __init__(
        self,
        http: ProviderHTTP,
        client_id: str,
        client_secret: str,
        markets: dict[str, dict[str, str]],
        *,
        credential_version: str = "3.2",
        partner_tag: str = "",
    ) -> None:
        if credential_version not in TOKEN_HOSTS or not markets:
            raise ValueError("Configure Amazon Creators API 3.x credentials and marketplaces")
        if any(country not in MARKETPLACES for country in markets):
            raise ValueError("Unsupported Amazon marketplace")
        self.tags = {c: m.get("partner_tag", partner_tag).strip() for c, m in markets.items()}
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", tag) for tag in self.tags.values()):
            raise ValueError("Each Amazon marketplace requires its own valid partner_tag")
        self.http, self.client_id, self.client_secret = http, client_id, client_secret
        self.token_host = TOKEN_HOSTS[credential_version]
        self.markets = {c: MARKETPLACES[c] for c in markets}
        self.discovery_countries = frozenset(self.markets)
        self.host_countries = {
            host: c
            for c, domain in self.markets.items()
            for host in (domain, domain.removeprefix("www."))
        }
        self.domains = set(self.host_countries)
        self._token = ""
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    def supports_url(self, url: str) -> bool:
        return urlsplit(url).hostname in self.domains

    def _identify(self, url: str) -> tuple[str, str]:
        parsed = validate_url(url, self.domains)
        match = re.fullmatch(
            r"/(?:[^/]+/)?(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:/ref=[^/]*)?/?",
            parsed.path,
        )
        if not match:
            raise InvalidProductUrlError()
        return self.host_countries[parsed.hostname or ""], match[1]

    async def _access_token(self) -> str:
        async with self._token_lock:
            if time.monotonic() >= self._expires_at:
                data = await self.http.json(
                    "POST",
                    f"https://{self.token_host}/auth/o2/token",
                    domains={self.token_host},
                    json_body={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "scope": "creatorsapi::default",
                    },
                )
                token, expires = data.get("access_token"), data.get("expires_in")
                if (
                    not isinstance(token, str)
                    or not token.strip()
                    or type(expires) is not int
                    or expires <= 0
                    or str(data.get("token_type", "bearer")).lower() != "bearer"
                ):
                    raise ProviderUnavailableError()
                self._token = token
                self._expires_at = time.monotonic() + max(0, expires - 60)
            return self._token

    async def _request(
        self, operation: str, country: str, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        for attempt in range(2):
            token = await self._access_token()
            try:
                return await self.http.json(
                    "POST",
                    self.api + operation,
                    domains={"creatorsapi.amazon"},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "x-marketplace": self.markets[country],
                    },
                    json_body={
                        "marketplace": self.markets[country],
                        "partnerTag": self.tags[country],
                        "resources": RESOURCES,
                        "condition": "New",
                        **parameters,
                    },
                )
            except ProviderHTTPError as exc:
                if exc.http_status != 401 or attempt:
                    raise
                async with self._token_lock:
                    if token == self._token:
                        self._expires_at = 0
        raise ProviderUnavailableError()

    @staticmethod
    def _items(data: dict[str, Any], *, search: bool = False) -> list[Any]:
        # The official GetItems operation and cURL examples use both spellings.
        envelope = (
            data.get("searchResult") if search else data.get("itemsResult", data.get("itemResults"))
        )
        if envelope is None and data.get("errors"):
            errors = data["errors"]
            if isinstance(errors, list) and all(
                isinstance(e, dict) and e.get("code") in ("ItemNotAccessible", "NoResults")
                for e in errors
            ):
                return []
            raise ProviderUnavailableError()
        if not isinstance(envelope, dict) or not isinstance(envelope.get("items"), list):
            raise ProviderUnavailableError()
        return list(envelope["items"])

    def _normalize(self, item: dict[str, Any], country: str) -> ProductOfferData:
        try:
            asin = item["asin"]
            if not isinstance(asin, str) or not re.fullmatch(r"[A-Z0-9]{10}", asin):
                raise ValueError("Invalid ASIN")
            url = item["detailPageURL"]
            if self._identify(url) != (country, asin):
                raise ValueError("Mismatched item link")
            listings = (item.get("offersV2") or {}).get("listings") or []
            # Conditional/Prime/subscription/deal prices must not trigger general alerts.
            listings = [
                listing
                for listing in listings
                if isinstance(listing, dict)
                and listing.get("isBuyBoxWinner") is True
                and listing.get("violatesMAP") is not True
                and not listing.get("type")
                and not listing.get("dealDetails")
                and (listing.get("condition") or {}).get("value") == "New"
            ]
            if len(listings) != 1:
                raise UnsupportedProductError()
            listing = listings[0]
            price = listing["price"]["money"]
            original = (listing["price"].get("savingBasis") or {}).get("money") or {}
            original_amount = original.get("amount")
            if (
                original.get("currency") != price["currency"]
                or not original_amount
                or original_amount <= price["amount"]
            ):
                original_amount = None
            stock = (listing.get("availability") or {}).get("type")
            availability = Availability.UNKNOWN
            if stock in ("IN_STOCK", "INSTOCK", "INSTOCKSCARCE", "IN_STOCK_SCARCE"):
                availability = Availability.IN_STOCK
            elif stock in ("OUT_OF_STOCK", "OUTOFSTOCK", "UNAVAILABLE"):
                availability = Availability.OUT_OF_STOCK
            info = item["itemInfo"]
            product_info = info.get("productInfo") or {}
            variant = {
                key: value["displayValue"]
                for key in ("size", "color")
                if isinstance(value := product_info.get(key), dict)
                and isinstance(value.get("displayValue"), str)
            }
            image = (((item.get("images") or {}).get("primary") or {}).get("medium") or {}).get(
                "url"
            )
            return ProductOfferData(
                provider=self.name,
                store_slug=f"amazon_{country.lower()}",
                store_name="Amazon",
                store_domain=self.markets[country],
                country=country,
                external_id=asin,
                asin=asin,
                url=url,
                title=info["title"]["displayValue"],
                price=price["amount"],
                currency=price["currency"],
                original_price=original_amount,
                availability=availability,
                variant=variant,
                image_url=image
                if isinstance(image, str) and image.startswith("https://")
                else None,
                brand=((info.get("byLineInfo") or {}).get("brand") or {}).get("displayValue"),
                seller=(listing.get("merchantInfo") or {}).get("name"),
                metadata={"offer_type": "featured_new"},
            )
        except (KeyError, ValueError, TypeError, AttributeError, InvalidProductUrlError) as exc:
            raise ProviderUnavailableError() from exc

    async def discover(self, query: DiscoveryQuery) -> list[ProductOfferData]:
        if query.method == "asin":
            try:
                offer = await self._get(query.country, query.text)
            except ProductNotFoundError:
                return []
            return [offer] if offer.currency == query.currency else []
        return await super().discover(query)

    async def _get(self, country: str, asin: str) -> ProductOfferData:
        data = await self._request("getItems", country, {"itemIds": [asin], "itemIdType": "ASIN"})
        items = self._items(data)
        matches = [i for i in items if isinstance(i, dict) and i.get("asin") == asin]
        if not matches:
            if items:
                raise ProviderUnavailableError()
            raise ProductNotFoundError()
        if len(matches) != 1:
            raise ProviderUnavailableError()
        return self._normalize(matches[0], country)

    async def resolve_url(self, url: str) -> ProductOfferData:
        country, asin = self._identify(url)
        return await self._get(country, asin)

    async def refresh_offer(self, offer: OfferReference) -> ProductOfferData:
        country, asin = self._identify(offer.url)
        if offer.external_id != asin or offer.store_slug != f"amazon_{country.lower()}":
            raise ProviderUnavailableError()
        return await self._get(country, asin)

    async def search(
        self,
        query: str,
        *,
        country: str | None = None,
        currency: str | None = None,
    ) -> list[ProductOfferData]:
        country = country or next(iter(self.markets))
        if country not in self.markets:
            return []
        data = await self._request(
            "searchItems", country, {"keywords": query[:200], "itemCount": 5}
        )
        results = []
        for item in self._items(data, search=True):
            if not isinstance(item, dict):
                continue
            try:
                offer = self._normalize(item, country)
            except (ProviderUnavailableError, UnsupportedProductError):
                continue
            if not currency or offer.currency == currency:
                results.append(offer)
        return results
