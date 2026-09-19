"""Public WooCommerce Store API, restricted to explicitly reviewed shop hosts."""

import re
from dataclasses import dataclass
from decimal import Decimal
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from pricehunter.core.security import validate_url
from pricehunter.domain.errors import (
    InvalidProductUrlError,
    ProductNotFoundError,
    ProviderUnavailableError,
    UnsupportedProductError,
    VariantOption,
    VariantSelectionRequiredError,
)
from pricehunter.domain.products import Availability, ProductOfferData
from pricehunter.providers.base import OfferReference, StoreProvider
from pricehunter.providers.http import ProviderHTTP


@dataclass(frozen=True)
class WooCommerceShop:
    name: str
    domain: str
    country: str
    search_variations: bool = False


SHOPS = {
    "pine64_eu": WooCommerceShop("PINE64 EU", "pine64eu.com", "PL"),
    "raspberrypi_dk": WooCommerceShop("RaspberryPi.dk", "raspberrypi.dk", "DK"),
    "hemptees_be": WooCommerceShop("Hemptees", "hemptees.be", "BE", True),
    "westernshop_be": WooCommerceShop("Western Shop Bruxelles", "westernshop.be", "BE", True),
}


def positive_id(value: object) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)):
        raise ValueError("Invalid product ID")
    return int(str(value))


def amount(value: object, decimals: object) -> Decimal:
    if type(decimals) is not int or not 0 <= decimals <= 4:
        raise ValueError("Invalid currency precision")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,18}", value):
        raise ValueError("Invalid minor-unit amount")
    return Decimal(value).scaleb(-decimals)


def plain_text(value: str) -> str:
    return unescape(re.sub(r"<[^>]*>", "", value)).strip()


class WooCommerceProvider(StoreProvider):
    def __init__(self, http: ProviderHTTP, shop: str) -> None:
        self.http = http
        self.shop = SHOPS[shop]
        self.store_slug = shop
        self.name = "woocommerce_" + shop
        self.domains = {self.shop.domain, "www." + self.shop.domain}
        self.api = f"https://{self.shop.domain}/wp-json/wc/store/v1/products"

    def supports_url(self, url: str) -> bool:
        return urlsplit(url).hostname in self.domains

    def _slug(self, url: str) -> str:
        parsed = validate_url(url, self.domains)
        match = re.fullmatch(
            r"/(?:en/)?(?:product|produkt|produit)/([a-zA-Z0-9%_-]+)/?", parsed.path
        )
        if not match:
            raise InvalidProductUrlError()
        return match[1]

    def _normalize(self, data: dict[str, Any]) -> ProductOfferData:
        try:
            product_id = positive_id(data["id"])
            kind = data.get("type")
            if kind == "variable":
                raise VariantSelectionRequiredError()
            if kind not in ("simple", "variation") or data.get("has_options") is True:
                raise UnsupportedProductError()
            if data.get("is_password_protected") is True:
                raise UnsupportedProductError()
            url = data["permalink"]
            self._slug(url)
            parsed = validate_url(url, self.domains)
            # Only selection parameters belong in the store link; never add-to-cart actions.
            selection = {
                key: values[0]
                for key, values in parse_qs(parsed.query).items()
                if key.startswith("attribute_") and len(values) == 1
            }
            parent = 0
            variant: dict[str, str] = {}
            title = plain_text(data["name"])
            if kind == "variation":
                parent = positive_id(data["parent"])
                description = plain_text(data.get("variation", ""))
                if not description:
                    raise ValueError("Missing variation description")
                title += " — " + description
                variant = {"configuration": description}
                selection["variation_id"] = str(product_id)
            url = urlunsplit(("https", self.shop.domain, parsed.path, urlencode(selection), ""))
            prices = data["prices"]
            if prices.get("price_range") is not None:
                raise UnsupportedProductError()
            price = amount(prices["price"], prices["currency_minor_unit"])
            regular = prices.get("regular_price")
            original = amount(regular, prices["currency_minor_unit"]) if regular else None
            if original is not None and original <= price:
                original = None
            stock = data.get("is_in_stock")
            availability = Availability.UNKNOWN
            if stock is False:
                availability = Availability.OUT_OF_STOCK
            elif stock is True and data.get("is_on_backorder") is not True:
                availability = Availability.IN_STOCK
            images = data.get("images") or []
            image = images[0].get("src") if images else None
            if image and not str(image).startswith("https://"):
                image = None
            brands = data.get("brands") or []
            return ProductOfferData(
                provider=self.name,
                store_slug=self.store_slug,
                store_name=self.shop.name,
                store_domain=self.shop.domain,
                country=self.shop.country,
                external_id=str(product_id),
                url=url,
                title=title,
                price=price,
                original_price=original,
                currency=prices["currency_code"],
                availability=availability,
                image_url=image,
                sku=data.get("sku") or None,
                brand=plain_text(brands[0]["name"]) if brands else None,
                variant=variant,
                metadata={"parent_id": parent},
            )
        except (KeyError, TypeError, ValueError, AttributeError, InvalidProductUrlError) as exc:
            raise ProviderUnavailableError() from exc

    def _selected_variant(self, data: dict[str, Any], url: str) -> int:
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        choices = data.get("variations")
        if not isinstance(choices, list):
            raise ProviderUnavailableError()
        selected = query.get("variation_id")
        if selected:
            try:
                product_id = positive_id(selected[0]) if len(selected) == 1 else 0
            except ValueError as exc:
                raise InvalidProductUrlError() from exc
            choices = [v for v in choices if isinstance(v, dict) and v.get("id") == product_id]
            if len(choices) != 1:
                raise VariantSelectionRequiredError()
            if not any(k.startswith("attribute_") for k in query):
                if not choices[0].get("attributes") or any(
                    not a.get("value") for a in choices[0]["attributes"]
                ):
                    raise VariantSelectionRequiredError()
                return product_id
        requested = {
            k.removeprefix("attribute_"): v for k, v in query.items() if k.startswith("attribute_")
        }
        if not requested or any(len(v) != 1 or not v[0] for v in requested.values()):
            raise VariantSelectionRequiredError()
        # Taxonomy keys (pa_ram) and custom attribute names (capacity) differ in URLs.
        names = {
            (a.get("taxonomy") or re.sub(r"\s+", "-", a["name"].casefold())): a["name"]
            for a in data.get("attributes", [])
            if isinstance(a, dict) and isinstance(a.get("name"), str)
        }
        aliases: dict[tuple[str, str], str] = {
            (a["name"], value.casefold()): term["slug"].casefold()
            for a in data.get("attributes", [])
            if isinstance(a, dict) and isinstance(a.get("name"), str)
            for term in a.get("terms", [])
            if isinstance(term, dict)
            and isinstance(term.get("slug"), str)
            and isinstance(term.get("name"), str)
            for value in (term["name"], term["slug"])
        }

        def canonical(name: str, value: str) -> str:
            return aliases.get((name, value.casefold()), value.casefold())

        matches = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            values = {
                a["name"]: a["value"]
                for a in choice.get("attributes", [])
                if isinstance(a, dict)
                and isinstance(a.get("name"), str)
                and isinstance(a.get("value"), str)
            }
            if all(
                key in names
                and isinstance(values.get(names[key]), str)
                and canonical(names[key], values[names[key]]) == canonical(names[key], value[0])
                for key, value in requested.items()
            ):
                matches.append(choice)
        if len(matches) != 1:
            raise VariantSelectionRequiredError()
        try:
            return positive_id(matches[0]["id"])
        except (ValueError, KeyError) as exc:
            raise ProviderUnavailableError() from exc

    async def resolve_url(self, url: str) -> ProductOfferData:
        slug = self._slug(url)
        items = await self.http.json_list(
            self.api, domains=self.domains, params={"slug": slug, "parent": "0", "per_page": "2"}
        )
        if not items:
            raise ProductNotFoundError()
        if len(items) != 1 or not isinstance(items[0], dict):
            raise ProviderUnavailableError()
        data = items[0]
        try:
            if self._slug(data["permalink"]) != slug:
                raise ProviderUnavailableError()
        except (KeyError, TypeError, InvalidProductUrlError) as exc:
            raise ProviderUnavailableError() from exc
        if data.get("is_password_protected") is True:
            raise UnsupportedProductError()
        if data.get("type") == "variable":
            try:
                variant_id = self._selected_variant(data, url)
            except VariantSelectionRequiredError as exc:
                raise self._variant_selection(data, url) from exc
            selected = await self.http.json("GET", f"{self.api}/{variant_id}", domains=self.domains)
            if selected.get("parent") != data.get("id") or selected.get("id") != variant_id:
                raise ProviderUnavailableError()
            data = selected
        elif any(
            k == "variation_id" or k.startswith("attribute_") for k in parse_qs(urlsplit(url).query)
        ):
            raise VariantSelectionRequiredError()
        result = self._normalize(data)
        if self._slug(result.url) != slug:
            raise ProviderUnavailableError()
        return result

    def _variant_selection(self, data: dict[str, Any], url: str) -> VariantSelectionRequiredError:
        """Offer concrete variants from the already fetched catalog, without guessing prices."""
        parsed = validate_url(url, self.domains)
        names = {
            (a["name"], t["slug"]): plain_text(t["name"])
            for a in data.get("attributes", [])
            if isinstance(a, dict) and isinstance(a.get("name"), str)
            for t in a.get("terms", [])
            if isinstance(t, dict)
            and isinstance(t.get("slug"), str)
            and isinstance(t.get("name"), str)
        }
        options = []
        for choice in (data.get("variations") or [])[:100]:
            try:
                product_id = positive_id(choice["id"])
                attributes = choice["attributes"]
                if not attributes or any(not a.get("value") for a in attributes):
                    continue  # A wildcard does not identify a concrete size/color.
                label = ", ".join(
                    plain_text(a["name"]) + ": " + names.get((a["name"], a["value"]), a["value"])
                    for a in attributes
                )
                selected = urlunsplit(
                    ("https", self.shop.domain, parsed.path, f"variation_id={product_id}", "")
                )
                options.append(VariantOption(label=label[:100], url=selected))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return VariantSelectionRequiredError(options, plain_text(data.get("name", "")))

    async def refresh_offer(self, offer: OfferReference) -> ProductOfferData:
        if offer.store_slug != self.store_slug:
            raise ProviderUnavailableError()
        try:
            product_id = positive_id(offer.external_id)
        except ValueError as exc:
            raise ProviderUnavailableError() from exc
        data = await self.http.json("GET", f"{self.api}/{product_id}", domains=self.domains)
        result = self._normalize(data)
        if result.external_id != offer.external_id or result.metadata != offer.metadata:
            raise ProviderUnavailableError()
        return result

    async def search(
        self,
        query: str,
        *,
        country: str | None = None,
        currency: str | None = None,
    ) -> list[ProductOfferData]:
        # These shops expose one catalog, not destination-specific marketplaces.
        items = await self.http.json_list(
            self.api,
            domains=self.domains,
            params={
                "search": query[:200],
                "type": "simple",
                "per_page": "5",
            },
        )
        if self.shop.search_variations:
            items += await self.http.json_list(
                self.api,
                domains=self.domains,
                params={"search": query[:200], "type": "variation", "per_page": "5"},
            )
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                result = self._normalize(item)
            except (
                ProviderUnavailableError,
                UnsupportedProductError,
                VariantSelectionRequiredError,
            ):
                continue
            if not currency or result.currency == currency:
                results.append(result)
        return results[:5]
