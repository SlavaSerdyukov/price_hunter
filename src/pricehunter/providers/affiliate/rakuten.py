import asyncio
import base64
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

from pydantic import ValidationError

from pricehunter.core.limits import RateLimiter
from pricehunter.core.security import validate_url
from pricehunter.core.xml import parse_xml
from pricehunter.domain.discovery import Capability, DiscoveryQuery
from pricehunter.domain.errors import (
    InvalidProductUrlError,
    ProviderHTTPError,
    ProviderUnavailableError,
    UnsupportedStoreError,
)
from pricehunter.domain.products import Availability, ProductOfferData, validate_trade_identifier
from pricehunter.providers.base import StoreProvider
from pricehunter.providers.http import ProviderHTTP

API = "https://api.linksynergy.com"
DOMAINS = {"api.linksynergy.com"}
LINK_DOMAINS = {"click.linksynergy.com", "linksynergy.com"}


class RakutenTokenManager:
    def __init__(
        self, http: ProviderHTTP, client_id: str, client_secret: str, account_id: str
    ) -> None:
        self.http, self.account_id = http, account_id
        self._credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._lock = asyncio.Lock()
        self._token = ""
        self._expires_at = 0.0

    async def token(self, rejected: str | None = None) -> str:
        async with self._lock:
            if rejected and self._token == rejected:
                self._expires_at = 0
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            data = await self.http.json(
                "POST",
                API + "/token",
                domains=DOMAINS,
                headers={"Authorization": "Bearer " + self._credentials},
                data={"scope": self.account_id},
            )
            try:
                token, expiry = data["access_token"], int(data["expires_in"])
                if (
                    not isinstance(token, str)
                    or not token.strip()
                    or len(token) > 16384
                    or not 0 < expiry <= 86400
                ):
                    raise ValueError
                self._token = token
                self._expires_at = time.monotonic() + max(0, expiry - min(60, expiry / 10))
            except (ValueError, KeyError, TypeError) as exc:
                raise ProviderUnavailableError() from exc
            return self._token


class RakutenProvider(StoreProvider):
    """Partner-advertiser search; no fabricated stock or item-refresh endpoint."""

    name = "rakuten"
    domains: set[str] = set()
    capabilities = frozenset({Capability.SEARCH_KEYWORD, Capability.SEARCH_MODEL})
    manages_request_limits = True

    def __init__(
        self,
        http: ProviderHTTP,
        tokens: RakutenTokenManager,
        limiter: RateLimiter,
        advertisers: dict[str, list[str]],
        *,
        page_size: int = 20,
        max_pages: int = 2,
        max_results: int = 50,
    ) -> None:
        if not 1 <= page_size <= 100 or not 1 <= max_pages <= 5 or not 1 <= max_results <= 100:
            raise ValueError("Unbounded Rakuten search")
        self.http, self.tokens, self.limiter = http, tokens, limiter
        self.advertisers = advertisers
        self.discovery_countries = frozenset(advertisers)
        self.page_size, self.max_pages, self.max_results = page_size, max_pages, max_results

    def supports_url(self, url: str) -> bool:
        return False

    async def resolve_url(self, url: str) -> ProductOfferData:
        raise UnsupportedStoreError()

    async def discover(self, query: DiscoveryQuery) -> list[ProductOfferData]:
        return await self._search(query.text, query.country, query.currency, exact=True)

    async def search(
        self, query: str, *, country: str | None = None, currency: str | None = None
    ) -> list[ProductOfferData]:
        return await self._search(query, country, currency, exact=False)

    async def _page(self, params: dict[str, str]) -> Element:
        token = await self.tokens.token()
        for attempt in range(2):
            try:
                # Each page and auth retry is charged, across all workers/searches.
                async with self.limiter.provider(self.name):
                    payload = await self.http.bytes(
                        "GET",
                        API + "/productsearch/1.0",
                        domains=DOMAINS,
                        params=params,
                        headers={"Authorization": "Bearer " + token},
                    )
                root = parse_xml(payload)
                if root.tag != "result":
                    raise ProviderUnavailableError()
                return root
            except ProviderHTTPError as exc:
                if attempt or exc.http_status != 401:
                    raise
                token = await self.tokens.token(rejected=token)
        raise ProviderUnavailableError()

    async def _search(
        self, query: str, country: str | None, currency: str | None, *, exact: bool
    ) -> list[ProductOfferData]:
        if country not in self.advertisers:
            return []
        assert country is not None
        query = re.sub(r"[&=?{}\\()\[\]\-;~|$!><*%]", " ", query[:200])
        query = " ".join(query.split())
        if not query:
            return []
        results: dict[tuple[str, str], ProductOfferData] = {}
        pages_used = 0
        for mid in dict.fromkeys(self.advertisers[country]):
            page = 1
            while pages_used < self.max_pages and len(results) < self.max_results:
                root = await self._page(
                    {
                        "exact" if exact else "keyword": query,
                        "mid": mid,
                        "max": str(self.page_size),
                        "pagenumber": str(page),
                    }
                )
                pages_used += 1
                for item in root.findall("item")[: self.page_size]:
                    offer = self._normalize(item, country, mid)
                    if offer and (currency is None or currency == offer.currency):
                        results[(offer.store_slug, offer.external_id)] = offer
                    if len(results) >= self.max_results:
                        break
                try:
                    total = int(root.findtext("TotalPages", "1"))
                    if int(root.findtext("PageNumber", str(page))) != page:
                        raise ValueError
                except ValueError as exc:
                    raise ProviderUnavailableError() from exc
                if page >= total:
                    break
                page += 1
        return list(results.values())

    @staticmethod
    def _normalize(item: Element, country: str, expected_mid: str) -> ProductOfferData | None:
        def value(path: str, size: int = 200) -> str:
            return (item.findtext(path) or "").strip()[:size]

        try:
            mid = value("mid")
            if mid != expected_mid:
                return None  # Only the reviewed partner advertiser for this catalog market.
            link = (item.findtext("linkurl") or "").strip()
            validate_url(link, LINK_DOMAINS)
            retail = item.find("price")
            if retail is None:
                return None
            price, currency = Decimal(retail.text or ""), retail.attrib["currency"]
            if not price.is_finite() or price <= 0:
                return None
            original = None
            sale = item.find("saleprice")
            if sale is not None and (sale.text or "").strip():
                sale_price = Decimal(sale.text or "")
                if not sale_price.is_finite():
                    return None
                if sale_price > 0 and sale.attrib.get("currency") == currency:
                    original = price if sale_price < price else None
                    price = sale_price
            upc, raw_upc = None, value("upccode")
            if raw_upc:
                try:
                    upc = validate_trade_identifier(raw_upc, (12,))
                except ValueError:
                    pass
            sku, link_id = value("sku"), value("linkid")
            # A merchant may expose different catalog prices for the same SKU by market.
            # MID remains merchant identity; the listing includes its catalog context.
            identity = sku or link_id
            if not identity:
                return None
            external_id = f"{country}:{identity}"
            image = (item.findtext("imageurl") or "").strip() or None
            if image:
                try:
                    validate_url(image, {urlsplit(image).hostname or ""})
                except InvalidProductUrlError:
                    image = None
            return ProductOfferData(
                provider="rakuten",
                store_slug=f"rakuten_{mid}",
                store_name=value("merchantname", 100),
                store_domain="click.linksynergy.com",
                external_merchant_id=mid,
                country=country,
                external_id=external_id,
                direct_url=None,
                affiliate_url=link,
                affiliate_network="rakuten",
                affiliate_metadata={
                    "mid": mid,
                    "linkid": link_id,
                    "partner_source": "product_search",
                },
                title=value("productname", 500),
                price=price,
                currency=currency,
                original_price=original,
                availability=Availability.UNKNOWN,
                upc=upc,
                sku=sku or None,
                image_url=image,
                metadata={
                    "category": value("category/primary"),
                    "description": value("description/short", 1000),
                    "raw_upc": raw_upc,
                    "market_country": country,
                },
            )
        except (InvalidOperation, KeyError, ValueError, ValidationError, InvalidProductUrlError):
            return None
