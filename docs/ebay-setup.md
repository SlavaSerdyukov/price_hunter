# Подключение eBay: Бельгия и ЕС

Telegram уже проверен на реальных сообщениях. Для реальных цен нужен отдельный доступ
к eBay Browse API. Код работает с Production (`api.ebay.com`); Sandbox-ключи здесь
не подходят. Ни ключ Telegram, ни обычная учётная запись покупателя этот доступ не заменяют.

## 1. Получить ключи

1. Зарегистрируйтесь в [eBay Developers Program](https://developer.ebay.com/develop/guides/sell/get-started-with-ebay-apis).
2. После активации учётной записи откройте [Application Keys](https://developer.ebay.com/my/keys).
3. Укажите имя приложения `PriceHunter`. В разделе **Production** выберите **Create a keyset**.
4. Сохраните **App ID (Client ID)** и **Cert ID (Client Secret)** в локальный `.env`.
   Dev ID и пользовательский User Token нашему адаптеру не нужны: он получает
   application access token через OAuth `client_credentials`.

### Если Production keyset отключён

До первого Production-вызова eBay требует настроить Marketplace Account Deletion/
Closure Notifications либо получить допустимое исключение. На странице ключей
проверьте сообщение **Your Keyset is currently disabled** и ссылку на его активацию.
Это отдельный шаг от проверки учётной записи и выдачи разрешения Browse API.

Официальная [инструкция по активации и уведомлениям](https://developer.ebay.com/develop/guides/sell/marketplace-user-account-deletion).
PriceHunter сохраняет данные объявлений и историю цен, включая имя продавца, если API
его возвращает. Нельзя заявлять `Not persisting eBay data` без соответствующего
основания. Если требуется подписка, для неё нужен публичный HTTPS endpoint с
проверкой уведомлений и обработкой удаления связанных данных; этот endpoint пока
не реализован. Само копирование App ID / Cert ID отключённый keyset не активирует.

Официальные инструкции: [создание keyset](https://edp.ebay.com/api-docs/static/gs_create-the-ebay-api-keysets.html),
[OAuth](https://developer.ebay.com/develop/guides/sell/authorization).

## 2. Получить доступ к Browse API

Наличие Production-ключей ещё не подтверждает доступ к Buy/Browse API.
Пройдите [процедуру допуска eBay](https://developer.ebay.com/api-docs/buy/buy-requirements.html):
регистрация в eBay Partner Network, заявка с описанием бизнес-модели и интерфейса,
далее рассмотрение через Developer Support и необходимые соглашения.
eBay может потребовать демонстрацию в Sandbox и
[Application Growth Check](https://developer.ebay.com/grow/application-growth-check).
Одобрение не гарантируется; текущий адаптер поддерживает только Production.

Текст для описания проекта — проверьте и адаптируйте перед отправкой:

> PriceHunter is a Telegram price-tracking application for Belgium and EU marketplaces.
> Users submit eBay listing URLs or search for products, save selected listings, and
> receive notifications when prices fall or reach a chosen target. The application
> uses the Browse API for product search and item retrieval, links users to eBay to
> purchase, and does not place orders. Price observations are stored to provide price
> history. We request confirmation that this storage, history display and notification
> use case is permitted under the applicable API terms and our approved access.

Сообщайте eBay реальные параметры запуска и ожидаемую нагрузку. При стандартных
настройках Free проверяет один уникальный товар раз в 12 часов; пользователи, которые
следят за одним предложением, используют общий запрос. Поиск и первичное добавление
также расходуют квоту. Партнёрская атрибуция пока не реализована — если она будет
условием доступа, её нужно будет добавить по требованиям eBay.

## 3. Настроить PriceHunter

В существующем `.env` заполните ключи, не заменяя остальное содержимое файла:

```dotenv
EBAY_CLIENT_ID=ваш_Production_App_ID
EBAY_CLIENT_SECRET=ваш_Production_Cert_ID
EBAY_ENABLED=true
EBAY_MARKETPLACES=["BE","DE","FR","NL","IT","ES","AT","IE","PL"]
EBAY_BELGIUM_LOCALE=nl-BE
```

Для франкоязычного бельгийского поиска используйте `EBAY_BELGIUM_LOCALE=fr-BE`.
Ссылка `befr.ebay.be` сама выбирает французский язык, `benl.ebay.be` — нидерландский.
Также поддерживаются `ebay.be`, `ebay.com.be`, `benl.ebay.be`, `befr.ebay.be`
и их варианты с `www`, включая ссылки, которые возвращает сам Browse API.
Для Бельгии используется `EBAY_BE`, как требует
[документация eBay](https://developer.ebay.com/api-docs/buy/ref-marketplace-supported.html).

Первая страна в `EBAY_MARKETPLACES` — рынок поиска по умолчанию. В боте `/settings`
позволяет выбрать другой. Поиск идёт на одной выбранной площадке; общий поиск по всему
ЕС и расчёт доставки в Бельгию пока не реализованы. Площадка товара по ссылке выбирается
из её домена. Сохраняется исходная валюта продавца: при наличии `convertedFromValue`
и `convertedFromCurrency` колебания конвертации eBay не считаются изменением цены.
Поиск ограничен предложениями с фиксированной ценой. Короткие ссылки `ebay.us`
не поддерживаются: используйте полный HTTPS-адрес товара с `/itm/`.

### Ссылка на объявление с несколькими вариантами

Обычная ссылка на такое объявление может вызвать ответ Browse `400 / 11006`:
это требование получить варианты через `getItemsByItemGroup`. Адаптер обрабатывает
его и показывает в Telegram кнопки выбора размера/цвета/комплектации (до 100 вариантов,
по восемь на страницу). До выбора варианта предложение и его цена не сохраняются.
Ссылка с `?var=…` сразу выбирает соответствующий вариант. Рекламные параметры
`itmprp`, `itmmeta` не влияют на выбор товара.

Проверенный пример:
[BRÜTTING LYDIARD](https://www.ebay.de/itm/167526377039) — 11 размеров;
вариант UK 5,5 на проверке 2026-09-19 стоил 239,95 EUR.
После выбора размер включается в название карточки, а обновление идёт по точному
ID варианта. Общий диапазон размеров продавца не подставляется вместо выбранного размера.
REST API для ссылки без выбора возвращает `variant_required`.

Ссылка из текста ошибки eBay не открывается: запрос вариантов строится по проверенному
ID объявления и фиксированному API endpoint. Из тела ошибки сохраняются только числовые
коды; сообщения и параметры ответа не попадают в исключения.

## 4. Проверить реальный доступ

Для запуска приложения M4A также требует проверенную политику `ebay` в
`PROVIDER_DATA_POLICIES`. Доступ к Browse API и участие в EPN не подтверждают права
на долговременную историю цен. См. [политики источников](international-commerce.md)
и [настройку EPN](affiliate-links.md). Диагностика ниже проверяет технический доступ.

Из каталога проекта:

```bash
uv run python -m pricehunter.apps.check_ebay --country BE --query headphones
# Или: make check-ebay
```

Для конкретного товара добавьте `--url 'https://www.ebay.de/itm/РЕАЛЬНЫЙ_ID'`.
Команда проверит OAuth, поиск, получение товара по ссылке и повторное чтение цены.
Она не создаёт пользователей/трекеры и не отправляет сообщений в Telegram. Секреты
читаются из `.env`; их значения и тела ошибок API не выводятся.
Проверку можно выполнить при `EBAY_ENABLED=false` перед включением интеграции в боте.

Вариант через Docker, с текущим `.env` и свежим образом:

```bash
docker compose --profile app build
docker compose --profile app run --rm --no-deps api python -m pricehunter.apps.check_ebay --country BE
```

Код завершения `0` означает успешный поиск, lookup и refresh. Код `2` — отсутствуют
ключи или выбранный рынок не включён. Код `1` — запрос/данные требуют проверки:

| Результат | Следующий шаг |
| --- | --- |
| HTTP 400/401 | Проверить пару Production App ID / Cert ID и параметры запроса |
| OAuth failed: HTTP 401 | Сверить App ID и Cert ID одного активного keyset; проверить статус Disabled и Marketplace Account Deletion в кабинете |
| HTTP 403 | Проверить разрешение Browse API для этого Production keyset |
| HTTP 429 | Дождаться восстановления квоты и проверить лимиты в кабинете eBay |
| Listing not found | Выбрать действующее объявление; для варианта сохранить `?var=…` |
| No products matched | Изменить `--query` или передать конкретный `--url` |
| Network / unusable response | Проверить соединение и статус eBay, затем повторить |

## 5. Включить в работающем боте

После успешной проверки оставьте `EBAY_ENABLED=true` и пересоздайте Python-сервисы:

```bash
docker compose --profile app up -d --build --force-recreate api bot worker
```

Обычный `docker compose restart` не подхватывает изменённые переменные `.env`.
Таблицы и история сохранятся в существующей базе.

В боте отправьте реальную ссылку eBay, проверьте цену и валюту, затем нажмите
«Следить за ценой». `/search headphones` проверяет поиск, `/my` — сохранённый трекер.
По умолчанию реальные товары Free проверяются раз в 12 часов, а не раз в минуту,
как в демо. Уведомление появится при подходящем изменении реальной цены.

Mock-каталог можно оставить для тестов. Перед `MOCK_PROVIDER_ENABLED=false` удалите
или поставьте на паузу свои демо-трекеры, чтобы worker не пытался обновлять их через
отключённый провайдер. После изменения флага снова пересоздайте сервисы.

Реальная проверка 2026-09-19 прошла: Production OAuth, поиск BE (10 результатов),
получение карточки и обновление объявления. Исправлена поддержка `www.benl.ebay.be`
и `www.befr.ebay.be`. eBay включён в локальном боте. Предыдущая ошибка
`401 invalid_client` больше не воспроизводится; значения ключей не выводились.
