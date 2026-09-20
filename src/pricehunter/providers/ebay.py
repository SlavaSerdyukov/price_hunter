import asyncio
import re
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import ValidationError

from pricehunter.core.security import validate_url
from pricehunter.domain.errors import (
    InvalidProductUrlError,
    ProviderHTTPError,
    ProviderUnavailableError,
    VariantOption,
    VariantSelectionRequiredError,
)
from pricehunter.domain.products import Availability, ProductOfferData, validate_trade_identifier
from pricehunter.providers.base import OfferReference, StoreProvider
from pricehunter.providers.http import ProviderHTTP

MARKETPLACES = {
    "US": ("ebay.com", "EBAY_US"),
    "DE": ("ebay.de", "EBAY_DE"),
    "GB": ("ebay.co.uk", "EBAY_GB"),
    "FR": ("ebay.fr", "EBAY_FR"),
    "NL": ("ebay.nl", "EBAY_NL"),
    "BE": ("ebay.com.be", "EBAY_BE"),
    "IT": ("ebay.it", "EBAY_IT"),
    "ES": ("ebay.es", "EBAY_ES"),
    "AT": ("ebay.at", "EBAY_AT"),
    "IE": ("ebay.ie", "EBAY_IE"),
    "PL": ("ebay.pl", "EBAY_PL"),
}

VARIANT_ASPECTS = {
    "size": "size",
    "grösse": "size",
    "taille": "size",
    "maat": "size",
    "taglia": "size",
    "talla": "size",
    "rozmiar": "size",
    "uk schuhgrösse": "size_uk",
    "uk-schuhgrösse": "size_uk",
    "uk shoe size": "size_uk",
    "eu schuhgrösse": "size_eu",
    "eu-schuhgrösse": "size_eu",
    "eu shoe size": "size_eu",
    "us schuhgrösse": "size_us",
    "us-schuhgrösse": "size_us",
    "us shoe size": "size_us",
    "color": "color",
    "colour": "color",
    "farbe": "color",
    "couleur": "color",
    "kleur": "color",
    "colore": "color",
    "kolor": "color",
}


def optional_identifier(value: object, lengths: tuple[int, ...] = (8, 12, 13, 14)) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return validate_trade_identifier(value, lengths)
    except ValueError:
        # Retailers may return localized "does not apply" or incomplete identifiers.
        # Keep the usable listing, but never use those values for product matching.
        return None


class EbayBrowseProvider(StoreProvider):
    name = "ebay"
    api = "https://api.ebay.com"

    def __init__(
        self,
        http: ProviderHTTP,
        client_id: str,
        client_secret: str,
        countries: list[str],
        *,
        belgium_locale: str = "nl-BE",
    ) -> None:
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        if belgium_locale not in ("nl-BE", "fr-BE"):
            raise ValueError("Unsupported Belgian locale")
        self.belgium_locale = belgium_locale
        self.markets = {c: MARKETPLACES[c] for c in countries if c in MARKETPLACES}
        self.domains = {h for domain, _ in self.markets.values() for h in (domain, "www." + domain)}
        self.host_countries = {
            h: country
            for country, (domain, _) in self.markets.items()
            for h in (domain, "www." + domain)
        }
        if "BE" in self.markets:
            for alias in (
                "ebay.be",
                "www.ebay.be",
                "benl.ebay.be",
                "www.benl.ebay.be",
                "befr.ebay.be",
                "www.befr.ebay.be",
            ):
                self.domains.add(alias)
                self.host_countries[alias] = "BE"
        self._token = ""
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    def supports_url(self, url: str) -> bool:
        return urlsplit(url).hostname in self.domains

    async def _headers(self, country: str, locale: str | None = None) -> dict[str, str]:
        async with self._token_lock:
            if time.monotonic() >= self._expires_at:
                response = await self.http.json(
                    "POST",
                    self.api + "/identity/v1/oauth2/token",
                    domains={"api.ebay.com"},
                    auth=httpx.BasicAuth(self.client_id, self.client_secret),
                    data={
                        "grant_type": "client_credentials",
                        "scope": "https://api.ebay.com/oauth/api_scope",
                    },
                )
                try:
                    token = response["access_token"]
                    expires_in = int(response["expires_in"])
                    if not isinstance(token, str) or not token.strip() or expires_in <= 0:
                        raise ValueError("Invalid OAuth response")
                    self._token = token
                    self._expires_at = time.monotonic() + max(0, expires_in - 60)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ProviderUnavailableError() from exc
        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-EBAY-C-MARKETPLACE-ID": self.markets[country][1],
        }
        if country == "BE":
            headers["Accept-Language"] = locale or self.belgium_locale
        return headers

    async def _request(
        self,
        country: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        # An expired/revoked application token gets one refresh, never an endless retry.
        for attempt in range(2):
            headers = await self._headers(country, locale)
            try:
                return await self.http.json(
                    "GET",
                    self.api + path,
                    domains={"api.ebay.com"},
                    headers=headers,
                    params=params,
                )
            except ProviderHTTPError as exc:
                if exc.http_status != 401 or attempt:
                    raise
                async with self._token_lock:
                    if headers["Authorization"] == f"Bearer {self._token}":
                        self._expires_at = 0
        raise ProviderUnavailableError()

    def _locale(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        if host == "befr.ebay.be":
            return "fr-BE"
        if host == "benl.ebay.be":
            return "nl-BE"
        return self.belgium_locale

    def _normalize(self, data: dict[str, Any], country: str) -> ProductOfferData:
        try:
            domain, _ = self.markets[country]
            url = str(data["itemWebUrl"])
            validate_url(url, self.domains)
            price = data["price"]
            if "convertedFromValue" in price or "convertedFromCurrency" in price:
                # Track the seller's native price, not fluctuations in eBay's FX conversion.
                amount, currency = price["convertedFromValue"], price["convertedFromCurrency"]
            else:
                amount, currency = price["value"], price["currency"]
            options = data.get("estimatedAvailabilities", [])
            availability = Availability.UNKNOWN
            if options:
                status = options[0].get("estimatedAvailabilityStatus")
                if status in ("IN_STOCK", "LIMITED_STOCK"):
                    availability = Availability.IN_STOCK
                elif status in ("OUT_OF_STOCK", "SOLD_OUT"):
                    availability = Availability.OUT_OF_STOCK
            # Do not merge used/refurbished listings or variants based only on a title.
            attributes = {
                x["name"].strip().casefold(): x["value"]
                for x in (data.get("localizedAspects") or [])
                if isinstance(x, dict)
                and isinstance(x.get("name"), str)
                and isinstance(x.get("value"), str)
            }
            attribute_names = {
                x["name"].strip().casefold(): x["name"].strip()
                for x in (data.get("localizedAspects") or [])
                if isinstance(x, dict) and isinstance(x.get("name"), str)
            }
            variant = {}
            selected_labels = []
            for key, value in attributes.items():
                if key not in VARIANT_ASPECTS:
                    continue
                dimension = VARIANT_ASPECTS[key]
                # Some sellers return both the selected shoe size and a whole size range.
                if dimension.startswith("size_") and re.fullmatch(
                    r"[\d.,]+\s*[-–]\s*[\d.,]+", value
                ):
                    continue
                if dimension not in variant:
                    variant[dimension] = value
                    selected_labels.append(f"{attribute_names[key]}: {value}")
            if data.get("conditionId"):
                variant["condition"] = str(data["conditionId"])
            title = data["title"]
            parts = str(data["itemId"]).split("|")
            if len(parts) == 3 and parts[0] == "v1" and parts[2] != "0":
                # Preserve the concrete variation even when the seller omits size aspects.
                variant["ebay_variation_id"] = parts[2]
                parsed = validate_url(url, self.domains)
                url = urlunsplit(
                    ("https", parsed.hostname, parsed.path, urlencode({"var": parts[2]}), "")
                )
                if selected_labels:
                    suffix = " — " + ", ".join(selected_labels)[:220]
                    title = title[: 500 - len(suffix)] + suffix
            return ProductOfferData(
                provider=self.name,
                store_slug=f"ebay_{country.lower()}",
                store_name="eBay",
                store_domain=domain,
                country=country,
                external_id=str(data["itemId"]),
                url=url,
                title=title,
                price=amount,
                currency=currency,
                availability=availability,
                image_url=(data.get("image") or {}).get("imageUrl"),
                brand=data.get("brand")
                or next(
                    (
                        attributes[k]
                        for k in ("brand", "marke", "marque", "merk", "marca", "marka")
                        if k in attributes
                    ),
                    None,
                ),
                gtin=optional_identifier(data.get("gtin")),
                ean=optional_identifier(attributes.get("ean"), (8, 13)),
                upc=optional_identifier(attributes.get("upc"), (12,)),
                model=attributes.get("mpn"),
                mpn=attributes.get("mpn"),
                sku=data.get("sku"),
                variant=variant,
                seller=(data.get("seller") or {}).get("username"),
            )
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            ValidationError,
            InvalidProductUrlError,
        ) as exc:
            raise ProviderUnavailableError() from exc

    async def resolve_url(self, url: str) -> ProductOfferData:
        parsed = validate_url(url, self.domains)
        match = re.fullmatch(r"/itm/(?:[^/]+/)?(\d{9,15})/?", parsed.path)
        if not match:
            raise InvalidProductUrlError()
        country = self.host_countries[parsed.hostname or ""]
        params = {"legacy_item_id": match[1]}
        variation = parse_qs(parsed.query).get("var")
        if variation:
            if len(variation) != 1 or not re.fullmatch(r"\d{9,15}", variation[0]):
                raise InvalidProductUrlError()
            params["legacy_variation_id"] = variation[0]
        try:
            data = await self._request(
                country,
                "/buy/browse/v1/item/get_item_by_legacy_id",
                params=params,
                locale=self._locale(url),
            )
        except ProviderHTTPError as exc:
            if exc.http_status != 400 or 11006 not in exc.error_codes or variation:
                raise
            # Build a fixed endpoint from the validated listing ID, never follow an error href.
            raise await self._group_selection(country, match[1], url) from exc
        if variation and data.get("itemId") != f"v1|{match[1]}|{variation[0]}":
            raise ProviderUnavailableError()
        return self._normalize(data, country)

    async def _group_selection(
        self, country: str, listing_id: str, url: str
    ) -> VariantSelectionRequiredError:
        data = await self._request(
            country,
            "/buy/browse/v1/item/get_items_by_item_group",
            params={"item_group_id": listing_id},
            locale=self._locale(url),
        )
        rows = data.get("items")
        if not isinstance(rows, list) or not rows:
            raise ProviderUnavailableError()
        items = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            match = re.fullmatch(
                r"v1\|" + listing_id + r"\|(\d{9,15})", str(item.get("itemId", ""))
            )
            group = item.get("primaryItemGroup")
            if (
                not match
                or not isinstance(group, dict)
                or str(group.get("itemGroupId")) != listing_id
            ):
                continue
            aspects = {
                a["name"]: a["value"]
                for a in (item.get("localizedAspects") or [])
                if isinstance(a, dict)
                and isinstance(a.get("name"), str)
                and isinstance(a.get("value"), str)
            }
            items.append((match[1], item, aspects))
        if not items:
            raise ProviderUnavailableError()
        names = dict.fromkeys(name for _, _, aspects in items for name in aspects)
        varying = [name for name in names if len({a.get(name) for _, _, a in items}) > 1]
        options = []
        parsed = validate_url(url, self.domains)
        for variation_id, item, aspects in items[:100]:
            label = ", ".join(f"{name}: {aspects[name]}" for name in varying if name in aspects)
            if not label:
                label = str(item.get("title") or "eBay")[:75] + f" · {variation_id}"
            selected_url = urlunsplit(
                (
                    "https",
                    parsed.hostname,
                    f"/itm/{listing_id}",
                    urlencode({"var": variation_id}),
                    "",
                )
            )
            options.append(VariantOption(label=label[:100], url=selected_url))
        return VariantSelectionRequiredError(options, str(items[0][1].get("title", "eBay")))

    async def refresh_offer(self, offer: OfferReference) -> ProductOfferData:
        country = offer.store_slug.removeprefix("ebay_").upper()
        if country not in self.markets:
            raise ProviderUnavailableError()
        data = await self._request(
            country,
            "/buy/browse/v1/item/" + quote(offer.external_id, safe=""),
            locale=self._locale(offer.url),
        )
        return self._normalize(data, country)

    async def search(
        self,
        query: str,
        *,
        country: str | None = None,
        currency: str | None = None,
    ) -> list[ProductOfferData]:
        country = country or next(iter(self.markets), "US")
        if country not in self.markets:
            return []
        params = {
            "gtin" if query.isdigit() and len(query) in (8, 12, 13, 14) else "q": query,
            "limit": "10",
            "filter": "buyingOptions:{FIXED_PRICE}",
        }
        data = await self._request(
            country,
            "/buy/browse/v1/item_summary/search",
            params=params,
        )
        items = data.get("itemSummaries", [])
        if not isinstance(items, list):
            raise ProviderUnavailableError()
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                results.append(self._normalize(item, country))
            except ProviderUnavailableError:
                continue  # One incomplete/unsupported listing must not break all results.
        if items and not results:
            raise ProviderUnavailableError()
        return [item for item in results if not currency or item.currency == currency]
