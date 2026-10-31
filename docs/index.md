# Documentation index

PriceHunter is a completed engineering portfolio snapshot. Start with the
[README](../README.md) and independent [case study](portfolio-case-study.md).
Operational guides describe implemented tools and prerequisites; they are not launch
commitments. Dated design/verification records preserve what was known at each stage.

## Architecture / domain

- [Architecture decisions](architecture.md)
- [Product identity and comparison](comparison.md)
- [Canonical Merchant / Store identity](merchant-identity.md)
- [Discovery, freshness and watches](discovery.md)
- [Subscription and billing ledger](billing.md)

## Commerce / comparison

- [Markets and provider data policies](international-commerce.md)
- [Market and snapshot correctness](m4a1-market-correctness.md)
- [Commerce feed ingestion/publication](commerce-feeds.md)
- [Feed validation and quality gates](feed-quality.md)
- [Coverage, work bounds and diagnostics](coverage-engine.md)
- [Merchant programs and rights](merchant-programs.md)
- [Historical pilot procedure](merchant-pilot.md)
- [Delivery scope, evidence and ranking](delivery-context.md)
- [Affiliate/outbound source provenance](affiliate-links.md)
- [Reference FX](fx.md)

## Reliability / operations

- [Local setup, API, configuration and tests](local-development.md)
- [Runtime contracts and resource bounds](runtime-hardening.md)
- [Preserved beta operational procedure](beta-operations.md)
- [PostgreSQL backup / disaster recovery](disaster-recovery.md)
- [Authenticated load rehearsal](load-rehearsal.md)
- [Sanitized local measurements](m5c-rehearsal-evidence.json)
- [Current baseline and evidence audit](portfolio-audit.md)

## Integrations

These guides record adapter configuration and permission boundaries. They do not imply
present account access, active merchants or production verification.

- [eBay Browse / EU marketplaces](ebay-setup.md)
- [Public WooCommerce catalogs](public-stores.md)
- [Amazon Creators, approval gated](amazon-setup.md)
- [Rakuten Product Search](rakuten-setup.md)
- [Awin feeds](awin-setup.md)
- [TradeDoubler feeds](tradedoubler-setup.md)
- [CJ feeds](cj-setup.md)

## Historical design records

- [Historical roadmap](../ROADMAP.md), [verification evidence](verification.md)
- [Billing design](m2-design.md)
- [Comparison core](m3a-design.md), [shared discovery](m3b-design.md)
- [International commerce](m4a-design.md), [feed ingestion](m4b-design.md)
- [Coverage engine](m4c-design.md), [canonical merchants](m4d-design.md)
- [Publication safety](m4e-design.md), [runtime hardening](m5a-design.md)
- [Delivery context](m5b-design.md), [recovery/load](m5c-design.md)

## Portfolio and release

- [Engineering case study](portfolio-case-study.md)
- [Changelog and owner-controlled post-merge release steps](../CHANGELOG.md)
