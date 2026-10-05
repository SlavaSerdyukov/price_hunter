import json
from decimal import Decimal
from html import escape
from importlib.resources import files
from typing import cast

from babel.numbers import format_currency

from pricehunter.localization.languages import NUMBER_LOCALES, normalize_language

EN = {
    "service_unavailable": "The service is temporarily unavailable. Please try again later.",
    "language": "🌐 Language",
    "stores_hint": "/stores — available stores",
    "stores_list": "<b>Available stores</b>\n{stores}\n\nSend a product link or use /search. Prices retain the store's currency. Delivery options depend on the store.",
    "stores_empty": "Real stores are not connected yet. The demo catalog may still be available.",
    "start": "Welcome to <b>PriceHunter</b>.\nSend a product link and I’ll watch its price.\n\nTry the demo:\nhttps://mock.pricehunter.test/products/headphones",
    "start_real": "Welcome to <b>PriceHunter</b>.\nSend a supported product link and I’ll watch its price.",
    "search": "🔎 Search products",
    "my": "📦 My products",
    "plans": "⭐ Plans",
    "settings": "⚙️ Settings",
    "track": "🔔 Track price",
    "target": "🎯 Set target",
    "history": "📊 History",
    "open_store": "🛒 Open store",
    "pause": "Pause",
    "resume": "Resume",
    "delete": "Stop tracking",
    "back": "← Back",
    "next": "Next →",
    "search_prompt": "What are you looking for? Send a product name.",
    "url_prompt": "Send a product URL from a supported store.",
    "target_prompt": "Send your target price in {currency}, or a drop such as 10%.\n/cancel to cancel.",
    "target_saved": "Target saved: {target}",
    "tracked": "Price tracking is on.",
    "deleted": "Tracking stopped.",
    "paused": "Tracking paused.",
    "resumed": "Tracking resumed.",
    "empty": "No tracked products yet. Send a product link to get started.",
    "card": "<b>{title}</b>\n{store}\nCurrent: <b>{price}</b>\nMinimum: {minimum}\n{availability}",
    "original": "\nList price: {price}",
    "tracker_card": "<b>{title}</b>\nCurrent: {price}\nSince tracking: {change}%\nTarget: {target}\n{status}",
    "active": "Tracking",
    "inactive": "Paused",
    "no_target": "Not set",
    "in_stock": "In stock",
    "out_of_stock": "Out of stock",
    "unknown": "Availability unknown",
    "history_card": "<b>Price history</b>\nCurrent: {current}\nMinimum: {minimum}\nMaximum: {maximum}\nAverage: {average}\nSince tracking: {change}\nObservations: {count}",
    "history_prompt": "Choose History on a tracked product.",
    "search_empty": "No products found. Try another name.",
    "search_partial": "Some stores are temporarily unavailable. Showing available results.",
    "plans_text": "<b>PriceHunter plans</b>\nFree: {free} products\nPro: {pro} products\nPower: {power} products\n\nPaid subscriptions are coming later. No payment is taken in this release.",
    "settings_text": "<b>Settings</b>\nLanguage: {language}\nCountry: {country}\nPreferred currency: {currency}\nTimezone: {timezone}\n\nStore prices always retain their original currency.",
    "country": "Country",
    "currency": "Currency",
    "timezone": "Timezone",
    "country_prompt": "Send a two-letter country code, e.g. BE or DE.",
    "currency_prompt": "Send a three-letter currency code, e.g. EUR or USD.",
    "timezone_prompt": "Send an IANA timezone, e.g. Europe/Brussels.",
    "settings_saved": "Settings saved.",
    "cancelled": "Cancelled.",
    "help": "Send a product URL to view its price. Tap Track to get alerts.\n\n/track — add a link\n/search — find products\n/my — manage tracking\n/history — price statistics\n/settings — language and region\n/plans — plans\n/paysupport — support\n/cancel — cancel the current action",
    "support": "Payment support\n{contact}",
    "invalid_url": "Use a valid HTTPS product link from a supported store.",
    "variant_required": "Choose a size or configuration on the store's page, then send the link with the selected options.",
    "choose_variant": "<b>{title}</b>\nChoose the size/color to track:",
    "variant_expired": "This selection has expired. Send the product link again.",
    "unsupported_product": "This product type cannot be tracked yet. Try a product with a fixed price and configuration.",
    "unsupported_store": "This store is not supported or enabled yet.",
    "provider_unavailable": "The store is temporarily unavailable. Please try again later.",
    "not_found": "This product or tracker is no longer available.",
    "rate_limit": "Please wait a minute before searching or adding another link.",
    "subscription_limit": "You’ve reached your tracking limit. Remove a product to add another.",
    "invalid_target": "Enter a positive amount, or a percentage between 0 and 100, e.g. 79.99 or 10%.",
    "invalid_input": "Please check the value and try again.",
    "unexpected_error": "Something went wrong. Please try again later.",
    "feature_unavailable": "This feature is not available yet.",
    "private_only": "Please open a private chat with PriceHunter.",
    "price_drop": "Price dropped",
    "target_reached": "Target reached",
    "back_in_stock": "Back in stock",
    "historical_low": "New historical low",
    "notification": "🔔 <b>{event}</b>\n{title}\n<b>{price}</b>",
}

RU = {
    "service_unavailable": "Сервис временно недоступен. Попробуйте позже.",
    "language": "🌐 Язык",
    "stores_hint": "/stores — доступные магазины",
    "stores_list": "<b>Доступные магазины</b>\n{stores}\n\nПришлите ссылку на товар или используйте /search. Цены показываются в валюте магазина. Условия доставки зависят от магазина.",
    "stores_empty": "Реальные магазины пока не подключены. Демо-каталог может быть доступен.",
    "start": "Добро пожаловать в <b>PriceHunter</b>.\nПришлите ссылку на товар — я буду следить за ценой.\n\nДемо:\nhttps://mock.pricehunter.test/products/headphones",
    "start_real": "Добро пожаловать в <b>PriceHunter</b>.\nПришлите ссылку на товар из поддерживаемого магазина — я буду следить за ценой.",
    "search": "🔎 Найти товар",
    "my": "📦 Мои товары",
    "plans": "⭐ Тарифы",
    "settings": "⚙️ Настройки",
    "track": "🔔 Следить за ценой",
    "target": "🎯 Задать цену",
    "history": "📊 История",
    "open_store": "🛒 В магазин",
    "pause": "Пауза",
    "resume": "Продолжить",
    "delete": "Не отслеживать",
    "back": "← Назад",
    "next": "Далее →",
    "search_prompt": "Что ищем? Пришлите название товара.",
    "url_prompt": "Пришлите ссылку на товар из поддерживаемого магазина.",
    "target_prompt": "Пришлите целевую цену в {currency} или процент снижения, например 10%.\n/cancel — отмена.",
    "target_saved": "Цель сохранена: {target}",
    "tracked": "Слежу за ценой.",
    "deleted": "Отслеживание остановлено.",
    "paused": "Отслеживание на паузе.",
    "resumed": "Отслеживание возобновлено.",
    "empty": "Пока нет товаров. Пришлите ссылку, чтобы начать.",
    "card": "<b>{title}</b>\n{store}\nСейчас: <b>{price}</b>\nМинимум: {minimum}\n{availability}",
    "original": "\nЦена без скидки: {price}",
    "tracker_card": "<b>{title}</b>\nСейчас: {price}\nС начала отслеживания: {change}%\nЦель: {target}\n{status}",
    "active": "Отслеживается",
    "inactive": "На паузе",
    "no_target": "Не задана",
    "in_stock": "В наличии",
    "out_of_stock": "Нет в наличии",
    "unknown": "Наличие неизвестно",
    "history_card": "<b>История цены</b>\nСейчас: {current}\nМинимум: {minimum}\nМаксимум: {maximum}\nСредняя: {average}\nС начала отслеживания: {change}\nНаблюдений: {count}",
    "history_prompt": "Нажмите «История» у отслеживаемого товара.",
    "search_empty": "Товары не найдены. Попробуйте другое название.",
    "search_partial": "Часть магазинов временно недоступна. Показываю доступные результаты.",
    "plans_text": "<b>Тарифы PriceHunter</b>\nFree: {free} товаров\nPro: {pro} товаров\nPower: {power} товаров\n\nПлатные подписки появятся позже. Эта версия не принимает оплату.",
    "settings_text": "<b>Настройки</b>\nЯзык: {language}\nСтрана: {country}\nВалюта: {currency}\nЧасовой пояс: {timezone}\n\nЦены магазинов всегда показываются в исходной валюте.",
    "country": "Страна",
    "currency": "Валюта",
    "timezone": "Часовой пояс",
    "country_prompt": "Пришлите код страны из двух букв, например BE или DE.",
    "currency_prompt": "Пришлите код валюты из трёх букв, например EUR или USD.",
    "timezone_prompt": "Пришлите часовой пояс IANA, например Europe/Brussels.",
    "settings_saved": "Настройки сохранены.",
    "cancelled": "Отменено.",
    "help": "Пришлите ссылку на товар и нажмите «Следить за ценой».\n\n/track — добавить ссылку\n/search — найти товар\n/my — управлять товарами\n/history — статистика цен\n/settings — язык и регион\n/plans — тарифы\n/paysupport — поддержка\n/cancel — отменить действие",
    "support": "Поддержка по оплате\n{contact}",
    "invalid_url": "Нужна корректная HTTPS-ссылка на товар из поддерживаемого магазина.",
    "variant_required": "Выберите размер или комплектацию на сайте магазина и пришлите ссылку с выбранными параметрами.",
    "choose_variant": "<b>{title}</b>\nВыберите размер/цвет для отслеживания:",
    "variant_expired": "Этот выбор устарел. Пришлите ссылку на товар ещё раз.",
    "unsupported_product": "Этот тип товара пока нельзя отслеживать. Попробуйте товар с фиксированной ценой и комплектацией.",
    "unsupported_store": "Этот магазин пока не поддерживается или отключён.",
    "provider_unavailable": "Магазин временно недоступен. Попробуйте позже.",
    "not_found": "Этот товар или отслеживание больше не доступны.",
    "rate_limit": "Подождите минуту перед следующим поиском или добавлением ссылки.",
    "subscription_limit": "Достигнут лимит товаров. Удалите один, чтобы добавить новый.",
    "invalid_target": "Введите положительную цену или процент от 0 до 100, например 79,99 или 10%.",
    "invalid_input": "Проверьте значение и попробуйте снова.",
    "unexpected_error": "Произошла ошибка. Попробуйте позже.",
    "feature_unavailable": "Эта функция пока недоступна.",
    "private_only": "Откройте личный чат с PriceHunter.",
    "price_drop": "Цена снизилась",
    "target_reached": "Цель достигнута",
    "back_in_stock": "Снова в наличии",
    "historical_low": "Новая минимальная цена",
    "notification": "🔔 <b>{event}</b>\n{title}\n<b>{price}</b>",
}


EN.update(
    {
        "plans_heading": "<b>PriceHunter plans</b>\nCurrent: {plan}",
        "plan_details": "<b>{plan} — {price} ⭐ / 30 days</b>\n{trackers} trackers · checks every {hours} h\nSearch: {search}/day, up to {results} results\nHistory: {history} days\n{features}",
        "billing_terms": "Basic price-drop alerts are included in every plan. Paid plans renew every 30 days. For an upgrade, cancel the old renewal first; the new period starts immediately at full price, without proration. Previous paid access remains.",
        "billing_disabled": "Checkout is currently disabled. No payment can be made here yet.",
        "upgrade_pro": "⭐ Upgrade to Pro",
        "upgrade_power": "🚀 Upgrade to Power",
        "my_subscription": "📋 My subscription",
        "pay_stars": "Pay with Telegram Stars",
        "invoice_title": "PriceHunter {plan}",
        "invoice_description": "{plan} subscription for 30 days. Recurring Telegram Stars payment. Cancel future renewal in /subscription. Payment support: /paysupport.",
        "checkout_ready": "{plan}: {price} ⭐ every 30 days. Access starts after payment. This purchase does not credit unused time on an earlier plan. Open the invoice to review and pay.",
        "subscription_details": "<b>Subscription: {plan}</b>\nStatus: {status}\nPaid access until: {until}\nRenewal: {renewal}\nTrackers stored: {count} · scheduled: {active}/{limit}\nTrackers beyond the current limit stay saved; the oldest enabled trackers are scheduled.",
        "subscription_free": "Free",
        "subscription_active": "Active",
        "subscription_cancelled": "Renewal cancelled; paid access retained",
        "subscription_expired": "Paid subscription expired",
        "subscription_refunded": "Refunded",
        "renewal_unknown": "Not established",
        "renewal_last_enabled": "Enabled at last payment; check Telegram for changes made there",
        "renewal_cancelled": "Cancelled by the bot",
        "cancel_plan_renewal": "Cancel {plan} renewal",
        "cancel_renewal_confirm": "Stop future automatic charges? Your already-paid access will remain until its expiration date.",
        "confirm_cancel_renewal": "Confirm cancellation",
        "renewal_cancelled_confirmation": "Future renewal cancelled. Already-paid access is unchanged.",
        "payment_successful": "Payment received. {plan} is active until {until}. See /subscription for current access.",
        "payment_processing": "The payment update has been saved for processing. Check /subscription shortly; contact /paysupport if access is missing. Do not pay again.",
        "payment_rejected": "This invoice cannot be accepted. Check your subscription and open a new invoice from /plans. Payment support: /paysupport.",
        "billing_unavailable": "Checkout is temporarily unavailable. Please try again later or contact /paysupport.",
        "subscription_conflict": "You already have this plan or a higher one, or a payment is still being processed. Check /subscription.",
        "cancel_renewal_first": "Cancel the existing subscription’s renewal in /subscription before purchasing another plan. Your paid access will remain.",
        "billing_operation_pending": "The operation needs confirmation from Telegram. Contact /paysupport; do not repeat the payment.",
        "feature_requires_upgrade": "This feature requires a paid plan. See /plans.",
        "quota_paused": "Saved; outside the current plan’s active quota",
        "feature_target_price_alerts": "target alerts",
        "feature_historical_low_alerts": "historical-low alerts",
        "feature_back_in_stock_alerts": "restock alerts",
        "feature_comparison_search": "comparison search",
        "feature_history_access": "price history",
        "support": "<b>Payment and refund support</b>\n{contact}\nInclude the payment date, plan and Telegram receipt. Never send passwords or bot tokens. /subscription shows current access. Renewal can be cancelled there; refunds are handled by support.",
        "subscription_limit": "Your plan’s tracker limit is reached. Remove a saved tracker or choose a larger plan in /plans.",
    }
)
RU.update(
    {
        "plans_heading": "<b>Тарифы PriceHunter</b>\nТекущий: {plan}",
        "plan_details": "<b>{plan} — {price} ⭐ / 30 дней</b>\nТрекеров: {trackers} · проверка каждые {hours} ч\nПоиск: {search}/день, до {results} результатов\nИстория: {history} дней\n{features}",
        "billing_terms": "Уведомления о снижении цены есть во всех тарифах. Платные тарифы продлеваются каждые 30 дней. Для перехода отмените старое продление; новый период начнётся сразу, по полной цене, без перерасчёта. Ранее оплаченный доступ сохранится.",
        "billing_disabled": "Приём платежей сейчас выключен. Оплатить подписку пока нельзя.",
        "upgrade_pro": "⭐ Перейти на Pro",
        "upgrade_power": "🚀 Перейти на Power",
        "my_subscription": "📋 Моя подписка",
        "pay_stars": "Оплатить Telegram Stars",
        "invoice_title": "PriceHunter {plan}",
        "invoice_description": "Подписка {plan} на 30 дней с автоматическим продлением за Telegram Stars. Отмена продления: /subscription. Поддержка по оплате: /paysupport.",
        "checkout_ready": "{plan}: {price} ⭐ каждые 30 дней. Доступ включится после оплаты. Неиспользованный срок старого тарифа не вычитается из стоимости. Откройте счёт, чтобы проверить условия и оплатить.",
        "subscription_details": "<b>Подписка: {plan}</b>\nСтатус: {status}\nДоступ оплачен до: {until}\nПродление: {renewal}\nСохранено товаров: {count} · проверяется: {active}/{limit}\nТовары сверх лимита сохраняются; проверяются старейшие включённые трекеры.",
        "subscription_free": "Free",
        "subscription_active": "Активна",
        "subscription_cancelled": "Продление отменено, оплаченный доступ сохранён",
        "subscription_expired": "Платная подписка истекла",
        "subscription_refunded": "Оплата возвращена",
        "renewal_unknown": "Нет подтверждённых данных",
        "renewal_last_enabled": "Было включено при последней оплате; изменения в Telegram проверяйте там",
        "renewal_cancelled": "Отменено через бота",
        "cancel_plan_renewal": "Отменить продление {plan}",
        "cancel_renewal_confirm": "Остановить будущие автоматические списания? Уже оплаченный доступ сохранится до конца срока.",
        "confirm_cancel_renewal": "Подтвердить отмену",
        "renewal_cancelled_confirmation": "Будущее продление отменено. Уже оплаченный доступ сохранён.",
        "payment_successful": "Оплата получена. Тариф {plan} активен до {until}. Текущий доступ — /subscription.",
        "payment_processing": "Событие оплаты сохранено для обработки. Проверьте /subscription чуть позже; если доступа нет, обратитесь в /paysupport. Повторно платить не нужно.",
        "payment_rejected": "Этот счёт нельзя принять. Проверьте подписку и откройте новый счёт через /plans. Поддержка: /paysupport.",
        "billing_unavailable": "Оплата временно недоступна. Попробуйте позже или обратитесь в /paysupport.",
        "subscription_conflict": "У вас уже есть этот тариф или более высокий, либо платёж ещё обрабатывается. Проверьте /subscription.",
        "cancel_renewal_first": "Перед покупкой другого тарифа отмените текущее продление в /subscription. Уже оплаченный доступ сохранится.",
        "billing_operation_pending": "Операция требует подтверждения Telegram. Обратитесь в /paysupport; повторно оплачивать не нужно.",
        "feature_requires_upgrade": "Эта функция доступна на платном тарифе. Подробности — /plans.",
        "quota_paused": "Сохранён; превышен лимит активных трекеров тарифа",
        "feature_target_price_alerts": "целевая цена",
        "feature_historical_low_alerts": "исторический минимум",
        "feature_back_in_stock_alerts": "возврат в продажу",
        "feature_comparison_search": "сравнение магазинов",
        "feature_history_access": "история цен",
        "support": "<b>Поддержка по оплате и возвратам</b>\n{contact}\nУкажите дату оплаты, тариф и чек Telegram. Пароли и токены присылать не нужно. Текущий доступ — /subscription. Там же можно отменить продление; возврат оформляется через поддержку.",
        "subscription_limit": "Достигнут лимит тарифа. Удалите сохранённый товар или выберите больший тариф в /plans.",
    }
)
EN["help"] += "\n/subscription — subscription and renewal"
RU["help"] += "\n/subscription — подписка и продление"

for _code, _catalog in (("en", EN), ("ru", RU)):
    _catalog.update(
        json.loads(
            files("pricehunter.localization")
            .joinpath(f"locales/{_code}_comparison.json")
            .read_text("utf-8")
        )
    )
CATALOGS = {"en": EN, "ru": RU}
for _code in ("fr", "de", "es", "it", "pl"):
    CATALOGS[_code] = cast(
        dict[str, str],
        json.loads(
            files("pricehunter.localization").joinpath(f"locales/{_code}.json").read_text("utf-8")
        ),
    )

for _code, _catalog in CATALOGS.items():
    _catalog.update(
        json.loads(
            files("pricehunter.localization")
            .joinpath(f"locales/{_code}_delivery.json")
            .read_text("utf-8")
        )
    )


def tr(locale: str, key: str, **values: object) -> str:
    messages = CATALOGS[normalize_language(locale)]
    template = messages[key] if key in messages else EN[key]
    return template.format(**{k: escape(str(v)) for k, v in values.items()})


def money(amount: Decimal, currency: str, language: str) -> str:
    return format_currency(amount, currency, locale=NUMBER_LOCALES[normalize_language(language)])
