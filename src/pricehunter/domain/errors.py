from dataclasses import dataclass


@dataclass(frozen=True)
class VariantOption:
    label: str
    url: str


class PriceHunterError(Exception):
    code = "unexpected_error"
    status_code = 400


class InvalidProductUrlError(PriceHunterError):
    code = "invalid_url"


class VariantSelectionRequiredError(PriceHunterError):
    code = "variant_required"

    def __init__(self, options: list[VariantOption] | None = None, title: str = "") -> None:
        super().__init__(self.code)
        self.options = options or []
        self.title = title


class UnsupportedProductError(PriceHunterError):
    code = "unsupported_product"


class UnsupportedStoreError(PriceHunterError):
    code = "unsupported_store"


class ProviderUnavailableError(PriceHunterError):
    code = "provider_unavailable"
    status_code = 503


class ProviderHTTPError(ProviderUnavailableError):
    """Safe upstream status for diagnostics; never retain bodies or credentials."""

    def __init__(self, http_status: int, *, error_codes: tuple[int, ...] = ()) -> None:
        self.http_status = http_status
        self.error_codes = error_codes
        super().__init__(f"Provider HTTP {http_status}")


class ProductNotFoundError(PriceHunterError):
    code = "not_found"
    status_code = 404


class RateLimitExceededError(PriceHunterError):
    code = "rate_limit"
    status_code = 429


class SubscriptionLimitReachedError(PriceHunterError):
    code = "subscription_limit"
    status_code = 409


class InvalidTargetPriceError(PriceHunterError):
    code = "invalid_target"


class FeatureUnavailableError(PriceHunterError):
    code = "feature_unavailable"
    status_code = 503


class FeatureRequiresUpgradeError(PriceHunterError):
    code = "feature_requires_upgrade"
    status_code = 403

    def __init__(self, feature: str = "") -> None:
        self.feature = feature
        super().__init__(self.code)


class PaymentRejectedError(PriceHunterError):
    code = "payment_rejected"


class BillingUnavailableError(PriceHunterError):
    code = "billing_unavailable"
    status_code = 503


class SubscriptionConflictError(PriceHunterError):
    code = "subscription_conflict"
    status_code = 409


class RenewalCancellationRequiredError(PriceHunterError):
    code = "cancel_renewal_first"
    status_code = 409


class BillingOperationPendingError(PriceHunterError):
    code = "billing_operation_pending"
    status_code = 409
