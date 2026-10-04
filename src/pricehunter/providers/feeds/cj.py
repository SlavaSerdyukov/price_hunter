import json
import re
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from pydantic import SecretStr, ValidationError

from pricehunter.db.base import utcnow
from pricehunter.db.models import MerchantProgram
from pricehunter.domain.errors import PriceHunterError
from pricehunter.domain.feeds import FeedError, FeedProductData, FeedReference, RejectedFeedRow
from pricehunter.providers.feeds.base import FeedSource
from pricehunter.providers.feeds.normalization import stock, timestamp, validate_links
from pricehunter.providers.feeds.streaming import FeedHTTP

API = "https://ads.api.cj.com/query"
FEED_FIELDS = (
    "adId advertiserId advertiserName advertiserCountry currency language "
    "feedName lastUpdated productCount"
)
PRODUCT_FIELDS = """__typename id adId advertiserId advertiserCountry title description brand
    price { amount currency } salePrice { amount currency }
    salePriceEffectiveDateStart salePriceEffectiveDateEnd
    imageLink link lastUpdated targetCountry serviceableAreas isDeleted
    linkCode(pid: PID) { clickUrl }
    ... on Shopping { gtin mpn availability itemGroupId color size material pattern
        sizeType sizeSystem productType
        shipping { country postalCode region locationId locationGroupName service
            price { amount currency } } }"""


class CJFeedSource(FeedSource):
    name = "cj"

    def __init__(
        self,
        http: FeedHTTP,
        token: SecretStr,
        company_id: str,
        website_id: str,
        *,
        page_size: int = 500,
        max_pages: int = 2000,
    ) -> None:
        if not token.get_secret_value().strip() or not all(
            re.fullmatch(r"[0-9]{1,30}", value) for value in (company_id, website_id)
        ):
            raise ValueError("CJ requires CJ_API_TOKEN, CJ_COMPANY_ID and CJ_WEBSITE_ID (PID)")
        if not 1 <= page_size <= 1000 or not 1 <= max_pages <= 2000:
            raise ValueError("Unbounded CJ pagination")
        self.http, self.token, self.company_id, self.website_id = (
            http,
            token,
            company_id,
            website_id,
        )
        self.page_size, self.max_pages = page_size, max_pages

    async def _query(self, name: str, args: dict[str, Any], fields: str) -> dict[str, Any]:
        arguments = ",".join(f"{key}:{json.dumps(value)}" for key, value in args.items())
        query = "{" + name + "(" + arguments + "){" + fields + "}}"
        if len(query.encode()) > 8192:
            raise FeedError("query_size_limit")
        response = await self.http.json(
            self.name,
            API,
            domains={"ads.api.cj.com"},
            method="POST",
            headers={"Authorization": "Bearer " + self.token.get_secret_value()},
            json={"query": query},
        )
        if not isinstance(response, dict):
            raise FeedError("invalid_schema")
        data = response.get("data")
        errors = response.get("errors") or []
        if not isinstance(errors, list) or len(errors) > 20:
            raise FeedError("graphql_error_limit")
        value = data.get(name) if isinstance(data, dict) else None
        if not isinstance(value, dict) or not isinstance(value.get("resultList"), list):
            raise FeedError("graphql_request_failed" if errors else "invalid_schema")
        nodes = value["resultList"]
        if len(nodes) > self.page_size:
            raise FeedError("page_size_limit")
        rejected = set()
        for error in errors:
            path = error.get("path") if isinstance(error, dict) else None
            if (
                not isinstance(path, list)
                or len(path) < 3
                or path[:2] != [name, "resultList"]
                or (type(path[2]) is not int or not 0 <= path[2] < len(nodes))
            ):
                # Unresolved merchant/request errors cannot certify a complete generation.
                raise FeedError("graphql_request_failed")
            rejected.add(path[2])
        for index in rejected:
            nodes[index] = None
        count, total = value.get("count"), value.get("totalCount")
        if type(count) is not int or type(total) is not int or count != len(nodes) or total < count:
            raise FeedError("inconsistent_page")
        return value

    async def _feeds(self, merchant_id: str | None = None) -> list[FeedReference]:
        refs: list[FeedReference] = []
        total = None
        for _ in range(min(self.max_pages, 5000)):
            args: dict[str, Any] = {
                "companyId": self.company_id,
                "limit": self.page_size,
                "offset": len(refs) + 1,
            }
            if merchant_id:
                args["partnerIds"] = [merchant_id]
            result = await self._query(
                "shoppingProductFeeds", args, "totalCount count resultList {" + FEED_FIELDS + "}"
            )
            if total is not None and total != result["totalCount"]:
                raise FeedError("inconsistent_page")
            total = result["totalCount"]
            if total > 5000:
                raise FeedError("feed_list_limit")
            for raw in result["resultList"]:
                if not isinstance(raw, dict):
                    raise FeedError("invalid_feed_list")
                ids = [str(raw.get(key, "")) for key in ("adId", "advertiserId")]
                if not all(re.fullmatch(r"[0-9]{1,30}", value) for value in ids):
                    raise FeedError("invalid_feed_list")
                if merchant_id and ids[1] != merchant_id:
                    raise FeedError("wrong_advertiser")
                refs.append(
                    FeedReference(
                        ids[0],
                        ids[1],
                        str(raw.get("advertiserName") or "")[:100],
                        market_country=raw.get("advertiserCountry"),
                        version=raw.get("lastUpdated"),
                        currency=raw.get("currency"),
                    )
                )
            if len(refs) == total:
                return refs
            if not result["count"] or len(refs) > total:
                raise FeedError("truncated_page")
        raise FeedError("page_limit")

    async def discover_feeds(self) -> list[FeedReference]:
        return await self._feeds()

    async def source_version(self, program: MerchantProgram) -> str | None:
        for ref in await self._feeds(program.external_merchant_id):
            if ref.feed_id == program.external_feed_id and ref.currency == program.currency:
                if ref.version is not None and (
                    not isinstance(ref.version, str) or len(ref.version) > 200
                ):
                    raise FeedError("invalid_source_version")
                return ref.version
        raise FeedError("feed_not_authorized")

    async def stream_items(
        self, program: MerchantProgram
    ) -> AsyncIterator[FeedProductData | RejectedFeedRow]:
        if not all(
            re.fullmatch(r"[0-9]{1,30}", v)
            for v in (program.external_merchant_id, program.external_feed_id)
        ):
            raise FeedError("invalid_feed_reference")
        consumed, total, cursor = 0, None, None
        cursors: set[str] = set()
        fields = PRODUCT_FIELDS.replace("PID", json.dumps(self.website_id))
        for _ in range(self.max_pages):
            args: dict[str, Any] = {
                "companyId": self.company_id,
                "partnerIds": [program.external_merchant_id],
                "adIds": [program.external_feed_id],
                "limit": self.page_size,
                "includeDeletedProducts": False,
            }
            if cursor is not None:
                args["page"] = cursor
            else:
                if consumed >= 10000:
                    raise FeedError("missing_page_cursor")
                args["offset"] = consumed + 1
            result = await self._query(
                "products", args, "totalCount count nextPage resultList {" + fields + "}"
            )
            if total is not None and total != result["totalCount"]:
                raise FeedError("inconsistent_page")
            total = result["totalCount"]
            if total > self.http.bounds.rows:
                raise FeedError("row_limit")
            for raw in result["resultList"]:
                if isinstance(raw, dict) and (
                    str(raw.get("advertiserId")) != program.external_merchant_id
                    or str(raw.get("adId")) != program.external_feed_id
                ):
                    raise FeedError("wrong_advertiser")
                try:
                    if (
                        not isinstance(raw, dict)
                        or raw.get("__typename") != "Shopping"
                        or raw.get("isDeleted")
                    ):
                        raise ValueError("Invalid shopping product")
                    yield self.normalize(raw, program)
                except (
                    ValueError,
                    ArithmeticError,
                    KeyError,
                    TypeError,
                    ValidationError,
                    PriceHunterError,
                ):
                    yield RejectedFeedRow("invalid_product")
            consumed += result["count"]
            if consumed == total:
                return
            if not result["count"] or consumed > total:
                raise FeedError("truncated_page")
            cursor = result.get("nextPage")
            if cursor is not None:
                if (
                    not isinstance(cursor, str)
                    or not cursor
                    or len(cursor) > 4096
                    or cursor in cursors
                ):
                    raise FeedError("invalid_page_cursor")
                cursors.add(cursor)
        raise FeedError("page_limit")

    @staticmethod
    def normalize(raw: dict[str, Any], program: MerchantProgram) -> FeedProductData:
        regular = raw["price"]
        amount = Decimal(str(regular["amount"]))
        if regular["currency"] != program.currency:
            raise ValueError("Wrong currency")
        current, original = amount, None
        sale = raw.get("salePrice")
        start, end = (
            timestamp(raw.get("salePriceEffectiveDateStart")),
            timestamp(raw.get("salePriceEffectiveDateEnd")),
        )
        now = utcnow()
        if sale and (start is None or start <= now) and (end is None or now < end):
            if sale.get("currency") != program.currency:
                raise ValueError("Wrong sale currency")
            current = Decimal(str(sale["amount"]))
            if current >= amount:
                raise ValueError("Invalid sale price")
            original = amount
        if raw.get("targetCountry") and raw["targetCountry"] != program.market_country:
            raise ValueError("Different target market")
        shipping = raw.get("shipping") or {}
        metadata = {
            "description": str(raw.get("description") or "")[:2000],
            "advertiser_country": raw.get("advertiserCountry"),
            "target_country": raw.get("targetCountry"),
            "serviceable_areas": raw.get("serviceableAreas"),
            "category": (raw.get("productType") or [])[:5],
        }
        delivery = None
        shipping_price = shipping.get("price") or {}
        if (
            shipping.get("country") == program.market_country
            and not any(
                shipping.get(k) for k in ("postalCode", "region", "locationId", "locationGroupName")
            )
            and (
                shipping_price.get("currency") == program.currency
                and shipping_price.get("amount") is not None
            )
        ):
            delivery = Decimal(str(shipping_price["amount"]))
        return validate_links(
            FeedProductData(
                external_id=str(raw["id"]),
                title=raw["title"],
                brand=raw.get("brand"),
                mpn=raw.get("mpn"),
                gtin=raw.get("gtin"),
                price=current,
                original_price=original,
                currency=program.currency,
                availability=stock(raw.get("availability")),
                affiliate_url=raw["linkCode"]["clickUrl"],
                direct_url=raw.get("link"),
                image_url=raw.get("imageLink"),
                parent_external_id=raw.get("itemGroupId"),
                variant={
                    key: str(raw[key])
                    for key in ("size", "color", "material", "pattern", "sizeType", "sizeSystem")
                    if raw.get(key)
                },
                source_updated_at=timestamp(raw.get("lastUpdated")),
                delivery_cost=delivery,
                delivery_country=program.market_country if delivery is not None else None,
                delivery_scope="country" if delivery is not None else "unknown",
                delivery_currency=program.currency if delivery is not None else None,
                metadata=metadata,
            ),
            program,
        )
