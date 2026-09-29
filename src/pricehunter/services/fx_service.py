from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from uuid import uuid4

from babel.numbers import get_currency_precision
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.core.config import Settings
from pricehunter.core.xml import parse_xml
from pricehunter.db.base import utcnow
from pricehunter.db.models import FxRate
from pricehunter.db.session import SessionFactory
from pricehunter.domain.errors import ProviderUnavailableError
from pricehunter.providers.http import ProviderHTTP

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


@dataclass(frozen=True)
class FxSnapshot:
    effective_date: date
    fetched_at: datetime
    rates: dict[str, Decimal]
    source: str = "ECB"

    def cross_rate(self, base: str, quote: str) -> Decimal | None:
        if base == quote:
            return Decimal(1)
        rates = {"EUR": Decimal(1), **self.rates}
        if base not in rates or quote not in rates:
            return None
        with localcontext() as ctx:
            ctx.prec = 28
            return rates[quote] / rates[base]

    def convert(self, amount: Decimal, base: str, quote: str) -> Decimal | None:
        rate = self.cross_rate(base, quote)
        if rate is None:
            return None
        with localcontext() as ctx:
            ctx.prec = 38
            return (amount * rate).quantize(Decimal(1).scaleb(-get_currency_precision(quote)))


def parse_ecb(payload: bytes, now: datetime) -> FxSnapshot:
    root = parse_xml(payload)
    dated = [node for node in root.iter() if "time" in node.attrib]
    try:
        if len(dated) != 1:
            raise ValueError
        effective = date.fromisoformat(dated[0].attrib["time"])
        if effective > now.date():
            raise ValueError
        rates: dict[str, Decimal] = {}
        for node in dated[0]:
            currency, raw = node.attrib["currency"], node.attrib["rate"]
            rate = Decimal(raw)
            if (
                len(currency) != 3
                or not currency.isascii()
                or not currency.isupper()
                or not currency.isalpha()
                or currency == "EUR"
                or currency in rates
                or not rate.is_finite()
                or not Decimal(0) < rate < Decimal("1e12")
                or rate.as_tuple().exponent not in range(-12, 30)
            ):
                raise ValueError
            rates[currency] = rate
        if not 1 <= len(rates) <= 100:
            raise ValueError
        return FxSnapshot(effective, now, rates)
    except (ValueError, KeyError, InvalidOperation) as exc:
        raise ProviderUnavailableError() from exc


class FxService:
    def __init__(self, sessions: SessionFactory, settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings

    async def snapshot(
        self, session: AsyncSession, now: datetime | None = None
    ) -> FxSnapshot | None:
        if not self.settings.fx_enabled:
            return None
        now = now or utcnow()
        latest = (
            select(func.max(FxRate.effective_date)).where(FxRate.source == "ECB").scalar_subquery()
        )
        rows = list(
            await session.scalars(
                select(FxRate).where(
                    FxRate.source == "ECB",
                    FxRate.base_currency == "EUR",
                    FxRate.effective_date == latest,
                )
            )
        )
        if not rows:
            return None
        row = rows[0]
        if (
            not timedelta(0)
            <= now.date() - row.effective_date
            <= timedelta(days=self.settings.fx_max_age_days)
        ):
            return None
        if any(r.fetched_at > now + timedelta(seconds=60) for r in rows):
            return None
        return FxSnapshot(
            row.effective_date,
            min(r.fetched_at for r in rows),
            {r.quote_currency: r.rate for r in rows},
        )

    async def refresh(self, http: ProviderHTTP, redis: Redis) -> bool:
        if not self.settings.fx_enabled:
            return False
        # Shared lease also limits failed fetches across processes. Successful fetches
        # set a longer cadence; a crash can delay the next attempt by at most 5 min.
        token = uuid4().hex
        if not await redis.set("ph:fx:fetch", token, nx=True, ex=300):
            return False
        try:
            snapshot = parse_ecb(
                await http.bytes("GET", ECB_URL, domains={"www.ecb.europa.eu"}), utcnow()
            )
            async with self.sessions.begin() as session:
                # Replace one complete publication atomically; never mix currencies
                # from different revisions of the same effective-date snapshot.
                await session.execute(
                    delete(FxRate).where(
                        FxRate.source == "ECB",
                        FxRate.effective_date == snapshot.effective_date,
                    )
                )
                for currency, rate in snapshot.rates.items():
                    await session.execute(
                        insert(FxRate)
                        .values(
                            id=uuid4(),
                            base_currency="EUR",
                            quote_currency=currency,
                            rate=rate,
                            effective_date=snapshot.effective_date,
                            fetched_at=snapshot.fetched_at,
                            source="ECB",
                        )
                        .on_conflict_do_update(
                            index_elements=[
                                FxRate.base_currency,
                                FxRate.quote_currency,
                                FxRate.effective_date,
                                FxRate.source,
                            ],
                            set_={"rate": rate, "fetched_at": snapshot.fetched_at},
                        )
                    )
            await redis.set("ph:fx:fetch", token, ex=self.settings.fx_refresh_seconds)
            await redis.delete("ph:fx:failures")
            return True
        except Exception:
            failures = int(await redis.incr("ph:fx:failures"))
            await redis.expire("ph:fx:failures", 86400)
            await redis.set("ph:fx:fetch", token, ex=min(3600, 300 * 2 ** min(failures - 1, 4)))
            # Keep the last committed snapshot; no response or credential logging.
            return False
