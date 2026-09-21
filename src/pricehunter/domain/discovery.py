from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    URL_RESOLVE = "url_resolve"
    SEARCH_KEYWORD = "search_keyword"
    SEARCH_GTIN = "search_gtin"
    SEARCH_MODEL = "search_model"
    SEARCH_ASIN = "search_asin"
    SEARCH_DETAILS = "search_details"
    REFRESH = "refresh"
    VARIANTS = "variants"


@dataclass(frozen=True)
class DiscoveryQuery:
    method: str
    text: str
    country: str
    currency: str


@dataclass(frozen=True)
class ProductSearchIdentity:
    gtin: str | None
    brand: str | None
    model: str | None
    mpn: str | None
    amazon_asin: str | None

    def query(
        self, capabilities: frozenset[Capability], country: str, currency: str
    ) -> DiscoveryQuery | None:
        if self.gtin and Capability.SEARCH_GTIN in capabilities:
            return DiscoveryQuery("gtin", self.gtin, country, currency)
        model_capable = Capability.SEARCH_MODEL in capabilities
        if self.brand and self.mpn and model_capable:
            return DiscoveryQuery("mpn", f"{self.brand} {self.mpn}", country, currency)
        if self.brand and self.model and model_capable:
            return DiscoveryQuery("model", f"{self.brand} {self.model}", country, currency)
        if self.amazon_asin and Capability.SEARCH_ASIN in capabilities:
            return DiscoveryQuery("asin", self.amazon_asin, country, currency)
        return None


class DiscoveryMismatch(Exception):
    """Resolver cannot safely attach this result to the requested canonical product."""
