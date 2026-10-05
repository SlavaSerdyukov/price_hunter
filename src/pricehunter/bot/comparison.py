from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from babel.dates import format_datetime

from pricehunter.bot.keyboards import Action, button, country_keyboard
from pricehunter.core.container import Container
from pricehunter.db.models import User
from pricehunter.domain.comparison import ComparisonOffer, ComparisonProduct
from pricehunter.domain.delivery import DeliveryContext
from pricehunter.domain.errors import InvalidDeliveryContextError
from pricehunter.localization.messages import money, tr
from pricehunter.schemas.watches import WatchCreate, WatchPatch


async def answer_sections(
    message: Message, sections: list[str], rows: list[list[InlineKeyboardButton]]
) -> None:
    # Each section contains complete escaped HTML. Split only between sections,
    # leaving room for UTF-16 surrogate pairs and attaching actions to the last message.
    text = ""
    for section in sections:
        combined = f"{text}\n\n{section}" if text else section
        if len(combined.encode("utf-16-le")) // 2 > 3500 and text:
            await message.answer(text)
            text = section
        else:
            text = combined
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def age_text(seconds: int, language: str) -> str:
    if seconds < 60:
        return tr(language, "age_now")
    divisor, key = (
        (60, "age_minutes")
        if seconds < 3600
        else (3600, "age_hours")
        if seconds < 86400
        else (86400, "age_days")
    )
    return tr(language, key, count=seconds // divisor)


def offer_line(offer: ComparisonOffer, language: str) -> str:
    text = (
        tr(
            language,
            "comparison_offer",
            store=offer.store[:60],
            country=offer.store_country,
            price=money(offer.price, offer.currency, language),
            availability=tr(language, offer.availability),
        )
        + "\n"
        + tr(language, f"freshness_{offer.freshness}", age=age_text(offer.age_seconds, language))
    )

    if offer.reference_price is not None and offer.reference_currency:
        text += "\n" + tr(
            language,
            "fx_reference",
            price=money(offer.reference_price, offer.reference_currency, language),
            source=offer.fx_source or "ECB",
            date=offer.fx_effective_date,
        )
    if offer.attribution:
        text += "\n" + tr(language, "provider_attribution", attribution=offer.attribution)
    return text


async def show_comparison(message: Message, product: ComparisonProduct, language: str) -> None:
    lines = [
        tr(
            language,
            "comparison_heading",
            title=product.canonical_name[:180],
            count=product.store_count,
        )
    ]
    if product.market_country:
        lines.append(tr(language, "market_context", country=product.market_country))
    rows = []
    for group in product.currency_groups[:4]:
        best = group.best_available_offer
        if best:
            lines.append(
                tr(language, "comparison_best_label")
                + f" {group.currency}\n"
                + offer_line(best, language)
            )
            rows.append(
                [
                    *(
                        [
                            InlineKeyboardButton(
                                text=tr(language, "best_offer", currency=group.currency),
                                url=best.url,
                            )
                        ]
                        if best.url
                        else []
                    ),
                    button(
                        language,
                        "watch_best",
                        "watch",
                        f"{product.id.hex}_{group.currency}"
                        + (f"_{product.market_country}" if product.market_country else ""),
                    ),
                ]
            )
        else:
            lines.append(tr(language, "comparison_none", currency=group.currency))
            lines.append(
                tr(language, "cheapest_known")
                + "\n"
                + offer_line(group.cheapest_known_offer, language)
            )
            rows.append(
                [
                    button(
                        language,
                        "watch_best",
                        "watch",
                        f"{product.id.hex}_{group.currency}"
                        + (f"_{product.market_country}" if product.market_country else ""),
                    )
                ]
            )
        rows.append(
            [
                button(
                    language,
                    "best_price_history",
                    "besthist",
                    f"{product.id.hex}_{group.currency}"
                    + (f"_{product.market_country}" if product.market_country else ""),
                    currency=group.currency,
                )
            ]
        )
        if not group.fresh_offer_count:
            lines.append(tr(language, "no_fresh_prices", currency=group.currency))
        if group.price_spread is not None:
            lines.append(
                tr(
                    language,
                    "comparison_spread",
                    spread=money(group.price_spread, group.currency, language),
                )
            )
    lines.extend(offer_line(offer, language) for offer in product.offers[:4])
    if product.delivery_country:
        lines.append(tr(language, "delivery_heading", country=product.delivery_country))
        for group in product.currency_groups[:4]:
            delivered = group.best_delivered_offer
            if (
                delivered is not None
                and delivered.delivered_total is not None
                and delivered.shipping_price is not None
            ):
                lines.append(
                    tr(
                        language,
                        "delivery_best",
                        store=delivered.store,
                        item=money(delivered.price, delivered.currency, language),
                        shipping=money(delivered.shipping_price, delivered.currency, language),
                        total=money(delivered.delivered_total, delivered.currency, language),
                    )
                )
                if delivered.url:
                    rows.append(
                        [
                            InlineKeyboardButton(
                                text=tr(language, "delivery_open"), url=delivered.url
                            )
                        ]
                    )
        if (
            sum(g.delivered_offer_count for g in product.currency_groups) < product.offer_count
            or not product.delivered_offers
            or any(o.delivery_quote_status != "complete" for o in product.delivered_offers)
        ):
            lines.append(tr(language, "delivery_incomplete"))
    if any(d.status == "backed_off" for d in product.discovery):
        lines.append(tr(language, "discovery_partial"))
    lines.append(tr(language, "comparison_price_note"))
    rows.append(
        [
            button(
                language,
                "delivery_compare",
                "delcmp",
                product.id.hex + (f"_{product.market_country}" if product.market_country else ""),
            ),
            button(
                language,
                "delivery_quote",
                "delquote",
                product.id.hex + (f"_{product.market_country}" if product.market_country else ""),
            ),
        ]
    )
    rows.append(
        [
            button(
                language,
                "refresh_prices",
                "refreshcmp",
                product.id.hex + (f"_{product.market_country}" if product.market_country else ""),
            )
        ]
    )
    rows.append(
        [
            button(
                language,
                "all_offers",
                "offers",
                f"{product.id.hex}_0"
                + (f"_{product.market_country}" if product.market_country else ""),
            ),
            button(
                language,
                "comparison_details",
                "details",
                product.id.hex + (f"_{product.market_country}" if product.market_country else ""),
            ),
        ]
    )
    await answer_sections(message, lines, rows)


async def show_offers(
    message: Message,
    container: Container,
    user: User,
    product_id: UUID,
    page: int,
    market_country: str | None = None,
) -> None:
    product = await container.products.comparisons.get(
        product_id, user.id, page=page, size=5, market_country=market_country
    )
    language = user.language_code
    lines = [
        tr(
            language,
            "comparison_heading",
            title=product.canonical_name[:180],
            count=product.store_count,
        )
    ]
    rows = []
    for index, offer in enumerate(product.offers, page * 5 + 1):
        lines.append(f"{index}. " + offer_line(offer, language))
        rows.append(
            [
                *(
                    [
                        InlineKeyboardButton(
                            text=f"{index}. {tr(language, 'open_store')}", url=offer.url
                        )
                    ]
                    if offer.url
                    else []
                ),
                button(language, "track", "track", offer.offer_id.hex),
            ]
        )
    navigation = []
    if page:
        navigation.append(
            button(
                language,
                "back",
                "offers",
                f"{product_id.hex}_{page - 1}"
                + (f"_{product.market_country}" if product.market_country else ""),
            )
        )
    if (page + 1) * 5 < product.offer_count:
        navigation.append(
            button(
                language,
                "next",
                "offers",
                f"{product_id.hex}_{page + 1}"
                + (f"_{product.market_country}" if product.market_country else ""),
            )
        )
    if navigation:
        rows.append(navigation)
    for currency in sorted({offer.currency for offer in product.offers}):
        rows.append(
            [
                button(
                    language,
                    "watch_best",
                    "watch",
                    f"{product_id.hex}_{currency}"
                    + (f"_{product.market_country}" if product.market_country else ""),
                )
            ]
        )
    rows.append(
        [
            button(
                language,
                "compare_stores",
                "compare",
                product_id.hex + (f"_{product.market_country}" if product.market_country else ""),
            )
        ]
    )
    await answer_sections(message, lines, rows)


async def show_watches(message: Message, container: Container, user: User, page: int = 0) -> None:
    watches = await container.watches.list(user.id, page=page, size=5)
    language = user.language_code
    if not watches:
        await message.answer(tr(language, "watches_empty"))
        return
    for watch in watches:
        await message.answer(
            tr(
                language,
                "watch_card",
                title=watch.canonical_name[:180],
                market_country=watch.market_country,
                currency=watch.currency,
                status=tr(
                    language,
                    "active"
                    if watch.scheduled
                    else "quota_paused"
                    if watch.enabled
                    else "inactive",
                ),
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [button(language, "compare_stores", "wcompare", watch.id.hex)],
                    [
                        button(
                            language,
                            "best_price_history",
                            "besthist",
                            f"{watch.product_id.hex}_{watch.currency}_{watch.market_country}",
                            currency=watch.currency,
                        )
                    ],
                    [
                        button(
                            language,
                            "pause" if watch.enabled else "resume",
                            "wpause" if watch.enabled else "wresume",
                            watch.id.hex,
                        ),
                        button(language, "delete", "wdelete", watch.id.hex),
                    ],
                ]
            ),
        )
    navigation = []
    if page:
        navigation.append(button(language, "back", "watches", str(page - 1)))
    if len(watches) == 5:
        navigation.append(button(language, "next", "watches", str(page + 1)))
    if navigation:
        await message.answer(
            tr(language, "my_watches"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[navigation]),
        )


async def show_best_history(
    message: Message,
    container: Container,
    user: User,
    product_id: UUID,
    currency: str,
    *,
    market_country: str | None = None,
) -> None:
    history = await container.best_prices.history(
        product_id, user.id, currency, limit=10, market_country=market_country
    )
    language = user.language_code
    best = history.current_best
    current = (
        f"{money(best.price, currency, language)} · {best.store[:60]}"
        if best
        else tr(language, "best_unknown")
    )
    lines = [
        tr(language, "best_history_title", title=history.canonical_name[:180], currency=currency),
        tr(language, "market_context", country=history.market_country),
        tr(language, "best_history_current", current=current),
        tr(
            language,
            "best_history_low",
            days=history.retention_days,
            price=money(history.minimum, currency, language)
            if history.minimum is not None
            else "—",
        ),
    ]
    for point in reversed(history.points):
        lines.append(
            tr(
                language,
                "best_history_point",
                date=format_datetime(
                    point.timestamp, "short", tzinfo=ZoneInfo(user.timezone), locale=language
                ),
                price=money(point.price, currency, language) if point.price is not None else "—",
                store=(point.store or "—")[:60],
                event=tr(language, f"best_history_{point.event_type}"),
            )
        )
    if not history.points:
        lines.append(tr(language, "best_history_empty"))
    rows = [
        [
            button(
                language,
                "compare_stores",
                "compare",
                f"{product_id.hex}_{history.market_country}",
            )
        ]
    ]
    if best and best.url:
        rows.append(
            [InlineKeyboardButton(text=tr(language, "best_offer", currency=currency), url=best.url)]
        )
    rows.append([button(language, "back", "watches", "0")])
    await answer_sections(message, lines, rows)


def build_comparison_router() -> Router:
    router = Router(name="comparison")

    @router.message(Command("watches"))
    async def watch_list(
        message: Message, container: Container, user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_watches(message, container, user)

    @router.callback_query(
        Action.filter(
            F.action.in_(
                {
                    "compare",
                    "wcompare",
                    "offers",
                    "details",
                    "watch",
                    "wmarket",
                    "watches",
                    "wpause",
                    "wresume",
                    "wdelete",
                    "besthist",
                    "refreshcmp",
                    "delcmp",
                    "delquote",
                }
            )
        )
    )
    async def callback(
        query: CallbackQuery,
        callback_data: Action,
        container: Container,
        user: User,
        language: str,
        state: FSMContext,
    ) -> None:
        if not isinstance(query.message, Message):
            await query.answer()
            return
        await query.answer()
        await state.clear()
        action, value = callback_data.action, callback_data.value
        if action in ("delcmp", "delquote"):
            if user.delivery_country is None:
                raise InvalidDeliveryContextError()
            parts = value.split("_")
            context = DeliveryContext(
                country=user.delivery_country, postal_code=user.delivery_postal_code
            )
            market = parts[1] if len(parts) == 2 else None
            if action == "delquote":
                product = await container.delivery.request(
                    UUID(parts[0]), user.id, context, market_country=market
                )
            else:
                product = await container.products.comparisons.get(
                    UUID(parts[0]), user.id, market_country=market, delivery_context=context
                )
            await show_comparison(query.message, product, language)
        elif action == "wcompare":
            watch = await container.watches.get(user.id, UUID(value))
            product = await container.products.comparisons.get(
                watch.product_id,
                user.id,
                market_country=watch.market_country,
                permission="tracking_allowed",
            )
            await show_comparison(query.message, product, language)
        elif action == "besthist":
            parts = value.split("_")
            product_id, currency = parts[:2]
            market = parts[2] if len(parts) == 3 else None
            if (
                len(currency) != 3
                or not currency.isascii()
                or not currency.isalpha()
                or not currency.isupper()
            ):
                raise ValueError("Invalid currency")
            await show_best_history(
                query.message, container, user, UUID(product_id), currency, market_country=market
            )
        elif action == "refreshcmp":
            parts = value.split("_")
            accepted = await container.comparison_operations.request_refresh(
                UUID(parts[0]), user.id, market_country=parts[1] if len(parts) == 2 else None
            )
            await query.message.answer(tr(language, "refresh_queued", count=accepted.queued_count))
        elif action == "watches":
            await show_watches(
                query.message, container, user, min(max(int(value or "0"), 0), 10000)
            )
        elif action in ("wpause", "wresume", "wdelete"):
            if action == "wdelete":
                await container.watches.delete(user.id, UUID(value))
            else:
                await container.watches.update(
                    user.id, UUID(value), WatchPatch(enabled=action == "wresume")
                )
            await query.message.answer(
                tr(
                    language,
                    "deleted"
                    if action == "wdelete"
                    else "paused"
                    if action == "wpause"
                    else "resumed",
                )
            )
        elif action in ("watch", "wmarket"):
            parts = value.split("_")
            product_id, currency = parts[:2]
            market = parts[2] if len(parts) == 3 else user.country_code
            if not market:
                await query.message.answer(
                    tr(language, "country_required"),
                    reply_markup=country_keyboard(
                        language, action="wmarket", prefix=f"{product_id}_{currency}_"
                    ),
                )
                return
            await container.watches.create(
                user.id,
                WatchCreate(product_id=UUID(product_id), currency=currency, market_country=market),
            )
            await query.message.answer(
                tr(language, "watch_created", currency=currency),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [button(language, "my_watches", "watches", "0")],
                    ]
                ),
            )
        elif action == "offers":
            parts = value.split("_")
            product_id, page = parts[:2]
            market = parts[2] if len(parts) == 3 else None
            await show_offers(
                query.message,
                container,
                user,
                UUID(product_id),
                min(max(int(page), 0), 10000),
                market_country=market,
            )
        else:
            parts = value.split("_")
            product = await container.products.comparisons.get(
                UUID(parts[0]), user.id, market_country=parts[1] if len(parts) == 2 else None
            )
            await show_comparison(query.message, product, language)
            if action == "details":
                await query.message.answer(
                    tr(
                        language,
                        "comparison_attributes",
                        brand=(product.brand or "—")[:100],
                        model=(product.model or "—")[:100],
                        variant=", ".join(f"{k}: {v}" for k, v in product.variant.items())[:300]
                        or "—",
                    )
                )

    return router
