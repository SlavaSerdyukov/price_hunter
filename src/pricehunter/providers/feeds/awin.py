from collections.abc import AsyncIterator
from decimal import Decimal
from urllib.parse import quote

from pydantic import SecretStr, ValidationError

from pricehunter.db.models import MerchantProgram
from pricehunter.domain.errors import PriceHunterError
from pricehunter.domain.feeds import FeedError, FeedProductData, FeedReference, RejectedFeedRow
from pricehunter.domain.markets import validate_country
from pricehunter.providers.feeds.base import FeedSource
from pricehunter.providers.feeds.normalization import stock, timestamp, validate_links
from pricehunter.providers.feeds.streaming import FeedHTTP, csv_rows

COLUMNS = (
    "merchant_id",
    "merchant_product_id",
    "product_name",
    "search_price",
    "currency",
    "aw_deep_link",
    "merchant_deep_link",
    "merchant_image_url",
    "brand_name",
    "product_model",
    "model_number",
    "mpn",
    "ean",
    "upc",
    "product_GTIN",
    "colour",
    "size",
    "material",
    "pattern",
    "parent_product_id",
    "product_price_old",
    "rrp_price",
    "delivery_cost",
    "stock_quantity",
    "in_stock",
    "last_updated",
    "description",
    "merchant_category",
)
REQUIRED = {
    "merchant_id",
    "merchant_product_id",
    "product_name",
    "search_price",
    "currency",
    "aw_deep_link",
}


class AwinFeedSource(FeedSource):
    name = "awin"

    def __init__(self, http: FeedHTTP, key: SecretStr) -> None:
        if not key.get_secret_value().strip():
            raise ValueError("AWIN_FEED_API_KEY is required")
        self.http, self.key = http, key

    async def discover_feeds(self) -> list[FeedReference]:
        url = "https://productdata.awin.com/datafeed/list/apikey/" + quote(
            self.key.get_secret_value(), safe=""
        )
        result: list[FeedReference] = []
        async for raw in csv_rows(
            self.http.stream(self.name, url, domains={"productdata.awin.com"}),
            self.http.bounds,
            required={"Advertiser ID", "Feed ID", "Advertiser Name"},
        ):
            if len(result) >= 5000:
                raise FeedError("feed_list_limit")
            merchant, feed = raw["Advertiser ID"], raw["Feed ID"]
            if not merchant.isdigit() or not feed.isdigit():
                raise FeedError("invalid_feed_list")
            result.append(
                FeedReference(
                    feed,
                    merchant,
                    raw["Advertiser Name"][:100],
                    raw.get("Primary Region") or None,
                    raw.get("Last Imported") or None,
                )
            )
        return result

    async def source_version(self, program: MerchantProgram) -> str | None:
        reference = next(
            (
                r
                for r in await self.discover_feeds()
                if r.feed_id == program.external_feed_id
                and r.merchant_id == program.external_merchant_id
            ),
            None,
        )
        if reference is None:
            raise FeedError("feed_not_authorized")
        # Primary Region is catalog metadata, unlike CJ's advertiser domicile.
        if reference.market_country:
            try:
                region = validate_country(reference.market_country)
            except ValueError:
                region = None  # Region labels such as EU are not country claims.
            if region is not None and region != program.market_country:
                raise FeedError("wrong_market")
        return reference.version

    async def stream_items(
        self, program: MerchantProgram
    ) -> AsyncIterator[FeedProductData | RejectedFeedRow]:
        if not program.external_feed_id.isdigit():
            raise FeedError("invalid_feed_reference")
        url = (
            "https://datafeed.api.productserve.com/datafeed/download/apikey/"
            + quote(self.key.get_secret_value(), safe="")
            + "/fid/"
            + program.external_feed_id
            + "/format/csv/language/"
            + program.feed_language
            + "/delimiter/%2C/compression/gzip/adultcontent/1/columns/"
            + quote(",".join(COLUMNS), safe="")
            + "/"
        )
        async for raw in csv_rows(
            self.http.stream(self.name, url, domains={"datafeed.api.productserve.com"}, gzip=True),
            self.http.bounds,
            required=REQUIRED,
        ):
            if raw["merchant_id"] != program.external_merchant_id:
                raise FeedError("wrong_advertiser")
            if raw["currency"] != program.currency:
                raise FeedError("wrong_currency")
            try:
                yield self.normalize(raw, program)
            except (ValueError, ValidationError, ArithmeticError, PriceHunterError):
                yield RejectedFeedRow("invalid_product")

    def normalize(self, raw: dict[str, str], program: MerchantProgram) -> FeedProductData:
        price = Decimal(raw["search_price"])
        old = raw.get("product_price_old") or raw.get("rrp_price")
        original = Decimal(old) if old else None
        fields = {
            target: raw.get(source) or None
            for target, source in {
                "brand": "brand_name",
                "model": "product_model",
                "mpn": "mpn",
                "ean": "ean",
                "upc": "upc",
                "gtin": "product_GTIN",
                "image_url": "merchant_image_url",
                "direct_url": "merchant_deep_link",
                "parent_external_id": "parent_product_id",
            }.items()
        }
        fields["model"] = fields["model"] or raw.get("model_number") or None
        return validate_links(
            FeedProductData(
                **fields,
                external_id=raw["merchant_product_id"],
                title=raw["product_name"],
                price=price,
                currency=raw["currency"],
                original_price=original if original and original > price else None,
                affiliate_url=raw["aw_deep_link"],
                availability=stock(raw.get("in_stock"), raw.get("stock_quantity")),
                variant={
                    key: raw[key]
                    for key in ("size", "colour", "material", "pattern")
                    if raw.get(key)
                },
                delivery_cost=Decimal(raw["delivery_cost"]) if raw.get("delivery_cost") else None,
                source_updated_at=timestamp(raw.get("last_updated")),
                metadata={
                    key: raw[key][:2000]
                    for key in ("description", "merchant_category")
                    if raw.get(key)
                },
            ),
            program,
        )
