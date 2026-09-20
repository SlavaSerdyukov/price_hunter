# Amazon: подготовлено, ожидает доступа

Адаптер Creators API реализован и проверен на синтетических ответах по официальной
схеме. Реальные запросы Amazon не проверены: у оператора пока нет ни API-доступа,
ни отдельного согласования отслеживания цен. По умолчанию `AMAZON_ENABLED=false`.

## Что требуется до включения

1. Получить доступ к Amazon Associates и Creators API для нужных рынков, создать
   Client ID / Client Secret и Partner Tag каждого рынка. Следовать актуальной
   [инструкции Amazon](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/introduction).
   Регистрация сама по себе не гарантирует API-доступ.
2. Согласовать с Amazon назначение PriceHunter: отслеживание цен, хранение истории,
   отображение в Telegram и уведомления. Стандартные
   [Program Policies, пункт (y)](https://affiliate-program.amazon.com/help/operating/policies)
   требуют отдельного согласования для price tracking / price alerting.
3. Перед запуском проверить полученные условия на совместимость с хранением истории,
   сроком кэширования, изображениями, отметками времени, текстом уведомлений и
   партнёрскими ссылками. Текущий общий механизм истории хранит данные бессрочно;
   адаптер сам по себе не реализует индивидуальные лицензионные условия Amazon.
   При необходимости сначала изменить хранение и отображение согласно соглашению.

Флаг `AMAZON_PRICE_TRACKING_APPROVED` фиксирует выполненное согласование;
его установка не заменяет разрешение Amazon. Без флага приложение откажется
включать Amazon, даже если ключи уже заполнены.

## Конфигурация после согласования

Секреты вносить только в локальный `.env`, не в чат и не в Git:

```dotenv
AMAZON_ENABLED=true
AMAZON_PRICE_TRACKING_APPROVED=true
AMAZON_CREDENTIAL_VERSION=3.2
AMAZON_CREATOR_CLIENT_ID=
AMAZON_CREATOR_CLIENT_SECRET=
AMAZON_MARKETPLACES={"BE":{"partner_tag":"your-belgian-tag"},"DE":{"partner_tag":"your-german-tag"}}
```

Поддержаны BE, DE, FR, NL, IT, ES, IE, PL, SE, GB. Домены зафиксированы в коде.
В `AMAZON_MARKETPLACES` включать только рынки, на которые выдан доступ. Каждый рынок
использует свой `partner_tag`; `AMAZON_PARTNER_TAG` оставлен как fallback для одного
рынка. Версия credential должна соответствовать выданным ключам: 3.1 / 3.2 / 3.3
выбирает соответствующий Login with Amazon endpoint; для EU обычно 3.2.

```bash
uv run python -m pricehunter.apps.check_amazon --country BE --query headphones
docker compose --profile app up -d --build api bot worker
```

Диагностика проверяет поиск, получение товара и обновление по ASIN, не пишет в БД
и не отправляет сообщения. При отключённой интеграции завершается с кодом 2 без
сетевых запросов. Неуспешная проверка возвращает код 1; успешная — 0.

## Поведение адаптера

- Creators API: POST `catalog/v1/getItems` и `catalog/v1/searchItems`, OAuth LwA,
  кэш токена и одно обновление при 401. PA-API 5 не используется.
- Полные HTTPS-ссылки `/dp/ASIN`, `/gp/product/ASIN`, `/gp/aw/d/ASIN`; короткие
  ссылки `amzn.*` не раскрываются. ASIN и рынок ответа сверяются с запросом.
- Цена обычного нового featured/Buy Box предложения, в валюте соответствующего
  рынка. Продавец такого предложения может меняться. Доставка, персональная цена
  и итог для бельгийского адреса не вычисляются.
- Prime, подписки, специальные deals, MAP-скрытые и неоднозначные предложения
  пропускаются. Если подходящей цены нет, нулевая цена не записывается; обновление
  завершается ошибкой, а прежнее наблюдение сохраняется с прежним временем.
- Варианты сохраняются по конкретному ASIN; parent ASIN не заменяет выбранный размер.
- Все вызовы используют существующие лимиты, проверку публичных IP, ограничения
  размера ответа и времени. Ни HTML-парсинга, ни обхода CAPTCHA нет.

Официальные технические источники, проверены 2026-09-19:
[OAuth и HTTP](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/get-started/using-curl),
[GetItems](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/api-reference/operations/get-items),
[SearchItems](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/api-reference/operations/search-items),
[OffersV2](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/api-reference/resources/offersV2).
