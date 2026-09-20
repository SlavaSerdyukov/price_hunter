from decimal import Decimal
from html.parser import HTMLParser
from string import Formatter

import pytest
from pydantic import ValidationError

from pricehunter.bot.keyboards import Action, settings_keyboard
from pricehunter.localization.languages import LANGUAGE_NAMES, SUPPORTED_LANGUAGES
from pricehunter.localization.messages import CATALOGS, EN, money, tr
from pricehunter.schemas.api import UserSettingsPatch


class TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        assert tag == "b" and not attrs
        self.tags.append(tag)

    def handle_endtag(self, tag):
        assert self.tags.pop() == tag


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_complete_catalog_placeholders_html_and_invoice_limits(language):
    catalog = CATALOGS[language]
    assert set(catalog) == set(EN)
    for key, template in catalog.items():
        fields = {f for _, f, _, _ in Formatter().parse(template) if f}
        assert fields == {f for _, f, _, _ in Formatter().parse(EN[key]) if f}, key
        rendered = tr(language, key, **dict.fromkeys(fields, '<script>&"'))
        assert "<script>" not in rendered
        parser = TelegramHTML()
        parser.feed(rendered)
        parser.close()
        assert parser.tags == [], key
        assert rendered.strip()
    assert len(tr(language, "invoice_title", plan="Power")) <= 32
    assert len(tr(language, "invoice_description", plan="Power")) <= 255
    assert UserSettingsPatch(language_code=language).language_code == language


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_language_picker_contains_native_names_and_marks_only_selected(language):
    buttons = [
        b
        for row in settings_keyboard(language).inline_keyboard
        for b in row
        if b.callback_data and Action.unpack(b.callback_data).action == "lang"
    ]
    assert [Action.unpack(b.callback_data).value for b in buttons] == list(SUPPORTED_LANGUAGES)
    assert [b.text.removeprefix("✓ ") for b in buttons] == list(LANGUAGE_NAMES.values())
    assert [Action.unpack(b.callback_data).value for b in buttons if b.text.startswith("✓ ")] == [
        language
    ]


@pytest.mark.parametrize(
    "language,amount",
    [
        ("en", "1,234.56"),
        ("fr", "1\u202f234,56"),
        ("de", "1.234,56"),
        ("es", "1.234,56"),
        ("it", "1.234,56"),
        ("pl", "1\u00a0234,56"),
        ("ru", "1\u00a0234,56"),
    ],
)
def test_currency_format_matches_selected_language(language, amount):
    formatted = money(Decimal("1234.56"), "EUR", language)
    assert amount in formatted and "€" in formatted


def test_english_fallback_and_supported_regional_codes(monkeypatch):
    assert tr("unknown", "settings") == tr("en", "settings")
    assert tr("fr-BE", "settings") == tr("fr", "settings")
    assert tr("DE_de", "settings") == tr("de", "settings")
    monkeypatch.delitem(CATALOGS["pl"], "settings")
    assert tr("pl", "settings") == tr("en", "settings")
    for unsupported in ("nl", "FR", "fr-BE", "", None):
        with pytest.raises(ValidationError):
            UserSettingsPatch(language_code=unsupported)
