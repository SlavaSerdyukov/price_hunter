import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    FeedSyncState,
    MerchantFeedItem,
    MerchantProgram,
    MerchantProgramAudit,
    Store,
    StoreOffer,
)
from pricehunter.db.session import SessionFactory
from pricehunter.domain.feeds import MerchantProgramInput, merchant_slug
from pricehunter.domain.provider_policy import ProviderDataPolicy


class MerchantProgramService:
    def __init__(self, sessions: SessionFactory, settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings

    async def save(self, data: MerchantProgramInput) -> MerchantProgram:
        """Create pending only. Existing identities are immutable through generic import."""
        async with self.sessions.begin() as session:
            identity = (
                MerchantProgram.network == data.network,
                MerchantProgram.external_merchant_id == data.external_merchant_id,
                MerchantProgram.market_country == data.market_country,
            )
            existing = await session.scalar(select(MerchantProgram).where(*identity))
            if existing is not None:
                return existing
            if data.active or data.approved or data.policy.reviewed:
                raise ValueError("Import pending programs; use explicit review and activation")
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
                policy_data=ProviderDataPolicy().model_dump(),
                review_reference="",
                last_reviewed_at=None,
                version=1,
            )
            program_id = await session.scalar(
                insert(MerchantProgram)
                .values(id=uuid4(), **values)
                .on_conflict_do_nothing(
                    index_elements=[
                        MerchantProgram.network,
                        MerchantProgram.external_merchant_id,
                        MerchantProgram.market_country,
                    ]
                )
                .returning(MerchantProgram.id)
            )
            if program_id is None:
                return (await session.scalars(select(MerchantProgram).where(*identity))).one()
            await session.execute(insert(FeedSyncState).values(merchant_program_id=program_id))
            program = await session.get(MerchantProgram, program_id)
            assert program is not None
            session.add(
                MerchantProgramAudit(
                    merchant_program_id=program.id,
                    action="created",
                    previous_version=0,
                    new_version=1,
                    changed_fields=sorted(values),
                    reason="Pending program imported",
                )
            )
            return program

    async def _locked(
        self, session: AsyncSession, program_id: UUID, expected_version: int
    ) -> MerchantProgram:
        # Same sync-state -> program lock order as feed completion/retention.
        await session.get(FeedSyncState, program_id, with_for_update=True)
        program = await session.get(MerchantProgram, program_id, with_for_update=True)
        if program is None:
            raise ValueError("Unknown merchant program")
        if program.version != expected_version:
            raise ValueError("version_conflict")
        return program

    async def _invalidate(self, session: AsyncSession, program: MerchantProgram) -> None:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.merchant_program_id == program.id)
            .values(catalog_active=False)
        )
        await session.execute(
            update(MerchantFeedItem)
            .where(MerchantFeedItem.merchant_program_id == program.id)
            .values(active=False)
        )
        state = await session.get(FeedSyncState, program.id)
        assert state is not None
        state.lease_token = state.lease_until = None
        state.status, state.next_sync_at = "pending", utcnow()

    async def _change(
        self,
        program_id: UUID,
        *,
        expected_version: int,
        action: str,
        changes: dict[str, Any],
        reason: str,
        dry_run: bool = False,
        invalidate: bool = False,
    ) -> MerchantProgram:
        if not reason.strip() or len(reason) > 500:
            raise ValueError("A reason of 1..500 characters is required")
        async with self.sessions.begin() as session:
            program = await self._locked(session, program_id, expected_version)
            if action == "activated":
                policy = ProviderDataPolicy.model_validate(program.policy_data)
                if not program.approved or not policy.reviewed or not program.last_reviewed_at:
                    raise ValueError("Program needs explicit review and approval")
                policy.require("catalog_persistence_allowed")
            if dry_run:
                return program
            changed = sorted(k for k, v in changes.items() if getattr(program, k) != v)
            if not changed:
                return program
            old = program.version
            for key, value in changes.items():
                setattr(program, key, value)
            if action == "metadata_changed":
                # Store is the network/advertiser identity shared by market programs.
                await session.execute(
                    update(Store)
                    .where(Store.id == program.store_id)
                    .values(name=program.display_name)
                )
            program.version += 1
            session.add(
                MerchantProgramAudit(
                    merchant_program_id=program.id,
                    action=action,
                    previous_version=old,
                    new_version=program.version,
                    changed_fields=changed,
                    reason=reason.strip(),
                )
            )
            if invalidate:
                await self._invalidate(session, program)
            elif action == "activated":
                state = await session.get(FeedSyncState, program.id)
                assert state is not None
                state.next_sync_at = utcnow()
            await session.flush()
            return program

    async def review(
        self,
        program_id: UUID,
        policy: ProviderDataPolicy,
        *,
        expected_version: int,
        reason: str,
        dry_run: bool = False,
    ) -> MerchantProgram:
        if not policy.reviewed or not policy.review_reference.strip():
            raise ValueError("Review needs an explicit policy reference")
        return await self._change(
            program_id,
            expected_version=expected_version,
            action="reviewed",
            changes={
                "policy_data": policy.model_dump(),
                "review_reference": policy.review_reference,
                "last_reviewed_at": utcnow(),
                "approved": True,
            },
            reason=reason,
            dry_run=dry_run,
        )

    async def change_policy(
        self,
        program_id: UUID,
        policy: ProviderDataPolicy,
        *,
        expected_version: int,
        reason: str,
        dry_run: bool = False,
    ) -> MerchantProgram:
        if not policy.reviewed or not policy.review_reference.strip():
            raise ValueError("Policy change needs an explicit review")
        return await self._change(
            program_id,
            expected_version=expected_version,
            action="policy_changed",
            changes={
                "policy_data": policy.model_dump(),
                "review_reference": policy.review_reference,
                "last_reviewed_at": utcnow(),
            },
            reason=reason,
            dry_run=dry_run,
        )

    async def activate(
        self, program_id: UUID, *, expected_version: int, reason: str, dry_run: bool = False
    ) -> MerchantProgram:
        return await self._change(
            program_id,
            expected_version=expected_version,
            action="activated",
            changes={"active": True},
            reason=reason,
            dry_run=dry_run,
        )

    async def disable(
        self, program_id: UUID, *, expected_version: int, reason: str, dry_run: bool = False
    ) -> MerchantProgram:
        return await self._change(
            program_id,
            expected_version=expected_version,
            action="disabled",
            changes={"active": False},
            reason=reason,
            dry_run=dry_run,
            invalidate=True,
        )

    async def update_metadata(
        self,
        program_id: UUID,
        *,
        expected_version: int,
        reason: str,
        display_name: str,
        dry_run: bool = False,
    ) -> MerchantProgram:
        if not display_name.strip() or len(display_name) > 100:
            raise ValueError("Invalid display name")
        return await self._change(
            program_id,
            expected_version=expected_version,
            action="metadata_changed",
            changes={"display_name": display_name},
            reason=reason,
            dry_run=dry_run,
        )

    async def change_feed_reference(
        self,
        program_id: UUID,
        external_feed_id: str,
        *,
        expected_version: int,
        reason: str,
        dry_run: bool = False,
    ) -> MerchantProgram:
        if not re.fullmatch(r"[0-9]{1,30}", external_feed_id):
            raise ValueError("Invalid feed reference")
        return await self._change(
            program_id,
            expected_version=expected_version,
            action="feed_reference_changed",
            changes={
                "external_feed_id": external_feed_id,
                "active": False,
                "approved": False,
                "policy_data": ProviderDataPolicy().model_dump(),
                "review_reference": "",
                "last_reviewed_at": None,
            },
            reason=reason,
            dry_run=dry_run,
            invalidate=True,
        )

    async def get(self, program_id: UUID) -> MerchantProgram:
        async with self.sessions() as session:
            program = await session.get(MerchantProgram, program_id)
            if program is None:
                raise ValueError("Unknown merchant program")
            return program

    async def history(self, program_id: UUID, *, limit: int = 100) -> list[MerchantProgramAudit]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(MerchantProgramAudit)
                    .where(MerchantProgramAudit.merchant_program_id == program_id)
                    .order_by(MerchantProgramAudit.new_version.desc())
                    .limit(min(max(limit, 1), 200))
                )
            )
