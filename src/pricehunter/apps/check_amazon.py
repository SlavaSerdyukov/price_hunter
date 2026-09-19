"""Read-only Creators API diagnostic, after Amazon access and tracking approval."""

import argparse
import asyncio
import json

import httpx
from pydantic import ValidationError

from pricehunter.core.config import Settings
from pricehunter.core.network import PublicHTTPTransport
from pricehunter.domain.errors import PriceHunterError, ProviderHTTPError
from pricehunter.providers.amazon import MARKETPLACES, AmazonCreatorsProvider
from pricehunter.providers.base import OfferReference
from pricehunter.providers.http import ProviderHTTP


async def check(
    settings: Settings,
    *,
    country: str,
    query: str,
    url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    if not settings.amazon_enabled or not settings.amazon_price_tracking_approved:
        print("Amazon disabled: obtain Creators API access and tracking approval first.")
        print("See docs/amazon-setup.md. No requests made.")
        return 2
    if country not in settings.amazon_marketplaces:
        print("Configure the requested country in AMAZON_MARKETPLACES.")
        return 2
    try:
        async with httpx.AsyncClient(
            transport=transport if transport is not None else PublicHTTPTransport(),
            trust_env=False,
        ) as client:
            provider = AmazonCreatorsProvider(
                ProviderHTTP(
                    client,
                    timeout=settings.provider_timeout_seconds,
                    max_bytes=settings.max_response_bytes,
                ),
                settings.amazon_creator_client_id.get_secret_value(),
                settings.amazon_creator_client_secret.get_secret_value(),
                settings.amazon_marketplaces,
                credential_version=settings.amazon_credential_version,
                partner_tag=settings.amazon_partner_tag,
            )
            results = await provider.search(query, country=country)
            print(f"Search OK: {len(results)} usable offers in {country}")
            if not results and not url:
                print("Try another query or a full product URL with --url.")
                return 1
            offer = await provider.resolve_url(url or results[0].url)
            refreshed = await provider.refresh_offer(
                OfferReference(offer.url, offer.external_id, offer.store_slug, 0, offer.metadata)
            )
            print(
                json.dumps(
                    {
                        "lookup": "ok",
                        "refresh": "ok",
                        "country": refreshed.country,
                        "asin": refreshed.asin,
                        "price": str(refreshed.price),
                        "currency": refreshed.currency,
                    }
                )
            )
            return 0
    except ProviderHTTPError as exc:
        print(
            f"Creators API HTTP {exc.http_status}. Check credentials, marketplace access and quota."
        )
    except PriceHunterError as exc:
        print(f"Check failed: {exc.code}. No data saved.")
    except ValueError:
        print("Invalid Amazon marketplace configuration. See docs/amazon-setup.md.")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", choices=sorted(MARKETPLACES), default="BE")
    parser.add_argument("--query", default="headphones")
    parser.add_argument("--url")
    args = parser.parse_args()
    try:
        settings = Settings()
    except ValidationError:
        raise SystemExit("Invalid configuration. See docs/amazon-setup.md.") from None
    raise SystemExit(
        asyncio.run(check(settings, country=args.country, query=args.query, url=args.url))
    )


if __name__ == "__main__":
    main()
