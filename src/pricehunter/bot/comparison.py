from uuid import UUID

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

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


def offer_line(offer: ComparisonOffer, language: str) -> str:
    return tr(
        language,
        "comparison_offer",
        store=offer.store[:60],
        country=offer.store_country,
        price=money(offer.price, offer.currency, language),
        availability=tr(language, offer.availability),
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
        if group.price_spread is not None:
            lines.append(
                tr(
                    language,
                    "comparison_spread",
                    spread=money(group.price_spread, group.currency, language),
                )
            )
    lines.extend(offer_line(offer, language) for offer in product.offers[:4])
    lines.append(tr(language, "comparison_price_note"))
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
                {"compare", "offers", "details", "watch", "watches", "wpause", "wresume", "wdelete"}
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
        if action == "watches":
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
