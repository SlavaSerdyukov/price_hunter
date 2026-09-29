from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from pydantic import SecretStr, ValidationError

from pricehunter.db.models import MerchantProgram
from pricehunter.domain.errors import PriceHunterError
from pricehunter.domain.feeds import FeedError, FeedProductData, FeedReference, RejectedFeedRow
from pricehunter.providers.feeds.base import FeedSource
from pricehunter.providers.feeds.normalization import stock, timestamp, validate_links
from pricehunter.providers.feeds.streaming import FeedHTTP


class TradeDoublerFeedSource(FeedSource):
    name = "tradedoubler"

    def __init__(
        self, http: FeedHTTP, token: SecretStr, *, page_size: int = 500, max_pages: int = 2000
    ) -> None:
        if not token.get_secret_value().strip():
            raise ValueError("TRADEDOUBLER_TOKEN is required")
        self.http, self.token, self.page_size, self.max_pages = http, token, page_size, max_pages

    async def _get(self, path: str) -> dict[str, Any]:
        data = await self.http.json(
            self.name,
            "https://api.tradedoubler.com/1.0/" + path,
            domains={"api.tradedoubler.com"},
            params={"token": self.token.get_secret_value()},
        )
        if not isinstance(data, dict):
            raise FeedError("invalid_schema")
        return data

    async def discover_feeds(self) -> list[FeedReference]:
        data = await self._get("productFeeds.json")
        feeds = data.get("feeds")
        if not isinstance(feeds, list) or len(feeds) > 5000:
            raise FeedError("invalid_feed_list")
        result = []
        for raw in feeds:
            if not isinstance(raw, dict) or not isinstance(raw.get("programs"), list):
                raise FeedError("invalid_feed_list")
            if not raw.get("active") or not raw.get("visible"):
                continue
            for merchant in raw["programs"]:
                result.append(
                    FeedReference(
                        str(raw["feedId"]),
                        str(merchant["programId"]),
                        str(merchant.get("name", raw.get("name", "")))[:100],
                        version=str(raw.get("lastModifiedTime", "")) or None,
                    )
                )
                if len(result) > 5000:
                    raise FeedError("feed_list_limit")
        return result

    async def source_version(self, program: MerchantProgram) -> str | None:
        if not program.external_feed_id.isdigit():
            raise FeedError("invalid_feed_reference")
        info = await self._get(f"productFeeds/{program.external_feed_id}.json")
        feeds = info.get("feeds", [])
        if not any(
            str(f.get("feedId")) == program.external_feed_id
            and f.get("active")
            and f.get("visible")
            and f.get("currencyISOCode") == program.currency
            and any(
                str(p.get("programId")) == program.external_merchant_id
                for p in f.get("programs", [])
            )
            for f in feeds
            if isinstance(f, dict)
        ):
            raise FeedError("feed_not_authorized")
        result = await self._get(
            f"productsUnlimited/lastUpdated.json;fid={program.external_feed_id}"
        )
        version = result.get("lastUpdatedTime")
        if (
            result.get("feedIds") != [int(program.external_feed_id)]
            or not isinstance(version, str)
            or not 0 < len(version) <= 200
        ):
            raise FeedError("invalid_source_version")
        return version

    async def stream_items(
        self, program: MerchantProgram
    ) -> AsyncIterator[FeedProductData | RejectedFeedRow]:
        if not program.external_feed_id.isdigit():
            raise FeedError("invalid_feed_reference")
        total = None
        consumed = 0
        for page in range(self.max_pages):
            result = await self._get(
                f"productsUnlimited.json;fid={program.external_feed_id};page={page};pageSize={self.page_size};sourceproducturl=true"
            )
            header, products = result.get("productHeader"), result.get("products")
            if (
                not isinstance(header, dict)
                or not isinstance(products, list)
                or len(products) > self.page_size
            ):
                raise FeedError("invalid_page")
            hits = header.get("totalHits")
            if type(hits) is not int or hits < 0 or (total is not None and total != hits):
                raise FeedError("inconsistent_page")
            total = hits
            if hits > self.http.bounds.rows:
                raise FeedError("row_limit")
            for product in products:
                if not isinstance(product, dict) or not isinstance(product.get("offers"), list):
                    yield RejectedFeedRow("invalid_product")
                    continue
                if len(product["offers"]) != 1:
                    # Single-feed downloads must not merge advertisers or hide variants.
                    raise FeedError("ambiguous_offers")
                offer = product["offers"][0]
                if (
                    not isinstance(offer, dict)
                    or str(offer.get("feedId")) != program.external_feed_id
                ):
                    raise FeedError("wrong_feed")
                try:
                    yield self.normalize(product, offer, program)
                except (
                    ValueError,
                    ValidationError,
                    ArithmeticError,
                    KeyError,
                    TypeError,
                    PriceHunterError,
                ):
                    yield RejectedFeedRow("invalid_product")
            consumed += len(products)
            if consumed == total:
                return
            if not products or consumed > total:
                raise FeedError("truncated_page")
        raise FeedError("page_limit")

    def normalize(
        self, product: dict[str, Any], offer: dict[str, Any], program: MerchantProgram
    ) -> FeedProductData:
        price = offer.get("price")
        if price is None:
            # Documented publisher example carries the current value in its price list.
            history = offer.get("priceHistory")
            if not isinstance(history, list) or not history:
                raise ValueError("Missing price")
            price = history[-1]["price"]
        if not isinstance(price, dict):
            raise ValueError("Invalid price")
        if price.get("currency") != program.currency:
            raise FeedError("wrong_currency")
        ids = product.get("identifiers") or {}
        custom = {
            str(f["name"]).casefold(): str(f["value"])
            for f in product.get("fields", [])
            if isinstance(f, dict) and "name" in f and "value" in f
        }
        variant = {
            key: custom[key]
            for key in ("size", "color", "colour", "material", "pattern", "capacity")
            if custom.get(key)
        }
        if offer.get("size") or product.get("size"):
            variant["size"] = str(offer.get("size") or product["size"])
        delivery = offer.get("shippingCost")
        return validate_links(
            FeedProductData(
                external_id=str(offer["sourceProductId"]),
                title=product["name"],
                brand=product.get("brand"),
                model=offer.get("model") or product.get("model"),
                mpn=str(ids["mpn"]) if ids.get("mpn") else None,
                ean=str(ids["ean"]) if ids.get("ean") else None,
                upc=str(ids["upc"]) if ids.get("upc") else None,
                price=Decimal(str(price["value"])),
                currency=price["currency"],
                affiliate_url=offer["productUrl"],
                direct_url=offer.get("sourceProductUrl"),
                image_url=(product.get("productImage") or {}).get("url"),
                variant=variant,
                availability=stock(offer.get("availability"), offer.get("inStock")),
                delivery_cost=Decimal(str(delivery))
                if delivery is not None and str(delivery).replace(".", "", 1).isdigit()
                else None,
                source_updated_at=timestamp(offer.get("modified")),
                metadata={"description": str(product.get("description", ""))[:2000]},
            ),
            program,
        )
