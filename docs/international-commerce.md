# International commerce (M4A)

M4A extends the existing comparison/discovery engine. It does not change Stars
billing, deterministic matching or authoritative native-currency price ranking.
Implementation decisions and the pre-change baseline are in [m4a-design.md](m4a-design.md).

## Durable market context

`ProductWatch.market_country` is required and unique together with user, canonical
product and currency. One user can watch the same product in BE/EUR and DE/EUR.
Creation uses an explicit `market_country`, otherwise the saved account country,
otherwise returns `country_required` (HTTP 400). Changing account country never
rewrites a watch or its discovery targets. Telegram offers a localized country
picker; settings accept other valid country codes too. BE, DE, FR, NL, IT, ES, AT,
IE, PL, GB, US and CA are offered as buttons. Provider coverage remains independent
of this list: choosing a country does not create API coverage for that market.

```json
{"product_id":"<canonical-product-uuid>","market_country":"DE","currency":"EUR"}
```

Send this to authenticated `POST /api/v1/product-watches`; watch responses include
the persisted market. Market cannot be patched: create a separate watch deliberately.
Exact-offer trackers continue to represent one merchant listing.

Market identifies the retailer catalog to query. It does **not** establish delivery
eligibility. Comparison/history still group all known matching offers by native
currency, rather than claiming a cheapest deliverable offer in that country.
StoreOffer has optional delivery_country, postal_code, shipping_price and tax
columns for M4B; M4A does not populate an invented delivered total.

## Provider data policy

`PROVIDER_DATA_POLICIES` is deployment JSON keyed by exact provider name, for example
`ebay`, `rakuten` or `woocommerce_pine64_eu`. Only synthetic `mock` data has an
automatic policy. A real integration must have a recorded review before activation.
**Existing real-provider deployments need this configuration before restarting M4A.**
The environment example starts real stores disabled. Existing database contents
are preserved by migration; an absent review does not silently authorize their use.

Start from this closed policy and fill it from your actual agreement/review:

```json
{
  "rakuten": {
    "reviewed": false,
    "review_reference": "",
    "catalog_persistence_allowed": false,
    "price_history_allowed": false,
    "tracking_allowed": false,
    "refresh_allowed": false,
    "affiliate_allowed": false,
    "affiliate_required": false,
    "max_cache_seconds": 3600,
    "display_attribution_required": null
  }
}
```

`review_reference` should identify the dated agreement/review; it is not a checkbox
that grants rights. Do not copy synthetic test policies into production. Public API
availability, OAuth approval and EPN/Rakuten membership are not proof of permission
for permanent price history or Telegram alerts.

- Catalog permission gates activation and persistence.
- History permission controls observations and price-history use.
- Tracking requires catalog and history permission because this implementation
  retains baselines and price aggregates. Tracking without history permission is
  deliberately rejected, rather than pretending those stored baselines are transient.
- Refresh also needs permission and the adapter's real REFRESH capability.
- Affiliate permission and active network configuration control outbound selection.
- Attribution text appears on comparison offers when configured.
- Cache lifetime caps display and recommendation freshness. A periodic bounded job
  evicts expired search-only offers and orphan catalog records. Old tracked/history
  references are protected: changing a legacy contract requires an operator-led
  export/retention decision, not silent deletion of existing users' records.

Rakuten can run as reviewed search-only catalog data with history/tracking/refresh
disabled. Such offers do not create price observations, watches or automatic item
refreshes. Their unknown stock cannot win the current available-price ranking.
Unreviewed legacy data is hidden from current comparison and refresh scheduling;
the migration does not erase historical data. Review retention before deployment.

Current official references:
[eBay API license](https://www.developer.ebay.com/join/api-license-agreement),
[Buy requirements](https://developer.ebay.com/api-docs/buy/buy-requirements.html),
[Rakuten Product Search](https://developers.rakutenadvertising.com/guides/product_search).
These describe access/use conditions; the application's own commercial permissions
must be reviewed separately. Best Buy remains disabled: no assumption about its
long-term caching/history rights is made.

## Upgrade and operation

1. Back up the database and stop bot/API/worker before migration.
2. Configure reviewed policies for the real providers you intend to activate.
3. `uv sync --frozen` and `uv run alembic upgrade head`.
4. Run `uv run alembic check`, then restart services with the same settings.

Migration `2c125500eaf6` backfills **existing** watches from User.country_code or BE.
BE is a legacy migration rule only; there is no database default for new watches.
It makes direct_url nullable and adds merchant ID, affiliate metadata, delivery
placeholders, OutboundClick and FxRate. All M3B and billing records are preserved.
Downgrade refuses affiliate-only offers, multiple-market watches, click or FX data
until exported/reconciled; it never fabricates missing direct URLs.

The existing migration-verifier command now covers M3A → real M3B → M4A:

```bash
uv run python scripts/verify_m3b_migration.py
```

It creates/drops a uniquely named scratch database using `TEST_DATABASE_URL`, which
must point at a dedicated database ending `_test`. The application database is not used.

See [Rakuten setup](rakuten-setup.md), [affiliate links](affiliate-links.md),
[reference FX](fx.md), and the executed [verification record](verification.md).

## M4B boundary

Next scope: confirmed delivery country/postal input, documented shipping/tax fields,
availability by destination, explicit missing-cost semantics and a separately
labeled delivered-cost comparison when every required component is known. Add
reviewed Awin/CJ feed onboarding with identifier/variant/stock provenance and contract
retention. Keep native groups, deterministic matching and billing unchanged. Automatic
product merge, frontend/Mini App/mobile, new payment methods and ML matching remain separate.
