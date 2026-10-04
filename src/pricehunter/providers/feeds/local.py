from uuid import UUID

from sqlalchemy import case, exists, func, or_, select

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import Merchant, MerchantFeedItem, MerchantProgram, Store
from pricehunter.db.session import SessionFactory
from pricehunter.domain.discovery import Capability, DiscoveryQuery
from pricehunter.domain.errors import UnsupportedStoreError
from pricehunter.domain.feeds import FeedProductData
from pricehunter.domain.products import ProductOfferData, model_code, validate_trade_identifier
from pricehunter.providers.base import StoreProvider
from pricehunter.services.policy_resolver import PolicyResolver


def materialization_data(item: MerchantFeedItem, program: MerchantProgram) -> ProductOfferData:
    return FeedProductData.model_validate(item.data).offer(
        program_id=program.id,
        network=program.network,
        merchant_id=program.external_merchant_id,
        name=program.display_name,
        domain=program.domain,
        market=program.market_country,
        generation=item.feed_generation,
    )


class FeedStoreProvider(StoreProvider):
    domains: set[str] = set()
    manages_request_limits = True
    capabilities = frozenset(
        {
            Capability.SEARCH_KEYWORD,
            Capability.SEARCH_GTIN,
            Capability.SEARCH_MODEL,
            Capability.SNAPSHOT_REFRESH,
            Capability.VARIANTS,
        }
    )

    def __init__(self, network: str, sessions: SessionFactory, settings: Settings) -> None:
        self.name, self.sessions, self.settings = network, sessions, settings
        self.discovery_interval_seconds = settings.feed_sync_seconds

    def supports_url(self, url: str) -> bool:
        return False

    async def resolve_url(self, url: str) -> ProductOfferData:
        raise UnsupportedStoreError()

    async def search(
        self,
        query: str,
        *,
        country: str | None = None,
        currency: str | None = None,
        program_id: UUID | None = None,
    ) -> list[ProductOfferData]:
        return await self._search(
            query, country, currency, "catalog_persistence_allowed", program_id=program_id
        )

    async def discover(self, query: DiscoveryQuery) -> list[ProductOfferData]:
        return await self._search(
            query.text, query.country, query.currency, "tracking_allowed", query.method
        )

    async def _search(
        self,
        query: str,
        country: str | None,
        currency: str | None,
        permission: str,
        method: str = "keyword",
        program_id: UUID | None = None,
    ) -> list[ProductOfferData]:
        if not country or not query.strip() or len(query) > 200:
            return []
        item, program = MerchantFeedItem, MerchantProgram
        text_query = func.plainto_tsquery("simple", query)
        text_match = item.search_document.op("@@")(text_query)
        identifiers = [func.lower(item.data["mpn"].astext) == query.strip().casefold()]
        try:
            identifiers.append(item.gtin == validate_trade_identifier(query.strip()).zfill(14))
        except ValueError:
            pass
        # Exact brand/model key is separator-normalized, never title-derived identity.
        for split in range(1, len(query.split())):
            words = query.split()
            key = model_code(" ".join(words[:split])) + ":" + model_code(" ".join(words[split:]))
            identifiers.extend([item.brand_model == key, item.brand_mpn == key])
        match = (
            or_(False, *identifiers)
            if method in {"gtin", "model", "mpn"}
            else or_(*identifiers, text_match)
        )
        clauses = [
            program.network == self.name,
            program.market_country == country,
            PolicyResolver(self.settings).program_filter(permission),
            exists().where(
                Store.id == program.store_id,
                Store.active.is_(True),
                Store.supported.is_(True),
                Merchant.id == Store.merchant_id,
                Merchant.active.is_(True),
            ),
            item.active.is_(True),
            item.seen_at
            > utcnow()
            - func.make_interval(
                0, 0, 0, 0, 0, 0, program.policy_data["max_cache_seconds"].as_integer()
            ),
            match,
        ]
        if currency:
            clauses.append(program.currency == currency)
        if program_id:
            clauses.append(program.id == program_id)
        async with self.sessions() as session:
            rows = await session.execute(
                select(item, program)
                .join(program)
                .where(*clauses)
                .order_by(
                    case((or_(False, *identifiers), 0), else_=1),
                    func.ts_rank(item.search_document, text_query).desc(),
                    program.id,
                    item.external_id,
                )
                .limit(self.settings.discovery_result_limit)
            )
            return [materialization_data(row, merchant) for row, merchant in rows]
