from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import FeedSyncState, MerchantProgram, Store
from pricehunter.db.session import SessionFactory
from pricehunter.domain.feeds import MerchantProgramInput, merchant_slug


class MerchantProgramService:
    def __init__(self, sessions: SessionFactory, settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings

    async def save(self, data: MerchantProgramInput) -> MerchantProgram:
        async with self.sessions.begin() as session:
            slug = merchant_slug(data.network, data.external_merchant_id)
            await session.execute(
                insert(Store)
                .values(
                    id=uuid4(),
                    slug=slug,
                    name=data.display_name,
                    domain=data.domain,
                    provider_type=data.network,
                    external_merchant_id=data.external_merchant_id,
                    country=data.market_country,
                )
                .on_conflict_do_nothing(index_elements=[Store.slug])
            )
            store = (await session.scalars(select(Store).where(Store.slug == slug))).one()
            values = data.model_dump(exclude={"policy"})
            values.update(
                store_id=store.id,
                policy_data=data.policy.model_dump(),
                review_reference=data.policy.review_reference,
                last_reviewed_at=utcnow() if data.policy.reviewed else None,
            )
            stmt = insert(MerchantProgram).values(id=uuid4(), **values)
            program_id = await session.scalar(
                stmt.on_conflict_do_update(
                    index_elements=[
                        MerchantProgram.network,
                        MerchantProgram.external_merchant_id,
                        MerchantProgram.market_country,
                    ],
                    set_=values,
                ).returning(MerchantProgram.id)
            )
            await session.execute(
                insert(FeedSyncState)
                .values(merchant_program_id=program_id)
                .on_conflict_do_nothing()
            )
            program = await session.get(MerchantProgram, program_id)
            assert program is not None
            if not program.active:
                structlog.get_logger().info(
                    "merchant_program_disabled",
                    network=data.network,
                    program_id=str(program.id),
                    market=data.market_country,
                )
            return program

    async def get(self, program_id: UUID) -> MerchantProgram:
        async with self.sessions() as session:
            program = await session.get(MerchantProgram, program_id)
            if program is None:
                raise ValueError("Unknown merchant program")
            return program
