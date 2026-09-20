from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from pricehunter.bot.comparison import show_comparison, show_watches
from pricehunter.domain.comparison import ComparisonProduct, currency_comparisons
from pricehunter.localization.messages import tr
from tests.unit.test_comparison_policy import comparison_offer


@pytest.mark.parametrize("language", ["en", "fr", "de", "es", "it", "pl", "ru"])
async def test_comparison_bounds_and_html_escaping_for_long_merchant_content(language):
    offers = [
        comparison_offer("12345", currency=currency, store='"&' * 50)
        for currency in ("EUR", "USD", "GBP", "CHF")
    ]
    product = ComparisonProduct(
        id=uuid4(),
        canonical_name='"&' * 250,
        brand=None,
        model=None,
        variant={},
        image_url=None,
        offers=offers,
        currencies=[o.currency for o in offers],
        currency_groups=currency_comparisons(offers),
        best_available_offer=None,
        price_spread=None,
        match_confidence=1,
        store_count=4,
        offer_count=4,
    )
    message = SimpleNamespace(answer=AsyncMock())
    await show_comparison(message, product, language)
    assert message.answer.await_count > 1
    for call in message.answer.await_args_list:
        assert len(call.args[0].encode("utf-16-le")) // 2 <= 3500
        assert "&quot;&amp;" in call.args[0]
    assert message.answer.await_args.kwargs["reply_markup"]
    unavailable = offers[0].model_copy(update={"availability": "out_of_stock"})
    product = product.model_copy(update={"currency_groups": currency_comparisons([unavailable])})
    message.answer.reset_mock()
    await show_comparison(message, product, language)
    text = "\n".join(call.args[0] for call in message.answer.await_args_list)
    assert tr(language, "comparison_none", currency="EUR") in text
    assert tr(language, "cheapest_known") in text


async def test_watch_pages_show_quota_paused_disabled_and_active_states():
    watches = [
        SimpleNamespace(
            id=uuid4(),
            product_id=uuid4(),
            canonical_name="Headphones <script>",
            currency="EUR",
            scheduled=index == 0,
            enabled=index != 1,
        )
        for index in range(5)
    ]
    container = SimpleNamespace(watches=SimpleNamespace(list=AsyncMock(return_value=watches)))
    user = SimpleNamespace(id=uuid4(), language_code="en")
    message = SimpleNamespace(answer=AsyncMock())
    await show_watches(message, container, user, page=1)
    text = "\n".join(call.args[0] for call in message.answer.await_args_list)
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert tr("en", "quota_paused") in text and tr("en", "inactive") in text
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert [b.text for b in keyboard.inline_keyboard[0]] == [tr("en", "back"), tr("en", "next")]
