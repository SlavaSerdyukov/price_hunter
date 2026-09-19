"""Read-only eBay production diagnostic. No database writes or Telegram messages."""

import argparse
import asyncio
import json

import httpx
from pydantic import ValidationError

from pricehunter.core.config import Settings
from pricehunter.core.network import PublicHTTPTransport
from pricehunter.domain.errors import (
    InvalidProductUrlError,
    ProductNotFoundError,
    ProviderHTTPError,
    ProviderUnavailableError,
    VariantSelectionRequiredError,
)
from pricehunter.providers.base import OfferReference
from pricehunter.providers.ebay import MARKETPLACES, EbayBrowseProvider
from pricehunter.providers.http import ProviderHTTP


async def check(
    settings: Settings,
    *,
    country: str,
    query: str,
    url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    missing = [
        name
        for name, value in (
            ("EBAY_CLIENT_ID", settings.ebay_client_id),
            ("EBAY_CLIENT_SECRET", settings.ebay_client_secret),
        )
        if not value.get_secret_value().strip()
    ]
    if missing:
        print("Missing production credentials: " + ", ".join(missing))
        print("See docs/ebay-setup.md. Secrets are read from .env, never command arguments.")
        return 2
    if country not in settings.ebay_marketplaces or country not in MARKETPLACES:
        print("The requested country must be supported and listed in EBAY_MARKETPLACES.")
        return 2
    if not settings.ebay_enabled:
        print("EBAY_ENABLED=false: checking access only; the bot integration stays disabled.")
    stage = "OAuth"
    try:
        async with httpx.AsyncClient(
            transport=transport if transport is not None else PublicHTTPTransport(),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            provider = EbayBrowseProvider(
                ProviderHTTP(
                    client,
                    timeout=settings.provider_timeout_seconds,
                    max_bytes=settings.max_response_bytes,
                ),
                settings.ebay_client_id.get_secret_value(),
                settings.ebay_client_secret.get_secret_value(),
                settings.ebay_marketplaces,
                belgium_locale=settings.ebay_belgium_locale,
            )
            await provider._headers(country)
            stage = "search"
            offers = await provider.search(query, country=country)
            print(f"Browse search OK: country={country}, usable_results={len(offers)}")
            if url is None and not offers:
                print("Access works, but no products matched. Retry with --query or --url.")
                return 1
            stage = "lookup"
            offer = await provider.resolve_url(url if url is not None else offers[0].url)
            stage = "refresh"
            refreshed = await provider.refresh_offer(
                OfferReference(offer.url, offer.external_id, offer.store_slug, 0, offer.metadata)
            )
            if (refreshed.external_id, refreshed.store_slug) != (
                offer.external_id,
                offer.store_slug,
            ):
                print("Refresh returned a different listing; access check failed.")
                return 1
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "lookup": "ok",
                        "refresh": "ok",
                        "country": refreshed.country,
                        "price": str(refreshed.price),
                        "currency": refreshed.currency,
                        "availability": refreshed.availability,
                        "url": refreshed.url,
                    }
                )
            )
            return 0
    except ProviderHTTPError as exc:
        advice = {
            400: "Check production App ID / Cert ID and supported marketplace parameters.",
            401: "Check App ID / Cert ID from the same active Production keyset.",
            403: "Check whether this production keyset has Browse API access approval.",
            429: "eBay quota reached. Wait before retrying and review approved API limits.",
        }.get(exc.http_status, "eBay returned an error. Retry later or check eBay API status.")
        print(f"{stage} failed: HTTP {exc.http_status}. {advice}")
    except InvalidProductUrlError:
        print("Use a full HTTPS eBay /itm/ product URL from an enabled marketplace.")
    except VariantSelectionRequiredError as exc:
        print("This listing has variants. Select one in the bot or pass its ?var= URL with --url.")
        for option in exc.options[:3]:
            print(f"{option.label}: {option.url}")
    except ProductNotFoundError:
        print(f"{stage} failed: listing not found. Try another active fixed-price listing.")
    except ProviderUnavailableError:
        print(f"{stage} failed: network failure or unusable eBay response; no secrets printed.")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", choices=sorted(MARKETPLACES), default="BE")
    parser.add_argument("--query", default="headphones")
    parser.add_argument("--url", help="Optional full eBay product URL to resolve and refresh")
    args = parser.parse_args()
    try:
        settings = Settings()
    except ValidationError:
        raise SystemExit("Invalid configuration. Check .env against .env.example.") from None
    raise SystemExit(
        asyncio.run(check(settings, country=args.country, query=args.query, url=args.url))
    )


if __name__ == "__main__":
    main()
