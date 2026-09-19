"""Check real public shop search, URL lookup and refresh, without modifying application data."""

import argparse
import asyncio
import json

import httpx

from pricehunter.core.config import Settings
from pricehunter.core.network import PublicHTTPTransport
from pricehunter.domain.errors import PriceHunterError
from pricehunter.providers.base import OfferReference
from pricehunter.providers.http import ProviderHTTP
from pricehunter.providers.woocommerce import SHOPS, WooCommerceProvider


async def check(shop: str, query: str, url: str | None) -> int:
    settings = Settings()
    async with httpx.AsyncClient(
        transport=PublicHTTPTransport(), trust_env=False, follow_redirects=False
    ) as client:
        provider = WooCommerceProvider(
            ProviderHTTP(
                client,
                timeout=settings.provider_timeout_seconds,
                max_bytes=settings.max_response_bytes,
            ),
            shop,
        )
        try:
            results = await provider.search(query)
            print(f"{provider.shop.name}: search returned {len(results)} products")
            if not results and url is None:
                print("No results. Try a different --query or supply --url.")
                return 1
            selected = url or results[0].url
            offer = await provider.resolve_url(selected)
            refreshed = await provider.refresh_offer(
                OfferReference(
                    offer.url,
                    offer.external_id,
                    offer.store_slug,
                    0,
                    offer.metadata,
                )
            )
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "store": provider.shop.name,
                        "title": refreshed.title,
                        "price": str(refreshed.price),
                        "currency": refreshed.currency,
                        "availability": refreshed.availability,
                        "url": refreshed.url,
                        "lookup": "ok",
                        "refresh": "ok",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        except PriceHunterError as exc:
            print(f"Check failed: {exc.code} ({type(exc).__name__}). No data was saved.")
            return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shop", choices=SHOPS)
    parser.add_argument("--query", default="pinecil")
    parser.add_argument("--url")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(check(args.shop, args.query, args.url)))


if __name__ == "__main__":
    main()
