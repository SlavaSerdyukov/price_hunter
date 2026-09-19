from urllib.parse import urlsplit

from pricehunter.core.security import validate_url
from pricehunter.domain.errors import UnsupportedStoreError
from pricehunter.providers.base import StoreProvider


class ProviderRegistry:
    def __init__(self, providers: list[StoreProvider]) -> None:
        self.providers = {provider.name: provider for provider in providers}
        if len(self.providers) != len(providers):
            raise ValueError("Duplicate provider name")

    def resolve_url(self, url: str) -> StoreProvider:
        # Validate syntax even when there are no enabled providers.
        try:
            host = urlsplit(url).hostname or ""
        except ValueError:
            host = ""
        validate_url(url, {host})
        for provider in self.providers.values():
            if provider.supports_url(url):
                validate_url(url, provider.domains)
                return provider
        raise UnsupportedStoreError()

    def get(self, name: str) -> StoreProvider:
        try:
            return self.providers[name]
        except KeyError as exc:
            raise UnsupportedStoreError() from exc
