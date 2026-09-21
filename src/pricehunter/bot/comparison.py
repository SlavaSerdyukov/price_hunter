from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from babel.dates import format_datetime

from pricehunter.bot.keyboards import Action, button
from pricehunter.core.container import Container
from pricehunter.db.models import User
from pricehunter.domain.comparison import ComparisonOffer, ComparisonProduct
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
    return (
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


async def show_comparison(message: Message, product: ComparisonProduct, language: str) -> None:
    lines = [
        tr(
            language,
            "comparison_heading",
            title=product.canonical_name[:180],
            count=product.store_count,
        )
    ]
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
                    InlineKeyboardButton(
                        text=tr(language, "best_offer", currency=group.currency),
                        url=best.url,
                    ),
                    button(language, "watch_best", "watch", f"{product.id.hex}_{group.currency}"),
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
                [button(language, "watch_best", "watch", f"{product.id.hex}_{group.currency}")]
            )
        rows.append(
            [
                button(
                    language,
                    "best_price_history",
                    "besthist",
                    f"{product.id.hex}_{group.currency}",
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
    if any(d.status == "backed_off" for d in product.discovery):
        lines.append(tr(language, "discovery_partial"))
    lines.append(tr(language, "comparison_price_note"))
    rows.append([button(language, "refresh_prices", "refreshcmp", product.id.hex)])
    rows.append(
        [
            button(language, "all_offers", "offers", f"{product.id.hex}_0"),
            button(language, "comparison_details", "details", product.id.hex),
        ]
    )
    await answer_sections(message, lines, rows)


async def show_offers(
    message: Message, container: Container, user: User, product_id: UUID, page: int
) -> None:
    product = await container.products.comparisons.get(product_id, user.id, page=page, size=5)
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
                InlineKeyboardButton(text=f"{index}. {tr(language, 'open_store')}", url=offer.url),
                button(language, "track", "track", offer.offer_id.hex),
            ]
        )
    navigation = []
    if page:
        navigation.append(button(language, "back", "offers", f"{product_id.hex}_{page - 1}"))
    if (page + 1) * 5 < product.offer_count:
        navigation.append(button(language, "next", "offers", f"{product_id.hex}_{page + 1}"))
    if navigation:
        rows.append(navigation)
    for currency in sorted({offer.currency for offer in product.offers}):
        rows.append([button(language, "watch_best", "watch", f"{product_id.hex}_{currency}")])
    rows.append([button(language, "compare_stores", "compare", product_id.hex)])
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
                    [button(language, "compare_stores", "compare", watch.product_id.hex)],
                    [
                        button(
                            language,
                            "best_price_history",
                            "besthist",
                            f"{watch.product_id.hex}_{watch.currency}",
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
    message: Message, container: Container, user: User, product_id: UUID, currency: str
) -> None:
    history = await container.best_prices.history(product_id, user.id, currency, limit=10)
    language = user.language_code
    best = history.current_best
    current = (
        f"{money(best.price, currency, language)} · {best.store[:60]}"
        if best
        else tr(language, "best_unknown")
    )
    lines = [
        tr(language, "best_history_title", title=history.canonical_name[:180], currency=currency),
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
    rows = [[button(language, "compare_stores", "compare", product_id.hex)]]
    if best:
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
                    "offers",
                    "details",
                    "watch",
                    "watches",
                    "wpause",
                    "wresume",
                    "wdelete",
                    "besthist",
                    "refreshcmp",
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
        if action == "besthist":
            product_id, currency = value.split("_", 1)
            if (
                len(currency) != 3
                or not currency.isascii()
                or not currency.isalpha()
                or not currency.isupper()
            ):
                raise ValueError("Invalid currency")
            await show_best_history(query.message, container, user, UUID(product_id), currency)
        elif action == "refreshcmp":
            accepted = await container.comparison_operations.request_refresh(UUID(value), user.id)
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
        elif action == "watch":
            product_id, currency = value.split("_", 1)
            await container.watches.create(
                user.id, WatchCreate(product_id=UUID(product_id), currency=currency)
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
            product_id, page = value.split("_", 1)
            await show_offers(
                query.message, container, user, UUID(product_id), min(max(int(page), 0), 10000)
            )
        else:
            product = await container.products.product(UUID(value), user.id)
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
