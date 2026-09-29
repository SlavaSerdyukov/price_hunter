import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import delete

from pricehunter.core.config import Settings
from pricehunter.core.security import validate_url
from pricehunter.db.base import utcnow
from pricehunter.db.models import OutboundClick, Store, StoreOffer
from pricehunter.db.session import SessionFactory
from pricehunter.domain.errors import PriceHunterError, ProductNotFoundError, ProviderPolicyError
from pricehunter.domain.markets import validate_country
from pricehunter.providers.ebay import MARKETPLACES
from pricehunter.schemas.api import OfferView

EBAY_LINK_DOMAINS = {
    host
    for domain in (
        {domain for domain, _ in MARKETPLACES.values()}
        | {"ebay.be", "benl.ebay.be", "befr.ebay.be"}
    )
    for host in (domain, "www." + domain)
} | {"rover.ebay.com"}

SURFACES = ("api", "comparison", "telegram", "notification", "history")


class OutboundLinkService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def destination(self, offer: StoreOffer, store: Store) -> tuple[str, str | None]:
        if not store.active or not store.supported:
            raise ProductNotFoundError()
        policy = self.settings.data_policy(store.provider_type)
        policy.require("catalog_persistence_allowed")
        if utcnow() - offer.last_checked_at > timedelta(seconds=policy.max_cache_seconds):
            raise ProductNotFoundError()
        network = offer.affiliate_network
        active = policy.affiliate_allowed and (
            (
                store.provider_type == "ebay"
                and network == "ebay_epn"
                and bool(self.settings.ebay_epn_campaign_id)
            )
            or (
                store.provider_type == "rakuten"
                and network == "rakuten"
                and self.settings.rakuten_enabled
            )
        )
        if offer.affiliate_url and active:
            domains = (
                {"click.linksynergy.com", "linksynergy.com"}
                if network == "rakuten"
                else EBAY_LINK_DOMAINS
            )
            validate_url(offer.affiliate_url, domains)
            return offer.affiliate_url, network
        if policy.affiliate_required or not offer.direct_url:
            raise ProviderPolicyError()
        validate_url(
            offer.direct_url,
            EBAY_LINK_DOMAINS
            if store.provider_type == "ebay"
            else {store.domain, "www." + store.domain.removeprefix("www.")},
        )
        return offer.direct_url, None

    def offer_view(self, offer: StoreOffer, store: Store, *, surface: str = "api") -> OfferView:
        result = OfferView.model_validate(offer)
        result.store = store.name
        result.attribution = self.settings.data_policy(
            store.provider_type
        ).display_attribution_required
        try:
            result.url = self.link(
                offer, store, surface=surface, market_country=offer.market_country
            )
        except PriceHunterError:
            result.url = None
        return result

    def link(
        self,
        offer: StoreOffer,
        store: Store,
        *,
        surface: str = "comparison",
        market_country: str | None = None,
    ) -> str:
        destination, _ = self.destination(offer, store)
        if not self.settings.public_base_url:
            return destination
        return (
            self.settings.public_base_url.rstrip("/")
            + "/r/"
            + self.sign(offer.id, surface=surface, market_country=market_country)
        )

    def sign(
        self,
        offer_id: UUID,
        *,
        surface: str = "comparison",
        market_country: str | None = None,
        now: datetime | None = None,
    ) -> str:
        if (
            not self.settings.public_base_url
            or not self.settings.redirect_signing_secret.get_secret_value()
        ):
            raise ProviderPolicyError()
        if surface not in SURFACES:
            raise ValueError("Unknown click surface")
        if market_country:
            validate_country(market_country)
        expiry = int((now or utcnow()).timestamp()) + self.settings.redirect_ttl_seconds
        payload = (
            offer_id.bytes
            + expiry.to_bytes(8, "big")
            + bytes([SURFACES.index(surface)])
            + (market_country or "--").encode()
            + secrets.token_bytes(16)
        )
        signature = hmac.digest(
            self.settings.redirect_signing_secret.get_secret_value().encode(),
            payload,
            hashlib.sha256,
        )
        return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")

    def verify(
        self, token: str, *, now: datetime | None = None
    ) -> tuple[UUID, str, str | None, str]:
        try:
            if (
                len(token) != 100
                or not self.settings.public_base_url
                or not self.settings.redirect_signing_secret.get_secret_value()
            ):
                raise ValueError
            raw = base64.b64decode(token, altchars=b"-_", validate=True)
            payload, signature = raw[:43], raw[43:]
            expected = hmac.digest(
                self.settings.redirect_signing_secret.get_secret_value().encode(),
                payload,
                hashlib.sha256,
            )
            if len(raw) != 75 or not hmac.compare_digest(signature, expected):
                raise ValueError
            if int.from_bytes(payload[16:24], "big") <= int((now or utcnow()).timestamp()):
                raise ValueError
            market = payload[25:27].decode()
            return (
                UUID(bytes=payload[:16]),
                SURFACES[payload[24]],
                None if market == "--" else validate_country(market),
                payload[27:].hex(),
            )
        except (ValueError, IndexError, UnicodeError) as exc:
            raise ProductNotFoundError() from exc

    async def redirect(self, sessions: SessionFactory, token: str) -> str:
        offer_id, surface, market, reference = self.verify(token)
        async with sessions.begin() as session:
            offer = await session.get(StoreOffer, offer_id)
            store = await session.get(Store, offer.store_id) if offer else None
            if offer is None or store is None:
                raise ProductNotFoundError()
            if market and market != offer.market_country:
                raise ProductNotFoundError()
            destination, network = self.destination(offer, store)
            session.add(
                OutboundClick(
                    offer_id=offer.id,
                    store_id=store.id,
                    affiliate_network=network,
                    surface=surface,
                    market_country=market,
                    opaque_click_reference=reference,
                )
            )
            return destination

    async def retain(self, sessions: SessionFactory) -> None:
        async with sessions.begin() as session:
            await session.execute(
                delete(OutboundClick).where(
                    OutboundClick.created_at
                    < utcnow() - timedelta(days=self.settings.outbound_click_retention_days)
                )
            )


def validate_redirect_settings(settings: Settings) -> None:
    secret = settings.redirect_signing_secret.get_secret_value()
    if bool(settings.public_base_url) != bool(secret):
        raise ValueError("Redirect requires PUBLIC_BASE_URL and REDIRECT_SIGNING_SECRET together")
    if settings.public_base_url:
        parsed = validate_url(
            settings.public_base_url, {urlsplit(settings.public_base_url).hostname or ""}
        )
        if parsed.query or parsed.fragment or len(secret) < 32:
            raise ValueError(
                "Redirect requires a clean HTTPS base URL and a secret of at least 32 characters"
            )
