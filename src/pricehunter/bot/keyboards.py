from uuid import UUID

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from babel import Locale

from pricehunter.domain.markets import MARKET_CHOICES
from pricehunter.localization.languages import LANGUAGE_NAMES, SUPPORTED_LANGUAGES
from pricehunter.localization.messages import tr


class Action(CallbackData, prefix="ph"):
    action: str
    value: str = ""


def button(
    language: str, key: str, action: str, value: str = "", **values: object
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=tr(language, key, **values),
        callback_data=Action(
            action=action,
            value=value,
        ).pack(),
    )


def main_menu(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button(language, "search", "search"), button(language, "my", "my", "0")],
            [button(language, "plans", "plans"), button(language, "settings", "settings")],
            [
                button(language, "my_watches", "watches", "0"),
                button(language, "language", "settings"),
            ],
        ]
    )


def offer_keyboard(language: str, offer_id: UUID, url: str | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(language, "track", "track", offer_id.hex),
                button(language, "target", "otarget", offer_id.hex),
            ],
            [
                button(language, "history", "history", offer_id.hex),
                *([InlineKeyboardButton(text=tr(language, "open_store"), url=url)] if url else []),
            ],
        ]
    )


def country_keyboard(
    language: str, *, action: str = "country", prefix: str = ""
) -> InlineKeyboardMarkup:
    locale = Locale(language)
    buttons = [
        InlineKeyboardButton(
            text=f"{country} · {locale.territories.get(country, country)}",
            callback_data=Action(action=action, value=prefix + country).pack(),
        )
        for country in MARKET_CHOICES
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    )


def tracker_keyboard(
    language: str,
    tracker_id: UUID,
    offer_id: UUID,
    enabled: bool,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(language, "target", "target", tracker_id.hex),
                button(language, "history", "history", offer_id.hex),
            ],
            [
                button(
                    language,
                    "pause" if enabled else "resume",
                    "pause" if enabled else "resume",
                    tracker_id.hex,
                ),
                button(language, "delete", "delete", tracker_id.hex),
            ],
        ]
    )


def settings_keyboard(language: str) -> InlineKeyboardMarkup:
    languages = [
        InlineKeyboardButton(
            text=("✓ " if code == language else "") + LANGUAGE_NAMES[code],
            callback_data=Action(action="lang", value=code).pack(),
        )
        for code in SUPPORTED_LANGUAGES
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *[languages[i : i + 2] for i in range(0, len(languages), 2)],
            [button(language, key, "setting", key) for key in ("country", "currency", "timezone")],
            [
                button(language, "delivery_country", "setting", "delivery_country"),
                button(language, "delivery_postal", "setting", "delivery_postal"),
            ],
            [button(language, "delivery_clear", "dclear")],
            [button(language, "back", "menu")],
        ]
    )


def variant_keyboard(
    language: str, options: list[dict[str, str]], token: str, page: int = 0
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=option["label"],
                callback_data=Action(action="variant", value=f"{token}_{i}").pack(),
            )
        ]
        for i, option in enumerate(options)
        if page * 8 <= i < (page + 1) * 8
    ]
    navigation = []
    if page:
        navigation.append(button(language, "back", "vpage", f"{token}_{page - 1}"))
    if (page + 1) * 8 < len(options):
        navigation.append(button(language, "next", "vpage", f"{token}_{page + 1}"))
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)
