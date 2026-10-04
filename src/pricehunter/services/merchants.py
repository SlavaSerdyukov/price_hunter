from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    Merchant,
    MerchantAudit,
    Product,
    ProductBestState,
    ProductWatch,
    Store,
    StoreOffer,
)
from pricehunter.db.session import SessionFactory
from pricehunter.domain.merchants import MerchantInput


def merchant_view(merchant: Merchant) -> dict[str, Any]:
    return {
        key: getattr(merchant, key)
        for key in ("id", "slug", "display_name", "primary_domain", "active", "version")
    }


class MerchantService:
    """Operator-only explicit identity reconciliation. No contract edits or row merging."""

    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    @staticmethod
    def authorize(reason: str, dry_run: bool, confirm: bool) -> None:
        if not reason.strip() or len(reason) > 500:
            raise ValueError("A reason of 1..500 characters is required")
        if dry_run and confirm:
            raise ValueError("Choose dry-run or confirmation, not both")
        if not dry_run and not confirm:
            raise ValueError("Explicit confirmation required; inspect with dry-run first")

    async def get(self, merchant_id: UUID) -> Merchant:
        async with self.sessions() as session:
            merchant = await session.get(Merchant, merchant_id)
            if merchant is None:
                raise ValueError("Unknown merchant")
            return merchant

    async def list_merchants(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = await session.execute(
                select(Merchant, func.count(Store.id).label("source_count"))
                .outerjoin(Store, Store.merchant_id == Merchant.id)
                .group_by(Merchant.id)
                .order_by(Merchant.slug)
                .limit(min(max(limit, 1), 1000))
            )
            return [{**merchant_view(m), "source_count": count} for m, count in rows]

    async def show(self, merchant_id: UUID) -> dict[str, Any]:
        merchant = await self.get(merchant_id)
        async with self.sessions() as session:
            sources = await session.scalars(
                select(Store).where(Store.merchant_id == merchant_id).order_by(Store.id).limit(1000)
            )
            return {
                **merchant_view(merchant),
                "sources": [
                    {
                        "id": s.id,
                        "slug": s.slug,
                        "network": s.provider_type,
                        "external_merchant_id": s.external_merchant_id,
                        "country": s.country,
                    }
                    for s in sources
                ],
            }

    async def history(self, merchant_id: UUID, limit: int = 100) -> list[dict[str, Any]]:
        await self.get(merchant_id)
        async with self.sessions() as session:
            rows = await session.scalars(
                select(MerchantAudit)
                .where(MerchantAudit.merchant_id == merchant_id)
                .order_by(MerchantAudit.new_version.desc())
                .limit(min(max(limit, 1), 1000))
            )
            return [
                {
                    k: getattr(a, k)
                    for k in (
                        "action",
                        "previous_version",
                        "new_version",
                        "store_id",
                        "related_merchant_id",
                        "changed_fields",
                        "reason",
                        "created_at",
                    )
                }
                for a in rows
            ]

    @staticmethod
    def audit(
        session: AsyncSession,
        merchant: Merchant,
        action: str,
        fields: list[str],
        reason: str,
        *,
        store_id: UUID | None = None,
        related: UUID | None = None,
    ) -> None:
        old = merchant.version
        merchant.version += 1
        merchant.updated_at = utcnow()
        session.add(
            MerchantAudit(
                merchant_id=merchant.id,
                action=action,
                previous_version=old,
                new_version=merchant.version,
                changed_fields=fields,
                reason=reason.strip(),
                store_id=store_id,
                related_merchant_id=related,
            )
        )

    async def create(
        self,
        data: MerchantInput,
        *,
        expected_version: int,
        reason: str,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> Merchant:
        self.authorize(reason, dry_run, confirm)
        if expected_version != 0:
            raise ValueError("version_conflict")
        async with self.sessions.begin() as session:
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended("canonical-merchant:" + data.slug, 0)
                    )
                )
            )
            if await session.scalar(select(Merchant.id).where(Merchant.slug == data.slug)):
                raise ValueError("slug_conflict")
            merchant = Merchant(id=uuid4(), **data.model_dump(), active=True, version=1)
            if not dry_run:
                session.add(merchant)
                await session.flush()
                session.add(
                    MerchantAudit(
                        merchant_id=merchant.id,
                        action="created",
                        previous_version=0,
                        new_version=1,
                        changed_fields=sorted(data.model_dump()),
                        reason=reason.strip(),
                    )
                )
                await session.flush()
            return merchant

    async def change(
        self,
        merchant_id: UUID,
        *,
        expected_version: int,
        reason: str,
        changes: dict[str, Any],
        dry_run: bool = False,
        confirm: bool = False,
    ) -> Merchant:
        self.authorize(reason, dry_run, confirm)
        if not changes or set(changes) - {"display_name", "primary_domain", "active"}:
            raise ValueError("Only merchant metadata and active state can change")
        if "active" in changes and len(changes) > 1:
            raise ValueError("Change active state separately from metadata")
        async with self.sessions.begin() as session:
            merchant = await session.get(Merchant, merchant_id, with_for_update=True)
            if merchant is None:
                raise ValueError("Unknown merchant")
            if merchant.version != expected_version:
                raise ValueError("version_conflict")
            metadata = MerchantInput(
                slug=merchant.slug,
                display_name=changes.get("display_name", merchant.display_name),
                primary_domain=changes.get("primary_domain", merchant.primary_domain),
            )
            if "active" in changes and not isinstance(changes["active"], bool):
                raise ValueError("Active must be boolean")
            values = {k: getattr(metadata, k) if k != "active" else changes[k] for k in changes}
            fields = sorted(k for k, v in values.items() if getattr(merchant, k) != v)
            if not dry_run and fields:
                for key, value in values.items():
                    setattr(merchant, key, value)
                action = (
                    ("enabled" if merchant.active else "disabled")
                    if fields == ["active"]
                    else "metadata_changed"
                )
                self.audit(session, merchant, action, fields, reason)
                await self.due(session, select(Store.id).where(Store.merchant_id == merchant.id))
                await session.flush()
            return merchant

    @staticmethod
    async def due(session: AsyncSession, stores: Select[tuple[UUID]] | list[UUID]) -> None:
        products = select(StoreOffer.product_id).where(StoreOffer.store_id.in_(stores))
        await session.execute(
            update(ProductBestState)
            .where(ProductBestState.product_id.in_(products))
            .values(next_evaluation_at=utcnow())
        )

    async def preview(self, store_id: UUID, merchant_id: UUID) -> dict[str, Any]:
        async with self.sessions() as session:
            store = await session.get(Store, store_id)
            target = await session.get(Merchant, merchant_id)
            if store is None or target is None:
                raise ValueError("Unknown source or merchant")
            raw = await session.scalar(
                select(func.count()).select_from(StoreOffer).where(StoreOffer.store_id == store_id)
            )
            return {
                "source_store_id": store.id,
                "source_slug": store.slug,
                "network": store.provider_type,
                "current_merchant": merchant_view(store.merchant),
                "target_merchant": merchant_view(target),
                "raw_source_offers": raw,
                "source_policies_preserved": True,
                "automatic_linking": False,
            }

    async def reassign(
        self,
        store_id: UUID,
        merchant_id: UUID | None,
        *,
        expected_source_merchant_id: UUID,
        expected_source_version: int,
        expected_version: int,
        reason: str,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> Merchant:
        self.authorize(reason, dry_run, confirm)
        async with self.sessions.begin() as session:
            # Watch/history evaluation serializes on Product. Take the same locks
            # before changing identity so an in-flight evaluation cannot overwrite
            # rebased current pointers with a stale retailer assignment.
            await session.execute(
                select(Product.id)
                .where(
                    Product.id.in_(
                        select(StoreOffer.product_id).where(StoreOffer.store_id == store_id)
                    )
                )
                .order_by(Product.id)
                .with_for_update(of=Product)
            )
            store = await session.scalar(
                select(Store).where(Store.id == store_id).with_for_update(of=Store)
            )
            if store is None:
                raise ValueError("Unknown source")
            if store.merchant_id != expected_source_merchant_id:
                raise ValueError("version_conflict")
            ids = sorted({store.merchant_id, *([merchant_id] if merchant_id else [])})
            merchants = {
                m.id: m
                for m in await session.scalars(
                    select(Merchant)
                    .where(Merchant.id.in_(ids))
                    .order_by(Merchant.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            }
            previous = merchants[store.merchant_id]
            if previous.version != expected_source_version:
                raise ValueError("version_conflict")
            if merchant_id is not None:
                target = merchants.get(merchant_id)
                if target is None:
                    raise ValueError("Unknown merchant")
                if target.version != expected_version:
                    raise ValueError("version_conflict")
                if target.id == previous.id:
                    return target
            else:
                if expected_version != 0:
                    raise ValueError("version_conflict")
                target_id = uuid4()
                target = Merchant(
                    id=target_id,
                    slug="source-" + str(target_id),
                    display_name=store.name,
                    primary_domain=store.domain,
                    active=True,
                    version=1,
                )
                if not dry_run:
                    session.add(target)
                    await session.flush()
                    session.add(
                        MerchantAudit(
                            merchant_id=target.id,
                            action="created",
                            previous_version=0,
                            new_version=1,
                            changed_fields=["slug", "display_name", "primary_domain"],
                            reason=reason.strip(),
                        )
                    )
            if not target.active:
                raise ValueError("Link target must be active")
            if not dry_run:
                self.audit(
                    session,
                    previous,
                    "source_unlinked",
                    ["merchant_id"],
                    reason,
                    store_id=store.id,
                    related=target.id,
                )
                self.audit(
                    session,
                    target,
                    "source_linked",
                    ["merchant_id"],
                    reason,
                    store_id=store.id,
                    related=previous.id,
                )
                store.merchant_id = target.id
                await session.flush()
                # Rebase CURRENT identity pointers; never rewrite historical snapshots.
                offers = select(StoreOffer.id).where(StoreOffer.store_id == store.id)
                await session.execute(
                    update(ProductWatch)
                    .where(ProductWatch.best_offer_id.in_(offers))
                    .values(best_merchant_id=target.id)
                )
                await session.execute(
                    update(ProductBestState)
                    .where(ProductBestState.store_offer_id.in_(offers))
                    .values(merchant_id=target.id, next_evaluation_at=utcnow())
                )
                await self.due(session, [store.id])
            return target
