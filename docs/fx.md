# ECB reference conversion

M4A displays optional approximate prices in the user's preferred_currency. Native
retailer price/currency are unchanged and remain authoritative. Best-price groups,
alerts, historical minima, subscriptions and payments never use converted values.
No cross-currency delivered-price winner is claimed.

Source: [official ECB daily XML](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml).
The ECB describes these as informational euro reference rates, normally published
on working days; see [reference-rate information](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html).
They are not card, checkout or settlement rates.

```dotenv
FX_ENABLED=true
FX_REFRESH_SECONDS=21600
FX_MAX_AGE_DAYS=7
```

With FX disabled (default), comparisons continue to use native prices alone. With
it enabled, the existing ARQ worker performs one shared bounded fetch, protected by
a Redis lease/cadence key. The maintenance task runs every 15 minutes and fetches
only when due (default six hours). Failures back off from five minutes to one hour,
retain the last committed snapshot and do not break native comparisons. No user
request/search performs ECB network I/O.

FxRate stores EUR base, quote currency, Decimal rate, effective_date, fetched_at and
source. A complete publication is persisted atomically. XML responses have bounded
size and disabled DTD/entity expansion. Invalid, non-finite, zero, negative, duplicate
or future-dated rates are rejected. Fixtures contain synthetic values, not live quotes.

Convert via `amount * EUR_quote_rate / EUR_base_rate`, treating EUR as 1. Calculations
use Decimal and round only the displayed amount to the target currency's minor-unit
precision. Unsupported pairs omit conversion. The latest working-day snapshot is
valid on weekends/holidays until FX_MAX_AGE_DAYS; refetching it does not reset its
effective date. Beyond the age threshold, approximate prices disappear.

Comparison offer DTO fields: reference_price, reference_currency, fx_rate,
fx_effective_date, fx_fetched_at and fx_source. ComparisonProduct also exposes
preferred_currency. Same-currency amounts are not redundantly relabeled as FX.
Telegram shows `≈` with a localized reference label, source and effective date;
the exact retailer amount remains visible above it. API consumers must keep that
distinction and must not use the optional display amount for payment calculations.

Tests cover EUR/USD, USD/EUR, GBP/PLN through EUR, same currency, unsupported currency,
weekend reuse, staleness, Decimal rounding, invalid XML/rates, shared fetching,
failure backoff and persisted/native-price separation without network dependency.
