import time
from uuid import UUID, uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from pricehunter.bot.keyboards import (
    Action,
    button,
    main_menu,
    offer_keyboard,
    settings_keyboard,
    tracker_keyboard,
    variant_keyboard,
)
from pricehunter.core.container import Container
from pricehunter.db.models import User
from pricehunter.domain.errors import VariantSelectionRequiredError
from pricehunter.domain.pricing import parse_target, percentage_change
from pricehunter.domain.subscriptions import Feature
from pricehunter.localization.languages import LANGUAGE_NAMES, normalize_language
from pricehunter.localization.messages import money, tr
from pricehunter.providers.woocommerce import WooCommerceProvider
from pricehunter.schemas.api import OfferView, TrackerCreate, TrackerPatch, UserSettingsPatch


class Conversation(StatesGroup):
    target = State()
    search = State()
    setting = State()
    variant = State()


async def resolve_offer(
    message: Message, url: str, container: Container, user: User, state: FSMContext
) -> None:
    try:
        offer = await container.products.resolve(url, user.id)
    except VariantSelectionRequiredError as exc:
        if not exc.options:
            raise
        token = uuid4().hex[:12]
        options = [{"label": option.label, "url": option.url} for option in exc.options]
        await state.set_state(Conversation.variant)
        await state.set_data(
            {
                "variant_token": token,
                "variant_options": options,
                "variant_created": time.time(),
            }
        )
        await message.answer(
            tr(user.language_code, "choose_variant", title=exc.title),
            reply_markup=variant_keyboard(user.language_code, options, token),
        )
        return
    await state.clear()
    await show_offer(message, offer, user.language_code)


async def show_offer(message: Message, offer: OfferView, language: str) -> None:
    from urllib.parse import urlsplit

    text = tr(
        language,
        "card",
        title=offer.title,
        store=urlsplit(offer.url).hostname,
        price=money(offer.price, offer.currency, language),
        minimum=money(offer.minimum_price, offer.currency, language),
        availability=tr(language, offer.availability),
    )
    if offer.original_price:
        text += tr(
            language, "original", price=money(offer.original_price, offer.currency, language)
        )
    keyboard = offer_keyboard(language, offer.id, offer.url)
    if offer.image_url and offer.image_url.startswith("https://"):
        try:
            await message.answer_photo(offer.image_url, caption=text, reply_markup=keyboard)
            return
        except TelegramBadRequest:
            pass  # Broken retailer images must not break the product card.
    await message.answer(text, reply_markup=keyboard)


async def show_my(message: Message, container: Container, user: User, page: int = 0) -> None:
    language = user.language_code
    trackers = await container.trackers.list(user.id, page=page, size=5)
    if not trackers:
        await message.answer(tr(language, "empty"), reply_markup=main_menu(language))
        return
    for tracker in trackers:
        offer = tracker.offer
        await message.answer(
            tr(
                language,
                "tracker_card",
                title=offer.title,
                price=money(offer.price, offer.currency, language),
                change=f"{percentage_change(tracker.baseline_price, offer.price):+.2f}",
                target=money(tracker.target_price, offer.currency, language)
                if tracker.target_price
                else tr(language, "no_target"),
                status=tr(
                    language,
                    "active"
                    if tracker.scheduled
                    else "quota_paused"
                    if tracker.enabled
                    else "inactive",
                ),
            ),
            reply_markup=tracker_keyboard(language, tracker.id, offer.id, tracker.enabled),
        )
    navigation = []
    if page:
        navigation.append(button(language, "back", "my", str(page - 1)))
    if len(trackers) == 5:
        navigation.append(button(language, "next", "my", str(page + 1)))
    if navigation:
        await message.answer(
            tr(language, "my"), reply_markup=InlineKeyboardMarkup(inline_keyboard=[navigation])
        )


async def show_settings(message: Message, user: User) -> None:
    await message.answer(
        tr(
            user.language_code,
            "settings_text",
            language=LANGUAGE_NAMES[normalize_language(user.language_code)],
            country=user.country_code or "—",
            currency=user.preferred_currency,
            timezone=user.timezone,
        ),
        reply_markup=settings_keyboard(user.language_code),
    )


async def run_search(message: Message, query: str, container: Container, user: User) -> None:
    results = await container.search.search(query[:200], user.id, country=user.country_code)
    language = user.language_code
    if results.unavailable_providers:
        await message.answer(tr(language, "search_partial"))
    if not results.products:
        await message.answer(tr(language, "search_empty"))
        return
    from pricehunter.bot.comparison import show_comparison

    for product in results.products:
        await show_comparison(message, product, language)


def build_router() -> Router:
    router = Router(name="pricehunter")

    @router.message(CommandStart())
    async def start(
        message: Message, container: Container, language: str, state: FSMContext
    ) -> None:
        await state.clear()
        key = "start" if container.settings.mock_provider_enabled else "start_real"
        await message.answer(
            tr(language, key) + "\n\n" + tr(language, "stores_hint"),
            reply_markup=main_menu(language),
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext, language: str) -> None:
        await state.clear()
        await message.answer(tr(language, "cancelled"), reply_markup=main_menu(language))

    @router.message(Command("help"))
    async def help_command(message: Message, language: str) -> None:
        await message.answer(
            tr(language, "help") + "\n" + tr(language, "stores_hint"),
            reply_markup=main_menu(language),
        )

    @router.message(Command("stores"))
    async def stores_command(
        message: Message,
        container: Container,
        language: str,
        state: FSMContext,
    ) -> None:
        await state.clear()
        lines = []
        for provider in container.registry.providers.values():
            if isinstance(provider, WooCommerceProvider):
                lines.append(f"{provider.shop.name} — https://{provider.shop.domain}")
            elif provider.name == "ebay":
                lines.append("eBay — " + ", ".join(container.settings.ebay_marketplaces))
            elif provider.name == "amazon":
                lines.append("Amazon — " + ", ".join(container.settings.amazon_marketplaces))
        await message.answer(
            tr(language, "stores_list", stores="\n".join(lines))
            if lines
            else tr(language, "stores_empty")
        )

    @router.message(Command("track"))
    async def track_command(
        message: Message,
        command: CommandObject,
        container: Container,
        user: User,
        state: FSMContext,
        language: str,
    ) -> None:
        await state.clear()
        if command.args:
            await resolve_offer(message, command.args, container, user, state)
        else:
            await message.answer(tr(language, "url_prompt"))

    @router.message(Command("my"))
    async def my_command(
        message: Message, container: Container, user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_my(message, container, user)

    @router.message(Command("history"))
    async def history_command(message: Message, container: Container, user: User) -> None:
        await message.answer(tr(user.language_code, "history_prompt"))
        await show_my(message, container, user)

    @router.message(Command("settings"))
    async def settings_command(message: Message, user: User, state: FSMContext) -> None:
        await state.clear()
        await show_settings(message, user)

    @router.message(Command("search"))
    async def search_command(
        message: Message,
        command: CommandObject,
        container: Container,
        user: User,
        state: FSMContext,
    ) -> None:
        await state.clear()
        if command.args:
            await run_search(message, command.args, container, user)
        else:
            await state.set_state(Conversation.search)
            await message.answer(tr(user.language_code, "search_prompt"))

    @router.callback_query(Action.filter())
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
        message = query.message
        action, value = callback_data.action, callback_data.value
        if action in ("variant", "vpage"):
            data = await state.get_data()
            token, _, number = value.partition("_")
            options = data.get("variant_options", [])
            valid = (
                token == data.get("variant_token")
                and time.time() - data.get("variant_created", 0) < 900
                and number.isdigit()
                and len(number) <= 3
            )
            index = int(number) if valid else -1
            limit = len(options) if action == "variant" else (len(options) + 7) // 8
            if not valid or not 0 <= index < limit:
                await query.answer(tr(language, "variant_expired"), show_alert=True)
                return
            await query.answer()
            if action == "vpage":
                await message.edit_reply_markup(
                    reply_markup=variant_keyboard(language, options, token, index)
                )
            else:
                selected_url = options[index]["url"]
                await resolve_offer(message, selected_url, container, user, state)
            return
        await state.clear()
        if action == "my":
            await show_my(message, container, user, min(max(int(value), 0), 10000))
        elif action == "search":
            await state.set_state(Conversation.search)
            await message.answer(tr(language, "search_prompt"))
        elif action == "menu":
            key = "start" if container.settings.mock_provider_enabled else "start_real"
            await message.answer(tr(language, key), reply_markup=main_menu(language))
        elif action == "settings":
            await show_settings(message, user)
        elif action == "lang":
            user = await container.users.settings(user.id, UserSettingsPatch(language_code=value))
            await show_settings(message, user)
        elif action == "setting" and value in ("country", "currency", "timezone"):
            await state.set_state(Conversation.setting)
            await state.update_data(setting=value)
            await message.answer(tr(language, value + "_prompt"))
        elif action in ("track", "otarget"):
            if action == "otarget":
                (await container.entitlements.for_user(user.id)).entitlements.require(
                    Feature.TARGET_ALERTS
                )
            tracker = await container.trackers.create(
                user.id, TrackerCreate(store_offer_id=UUID(value))
            )
            if action == "track":
                await message.answer(
                    tr(language, "tracked"),
                    reply_markup=tracker_keyboard(
                        language,
                        tracker.id,
                        tracker.offer.id,
                        tracker.enabled,
                    ),
                )
            else:
                await state.set_state(Conversation.target)
                await state.update_data(tracker_id=str(tracker.id))
                await message.answer(tr(language, "target_prompt", currency=tracker.offer.currency))
        elif action == "target":
            (await container.entitlements.for_user(user.id)).entitlements.require(
                Feature.TARGET_ALERTS
            )
            tracker = await container.trackers.get(user.id, UUID(value))
            await state.set_state(Conversation.target)
            await state.update_data(tracker_id=str(tracker.id))
            await message.answer(tr(language, "target_prompt", currency=tracker.offer.currency))
        elif action in ("pause", "resume"):
            tracker = await container.trackers.update(
                user.id, UUID(value), TrackerPatch(enabled=action == "resume")
            )
            await message.answer(
                tr(
                    language,
                    "paused"
                    if action == "pause"
                    else "resumed"
                    if tracker.scheduled
                    else "quota_paused",
                ),
                reply_markup=tracker_keyboard(
                    language, tracker.id, tracker.offer.id, tracker.enabled
                ),
            )
        elif action == "delete":
            await container.trackers.delete(user.id, UUID(value))
            await message.answer(tr(language, "deleted"))
        elif action == "history":
            history = await container.products.history(UUID(value), user.id)
            await message.answer(
                tr(
                    language,
                    "history_card",
                    current=money(history.current, history.currency, language),
                    minimum=money(history.minimum, history.currency, language),
                    maximum=money(history.maximum, history.currency, language),
                    average=money(history.average, history.currency, language),
                    count=history.count,
                    change=f"{history.change_since_tracking:+.2f}%"
                    if history.change_since_tracking is not None
                    else "—",
                )
            )
        await query.answer()

    @router.message(Conversation.target, F.text)
    async def target_input(
        message: Message,
        container: Container,
        user: User,
        language: str,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        tracker = await container.trackers.get(user.id, UUID(data["tracker_id"]))
        target = parse_target(message.text or "", tracker.offer.price)
        await container.trackers.update(user.id, tracker.id, TrackerPatch(target_price=target))
        await state.clear()
        await message.answer(
            tr(language, "target_saved", target=money(target, tracker.offer.currency, language))
        )

    @router.message(Conversation.setting, F.text)
    async def setting_input(
        message: Message,
        container: Container,
        user: User,
        language: str,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        field = {
            "country": "country_code",
            "currency": "preferred_currency",
            "timezone": "timezone",
        }[data["setting"]]
        value = (message.text or "").strip()
        patch = UserSettingsPatch.model_validate(
            {field: value if field == "timezone" else value.upper()}
        )
        user = await container.users.settings(user.id, patch)
        await state.clear()
        await message.answer(tr(language, "settings_saved"))
        await show_settings(message, user)

    @router.message(Conversation.search, F.text)
    async def search_input(
        message: Message,
        container: Container,
        user: User,
        state: FSMContext,
    ) -> None:
        await run_search(message, message.text or "", container, user)
        await state.clear()

    @router.message(F.text)
    async def text_input(
        message: Message, container: Container, user: User, language: str, state: FSMContext
    ) -> None:
        text = (message.text or "").strip()
        if text.startswith(("https://", "http://")):
            await resolve_offer(message, text, container, user, state)
        else:
            await message.answer(tr(language, "url_prompt"), reply_markup=main_menu(language))

    return router
