"""Named abstract contracts; none is registered before authorized feed onboarding."""

from pricehunter.providers.affiliate.base import AffiliateProvider


class RakutenProvider(AffiliateProvider):
    name = "rakuten"


class CJProvider(AffiliateProvider):
    name = "cj"


class AwinProvider(AffiliateProvider):
    name = "awin"
