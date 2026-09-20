import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.db.base import utcnow
from pricehunter.db.models import NotificationEvent, PriceObservation, Store, StoreOffer, Tracker
from pricehunter.db.session import SessionFactory
from pricehunter.domain.pricing import TrackerRules, anomaly_reason, evaluate_rules
from pricehunter.domain.products import ProductOfferData
from pricehunter.domain.subscriptions import Plan
from pricehunter.providers.base import OfferReference
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.services.entitlement_service import EntitlementService

log = structlog.get_logger()


@dataclass(frozen=True)
class RefreshClaim:
    offer_id: UUID
    token: UUID


class PriceCheckService:
    def __init__(
        self,
        sessions: SessionFactory,
        registry: ProviderRegistry,
        limiter: RateLimiter,
        settings: Settings,
        entitlements: EntitlementService,
    ) -> None:
        self.sessions, self.registry = sessions, registry
        self.limiter, self.settings, self.entitlements = limiter, settings, entitlements

    async def claim_due(self) -> list[RefreshClaim]:
        now = utcnow()
        async with self.sessions.begin() as session:
            eligible = self.entitlements.scheduled_tracker_ids(now)
            plan = self.entitlements.plan_expression(now)
            interval = (
                select(
                    func.min(
                        case(
                            *[
                                (
                                    plan == p,
                                    self.entitlements.policy.for_plan(p).check_interval_seconds,
                                )
                                for p in Plan
                            ],
                            else_=self.settings.free_check_seconds,
                        )
                    )
                )
                .where(Tracker.store_offer_id == StoreOffer.id, Tracker.id.in_(eligible))
                .correlate(StoreOffer)
                .scalar_subquery()
            )
            actual_interval = case(
                (Store.provider_type == "mock", self.settings.mock_check_interval_seconds),
                else_=interval,
            )
            offers = list(
                await session.scalars(
                    select(StoreOffer)
                    .join(Store)
                    .where(
                        StoreOffer.next_check_at <= now,
                        Store.active.is_(True),
                        or_(
                            StoreOffer.refresh_sequence == 0,
                            StoreOffer.failure_count > 0,
                            StoreOffer.suspicious_price.is_not(None),
                            StoreOffer.last_checked_at
                            <= now - func.make_interval(0, 0, 0, 0, 0, 0, actual_interval),
                        ),
                        or_(StoreOffer.lease_until.is_(None), StoreOffer.lease_until < now),
                        exists().where(
                            Tracker.store_offer_id == StoreOffer.id, Tracker.id.in_(eligible)
                        ),
                    )
                    .order_by(StoreOffer.next_check_at)
                    .limit(self.settings.batch_size)
                    .with_for_update(of=StoreOffer, skip_locked=True)
                )
            )
            claims = []
            for offer in offers:
                offer.lease_token = uuid4()
                offer.lease_until = now + timedelta(seconds=self.settings.refresh_lease_seconds)
                claims.append(RefreshClaim(offer.id, offer.lease_token))
            return claims

    async def refresh(self, claim: RefreshClaim) -> bool:
        async with self.limiter.offer_refresh(claim.offer_id) as acquired:
            if not acquired:
                return False
            return await self._refresh(claim)

    async def _refresh(self, claim: RefreshClaim) -> bool:
        # This read session closes before the provider performs any network I/O.
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(StoreOffer, Store)
                    .join(Store)
                    .where(
                        StoreOffer.id == claim.offer_id,
                        StoreOffer.lease_token == claim.token,
                        StoreOffer.lease_until > utcnow(),
                    )
                )
            ).one_or_none()
            if row is None:
                return False
            offer, store = row
            reference = OfferReference(
                url=offer.url,
                external_id=offer.external_id,
                store_slug=store.slug,
                refresh_sequence=offer.refresh_sequence,
                metadata=offer.metadata_json,
            )
            provider_name = store.provider_type
        try:
            provider = self.registry.get(provider_name)
            async with self.limiter.provider(provider_name):
                data = await provider.refresh_offer(reference)
            return await self.accept(claim, data)
        except Exception as exc:
            # Persist a bounded retry schedule. One failed store does not kill a batch.
            log.warning(
                "offer_refresh_failed",
                store_offer_id=str(claim.offer_id),
                provider=provider_name,
                error_type=type(exc).__name__,
            )
            await self.failed(claim)
            return False

    async def failed(self, claim: RefreshClaim) -> None:
        async with self.sessions.begin() as session:
            offer = await session.scalar(
                select(StoreOffer)
                .where(
                    StoreOffer.id == claim.offer_id,
                    StoreOffer.lease_token == claim.token,
                )
                .with_for_update()
            )
            if offer:
                offer.failure_count += 1
                delay = min(3600, 30 * 2 ** min(offer.failure_count, 7)) + random.randint(0, 30)
                offer.next_check_at = utcnow() + timedelta(seconds=delay)
                offer.lease_token, offer.lease_until = None, None

    async def accept(self, claim: RefreshClaim, data: ProductOfferData) -> bool:
        now = utcnow()
        async with self.sessions.begin() as session:
            offer = await session.scalar(
                select(StoreOffer)
                .where(
                    StoreOffer.id == claim.offer_id,
                    StoreOffer.lease_token == claim.token,
                    StoreOffer.lease_until > now,
                )
                .with_for_update()
            )
            if offer is None:
                return False  # A stale worker can never overwrite a new owner's result.
            store = await session.get(Store, offer.store_id)
            assert store is not None
            if (data.external_id, data.store_slug, data.provider) != (
                offer.external_id,
                store.slug,
                store.provider_type,
            ):
                raise ValueError("Provider changed listing identity")
            trackers = list(
                await session.scalars(
                    select(Tracker)
                    .where(
                        Tracker.store_offer_id == offer.id,
                        Tracker.id.in_(self.entitlements.scheduled_tracker_ids(now)),
                    )
                    .with_for_update()
                )
            )
            rights = {
                t.user_id: (
                    await self.entitlements.for_user(t.user_id, session=session, now=now)
                ).entitlements
                for t in trackers
            }
            interval = min(
                (right.check_interval_seconds for right in rights.values()),
                default=self.settings.free_check_seconds,
            )
            if store.provider_type == "mock":
                interval = self.settings.mock_check_interval_seconds
            for tracker in trackers:
                tracker.check_interval_seconds = (
                    interval
                    if store.provider_type == "mock"
                    else rights[tracker.user_id].check_interval_seconds
                )
            offer.lease_token, offer.lease_until = None, None
            offer.next_check_at = now + timedelta(seconds=interval)
            reason = anomaly_reason(offer.price, data.price, offer.currency, data.currency)
            confirmed = (
                reason == "extreme_drop"
                and offer.suspicious_price == data.price
                and offer.suspicious_currency == data.currency
            )
            if reason and not confirmed:
                offer.suspicious_price, offer.suspicious_currency = data.price, data.currency
                offer.next_check_at = now + timedelta(seconds=min(interval, 300))
                log.warning("observation_quarantined", store_offer_id=str(offer.id), reason=reason)
                return False
            previous, previous_availability = offer.price, offer.availability
            minimum = offer.minimum_price
            observation_id = uuid4()
            await session.execute(
                insert(PriceObservation)
                .values(
                    id=observation_id,
                    store_offer_id=offer.id,
                    refresh_key=str(claim.token),
                    price=data.price,
                    currency=data.currency,
                    availability=data.availability,
                    checked_at=now,
                )
                .on_conflict_do_nothing(index_elements=[PriceObservation.refresh_key])
            )
            offer.price, offer.availability = data.price, data.availability
            offer.title, offer.image_url = data.title, data.image_url
            offer.original_price = data.original_price
            offer.last_checked_at = now
            offer.minimum_price = min(minimum, data.price)
            offer.maximum_price = max(offer.maximum_price, data.price)
            offer.total_price += data.price
            offer.observation_count += 1
            offer.refresh_sequence += 1
            offer.failure_count = 0
            offer.suspicious_price, offer.suspicious_currency = None, None
            cooldown = self.settings.notification_cooldown_seconds
            recent_priority = (
                set(
                    await session.execute(
                        select(
                            NotificationEvent.tracker_id,
                            NotificationEvent.event_type,
                        ).where(
                            NotificationEvent.tracker_id.in_([tracker.id for tracker in trackers]),
                            NotificationEvent.event_type.in_(["target_reached", "back_in_stock"]),
                            NotificationEvent.created_at > now - timedelta(seconds=cooldown),
                        )
                    )
                )
                if trackers and cooldown
                else set()
            )
            event_values: list[dict[str, Any]] = []
            for tracker in trackers:
                events = evaluate_rules(
                    TrackerRules(
                        target=tracker.target_price,
                        any_drop=tracker.notify_on_any_drop,
                        on_target=tracker.notify_on_target
                        and rights[tracker.user_id].target_price_alerts,
                        back_in_stock=tracker.notify_on_back_in_stock
                        and rights[tracker.user_id].back_in_stock_alerts,
                        historical_low=tracker.notify_on_historical_low
                        and rights[tracker.user_id].historical_low_alerts,
                    ),
                    previous=previous,
                    current=data.price,
                    previous_availability=previous_availability,
                    availability=data.availability,
                    historical_minimum=minimum,
                )
                if not events:
                    continue
                if (
                    tracker.last_notified_at
                    and (now - tracker.last_notified_at).total_seconds() < cooldown
                ):
                    if events[0] not in ("target_reached", "back_in_stock"):
                        continue
                    if (tracker.id, events[0]) in recent_priority:
                        continue
                event_values.append(
                    {
                        "id": uuid4(),
                        "tracker_id": tracker.id,
                        "event_type": events[0],
                        "price": data.price,
                        "currency": data.currency,
                        "deduplication_key": f"{tracker.id}:{observation_id}",
                    }
                )
                tracker.last_notified_at = now
            if event_values:
                await session.execute(
                    insert(NotificationEvent).on_conflict_do_nothing(
                        index_elements=[NotificationEvent.deduplication_key],
                    ),
                    event_values,
                )
            return True

    async def prune_history(self, *, before: datetime, batch_size: int = 5000) -> int:
        # Explicit operator command; never deletes financial events or outbox records.
        from sqlalchemy import delete

        async with self.sessions.begin() as session:
            ids = (
                select(PriceObservation.id)
                .where(PriceObservation.checked_at < before)
                .limit(batch_size)
            )
            rows = await session.scalars(
                delete(PriceObservation)
                .where(
                    PriceObservation.id.in_(ids),
                )
                .returning(PriceObservation.id)
            )
            return len(list(rows))
