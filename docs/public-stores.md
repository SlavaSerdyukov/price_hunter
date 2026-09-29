# Реальные магазины без ключей

Подключены четыре каталога электроники и одежды через публичный WooCommerce Store API. Они
работают независимо от ожидаемого одобрения eBay. Аккаунт магазина, API-ключи
и авторизация покупателя не нужны.

В M4A перед включением каждого реального источника нужна проверенная конфигурация
`PROVIDER_DATA_POLICIES`: доступность публичного API сама по себе не разрешает
хранение истории и отслеживание цен. См. [политики и запуск](international-commerce.md).

| Магазин | Каталог | Валюта публичного API |
| --- | --- | --- |
| [PINE64 EU](https://pine64eu.com/) | Паяльники Pinecil, питание, платы, аксессуары | EUR |
| [RaspberryPi.dk](https://raspberrypi.dk/) | Raspberry Pi, дисплеи, накопители, аксессуары | DKK |
| [Hemptees](https://hemptees.be/) | Бельгийский магазин одежды: футболки, лонгсливы, бельё | EUR |
| [Western Shop Bruxelles](https://westernshop.be/) | Одежда, обувь и аксессуары в стиле western | EUR |

Это интеграция каталогов, а не рекомендация продавцов. Доступность доставки в Бельгию,
её стоимость и итоговые налоги проверяются в магазине. Параметр страны в боте
не меняет валюту и налоговый контекст этих каталогов. Исходная цена остаётся в валюте
магазина; необязательный [пересчёт ECB](fx.md) показывается только как ориентир.

## Попробовать в Telegram

1. Отправьте `/stores`, чтобы увидеть включённые реальные магазины.
2. Отправьте одну из ссылок ниже либо `/search pinecil` или `/search Touch Display`.
3. Проверьте цену/валюту/наличие и нажмите «Следить за ценой».
4. При необходимости задайте целевую цену. `/my` показывает сохранённые товары.

Проверенные ссылки:

- [PINEPOWER Desktop v2](https://pine64eu.com/product/pinepower-desktop-v2/).
- [Raspberry Pi Touch Display 2](https://raspberrypi.dk/produkt/officiel-raspberry-pi-10-touch-display-2/).
- [Raspberry Pi Flash Drive, 256 ГБ](https://raspberrypi.dk/produkt/raspberry-pi-flash-drive/?attribute_capacity=256GB).
- [Hemptees Short Sleeve Tee Max](https://hemptees.be/product/short-sleeve-tee-max/) — выбор размера и цвета в боте.
- [Western Shop Speed Shop](https://westernshop.be/produit/speed-shop/).

На проверке 2026-09-19 API возвращал 76,75 EUR для PINEPOWER (нет в наличии),
699 DKK для дисплея и 499 DKK для накопителя 256 ГБ (оба в наличии).
Это значения на момент проверки, не обещание текущей цены.

Цена реального магазина не меняется искусственно: уведомление приходит при
подходящем изменении цены или наличии. Free по умолчанию обновляет цену раз в
12 часов; минутный интервал применяется только к mock-каталогу.

## Варианты и ограничения

- Простые товары отслеживаются по точному ID магазина.
- Для товара с вариантами пришлите обычную ссылку: бот предложит размер/цвет кнопками,
  по восемь вариантов на странице. Показываются до 100 конкретных сочетаний;
  кнопки действуют 15 минут и только для текущего пользователя и выбора товара.
  Также поддержаны ссылки с `attribute_…` или `variation_id` с сайта магазина.
  REST API для ссылки без выбора возвращает `variant_required`.
  Минимальная цена всей линейки не используется как цена выбранной комплектации.
- После добавления обновления идут по ID выбранного варианта. Название карточки
  включает комплектацию, например `Capacity: 256GB`.
- Поиск возвращает до пяти предложений каждого магазина; для магазинов одежды
  проверяются и простые товары, и конкретные варианты. Для другого размера/цвета
  отправьте обычную ссылку на товар. Бот показывает первые пять найденных предложений.
- Товары с паролем, сгруппированные и требующие дополнительных опций не поддержаны.
- При предзаказе/неясном статусе показывается неизвестное наличие.
- API-цены читаются как целые минимальные единицы валюты, затем преобразуются в
  `Decimal` согласно `currency_minor_unit`. HTML-текст не используется для чтения цены.

## Конфигурация

```dotenv
WOOCOMMERCE_STORES=["pine64_eu","raspberrypi_dk","hemptees_be","westernshop_be"]
PROVIDER_RATE_LIMITS={"woocommerce_pine64_eu":10,"woocommerce_raspberrypi_dk":10,"woocommerce_hemptees_be":5,"woocommerce_westernshop_be":5}
```

Пустой список отключает эти адаптеры. Добавление произвольного домена через Telegram
или `.env` не поддерживается: домены и фиксированные API-маршруты заданы в коде.
Новые магазины сначала проверяются, затем добавляются в разрешённый список.

Лимит — 10 операций в минуту для электроники, 5 для одежды, общий через Redis.
Обычно операция делает один запрос; добавление варианта и поиск одежды — два. Планировщик
обновляет общее предложение один раз для всех подписчиков. HTTP-редиректы,
недопустимые адреса, слишком большие ответы и невалидные цены отклоняются.

После изменения `.env` нужно пересоздать Python-сервисы:

```bash
docker compose --profile app up -d --build api bot worker
```

## Проверка без отправки сообщений

```bash
uv run python -m pricehunter.apps.check_stores pine64_eu --query pinecil
uv run python -m pricehunter.apps.check_stores raspberrypi_dk --query 'Touch Display'
uv run python -m pricehunter.apps.check_stores hemptees_be --query 'Short Sleeve' --url 'https://hemptees.be/product/short-sleeve-tee-max/?variation_id=6263'
uv run python -m pricehunter.apps.check_stores westernshop_be --query 'Speed Shop'
```

Можно добавить `--url 'полная ссылка на товар'`. Команда проверяет поиск, разрешение
ссылки и повторное получение цены, не записывая данные в БД и не отправляя сообщений.
Для Docker замените `uv run` на `docker compose exec -T api`.

## Источники и проверка доступа

Проверены 2026-09-18/19:

- [WooCommerce Store API](https://developer.woocommerce.com/docs/apis/store-api/) — публичный API без ключей.
- [Products API](https://developer.woocommerce.com/docs/apis/store-api/resources-endpoints/products/) — поиск, ID, варианты и денежные единицы.
- [PINE64 EU robots.txt](https://pine64eu.com/robots.txt) и [условия магазина](https://pine64eu.com/refund_returns/).
- [RaspberryPi.dk robots.txt](https://raspberrypi.dk/robots.txt) и [условия магазина](https://raspberrypi.dk/handelsbetingelser/).
- [Hemptees robots.txt](https://hemptees.be/robots.txt) и [публичный каталог](https://hemptees.be/).
- [Western Shop robots.txt](https://westernshop.be/robots.txt) и [каталог](https://westernshop.be/).

На момент проверки robots.txt не запрещали используемые маршруты публичного каталога.
Не используются браузеры, CAPTCHA, личные кабинеты или обход ограничений доступа.
Сохранённые тестовые фикстуры содержат только необходимые публичные поля товаров;
автоматические тесты не обращаются к внешним магазинам.
