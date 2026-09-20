from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.core.config import Settings
from pricehunter.db.models import BillingProductRecord
from pricehunter.domain.billing import BillingProduct
from pricehunter.domain.errors import BillingUnavailableError, PaymentRejectedError
from pricehunter.domain.subscriptions import Plan


class BillingCatalog:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def for_plan(self, plan: Plan) -> BillingProduct:
        if plan == Plan.FREE:
            raise PaymentRejectedError()
        return BillingProduct(
            code=f"pricehunter_{plan}_monthly_{self.settings.billing_price_version}",
            plan=plan,
            stars=self.settings.pro_price_stars
            if plan == Plan.PRO
            else self.settings.power_price_stars,
            active=self.settings.stars_billing_enabled,
        )

    async def persist(self, session: AsyncSession, product: BillingProduct) -> BillingProductRecord:
        await session.execute(
            insert(BillingProductRecord)
            .values(
                code=product.code,
                plan=product.plan,
                stars=product.stars,
                subscription_period=product.subscription_period,
                active=product.active,
            )
            .on_conflict_do_nothing(index_elements=[BillingProductRecord.code])
        )
        row = await session.get(BillingProductRecord, product.code)
        assert row is not None
        if (row.plan, row.stars, row.subscription_period) != (
            product.plan,
            product.stars,
            product.subscription_period,
        ) or not row.active:
            # Price changes require a new version; never rewrite an old subscriber's contract.
            raise BillingUnavailableError()
        return row
