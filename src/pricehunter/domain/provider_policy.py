from pydantic import BaseModel, ConfigDict, Field, model_validator

from pricehunter.domain.errors import ProviderPolicyError


class ProviderDataPolicy(BaseModel):
    """Deployment-reviewed permissions, never inferred from credentials or API access."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    reviewed: bool = False
    review_reference: str = Field(default="", max_length=500)
    catalog_persistence_allowed: bool = False
    price_history_allowed: bool = False
    tracking_allowed: bool = False
    refresh_allowed: bool = False
    affiliate_allowed: bool = False
    affiliate_required: bool = False
    max_cache_seconds: int = Field(default=3600, ge=60, le=31536000)
    display_attribution_required: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def consistent(self) -> "ProviderDataPolicy":
        permissions = (
            self.catalog_persistence_allowed,
            self.price_history_allowed,
            self.tracking_allowed,
            self.refresh_allowed,
            self.affiliate_allowed,
        )
        if any(permissions) and not (self.reviewed and self.review_reference.strip()):
            raise ValueError("Provider permissions require a recorded policy review")
        if (
            self.tracking_allowed or self.price_history_allowed
        ) and not self.catalog_persistence_allowed:
            raise ValueError("Tracking/history require catalog persistence permission")
        if self.affiliate_required and not self.affiliate_allowed:
            raise ValueError("Affiliate-required providers need affiliate permission")
        if self.tracking_allowed and not self.price_history_allowed:
            raise ValueError(
                "Persistent watches retain price baselines and require history permission"
            )
        return self

    def require(self, permission: str) -> None:
        if not self.reviewed or not getattr(self, permission, False):
            raise ProviderPolicyError()


SYNTHETIC_POLICY = ProviderDataPolicy(
    reviewed=True,
    review_reference="Synthetic PriceHunter fixture data",
    catalog_persistence_allowed=True,
    price_history_allowed=True,
    tracking_allowed=True,
    refresh_allowed=True,
    affiliate_allowed=True,
    max_cache_seconds=31536000,
)
